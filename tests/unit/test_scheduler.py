"""Tests for ``clipforge.scheduler`` + ``clipforge.publishing.limits`` — real API.

  DAILY_CAPS (publishing.limits): {"youtube": 6, "instagram": 100,
      "facebook": 30, "tiktok": 5} — YouTube 6 is the CONSERVATIVE figure
      (1,600 units/insert from the 10,000-unit pool); the newer separate
      100-uploads/day bucket is recorded as daily_uploads_new_bucket and
      is VERIFY_LIVE in Cloud Console.
  due_posts(session, now=None) -> list[Post]
      approved/scheduled and due (or unscheduled); pending posts excluded.
  published_today_count(session, account_id, platform, now=None) -> int
  daily_cap_for(session, post) -> int   # account override wins, else DAILY_CAPS
  run_due_posts(session, dry_run=True, now=None, publish_fn=None)
      -> list[dict]; idempotent (already-published/dry-run posts leave the
      due set); cap-reached posts are left scheduled with an error note.

Rules (research report §3 + ARCHITECTURE.md §4): caps are per account per
platform per rolling 24 h; the idempotency key on Post is unique, so a
clip is never published twice by accident; the scheduler only ever sees
approved clips.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from tests.conftest import load_or_skip

# --- Spec values pinned from the research report (these tests run now). ---
# YouTube: conservative 6/day (older quota docs); the newer 100/day bucket
# is VERIFY_LIVE. TikTok 5 = pending inbox drafts / 24 h pre-audit; the
# audited Direct Post cap is set from the application. Instagram 100 and
# Facebook 30 are rolling-24 h API caps.
SPEC_DAILY_CAPS = {"youtube": 6, "instagram": 100, "facebook": 30, "tiktok": 5, "x": 25}


@pytest.fixture
def session(tmp_path):
    db = load_or_skip("clipforge.db")
    url = f"sqlite:///{tmp_path}/sched.db"
    db.init_db(url)
    sess = db.session_factory(url)()
    yield sess
    sess.close()


def _post(platform="youtube", status="approved", **kwargs):
    db = load_or_skip("clipforge.db")
    return db.Post(platform=platform, status=status, **kwargs)


def test_daily_caps_match_research():
    limits = load_or_skip("clipforge.publishing.limits")
    assert limits.DAILY_CAPS == SPEC_DAILY_CAPS
    assert SPEC_DAILY_CAPS["instagram"] == 100
    assert SPEC_DAILY_CAPS["facebook"] == 30
    assert SPEC_DAILY_CAPS["tiktok"] == 5


def test_youtube_cap_is_the_conservative_figure_with_new_bucket_flagged():
    """Quota conflict: code enforces 6/day; 100/day bucket is VERIFY_LIVE."""
    limits = load_or_skip("clipforge.publishing.limits")
    assert limits.DAILY_CAPS["youtube"] == 6
    assert limits.PLATFORM_LIMITS["youtube"]["daily_uploads_new_bucket"] == 100
    assert limits.VERIFY_LIVE is True


def test_daily_caps_are_positive():
    assert all(isinstance(v, int) and v > 0 for v in SPEC_DAILY_CAPS.values())


def test_all_four_platforms_have_caps():
    assert set(SPEC_DAILY_CAPS) == {"youtube", "instagram", "facebook", "tiktok", "x"}


# --- Implementation tests against the landed module (DB-backed). -----------


def test_published_today_count_empty(session):
    sched = load_or_skip("clipforge.scheduler")
    assert sched.published_today_count(session, None, "facebook") == 0


def test_daily_cap_for_defaults_and_account_override(session):
    sched = load_or_skip("clipforge.scheduler")
    db = load_or_skip("clipforge.db")
    post = _post("facebook")
    assert sched.daily_cap_for(session, post) == 30
    account = db.SocialAccount(provider="facebook", daily_cap_override=5)
    session.add(account)
    session.commit()
    post.account_id = account.id
    assert sched.daily_cap_for(session, post) == 5


def test_due_posts_only_approved_and_due(session):
    sched = load_or_skip("clipforge.scheduler")
    db = load_or_skip("clipforge.db")
    approved = _post("youtube", status=db.PostStatus.APPROVED.value)
    pending = _post("youtube", status=db.PostStatus.PENDING_APPROVAL.value)
    session.add_all([approved, pending])
    session.commit()
    due = sched.due_posts(session)
    assert [p.id for p in due] == [approved.id]


def test_run_due_posts_dry_run_then_idempotent(session):
    sched = load_or_skip("clipforge.scheduler")
    base = load_or_skip("clipforge.publishing.base")
    post = _post("youtube", status="approved")
    session.add(post)
    session.commit()

    def stub_publish(p, dry_run):
        return base.PublishResult(platform=p.platform, status="dry_run", dry_run=True)

    results = sched.run_due_posts(session, dry_run=True, publish_fn=stub_publish)
    assert len(results) == 1
    assert results[0]["status"] == "dry_run"
    assert results[0]["post_id"] == post.id
    session.refresh(post)
    assert post.status == "dry_run"
    assert post.attempts == 1

    # Second run: the post left the due set — nothing is published twice.
    assert sched.run_due_posts(session, dry_run=True, publish_fn=stub_publish) == []


def test_run_due_posts_skips_cap_reached_post(session):
    sched = load_or_skip("clipforge.scheduler")
    db = load_or_skip("clipforge.db")
    account = db.SocialAccount(provider="facebook", daily_cap_override=0)
    session.add(account)
    session.commit()
    post = _post("facebook", status="approved", account_id=account.id)
    session.add(post)
    session.commit()

    def stub_publish(p, dry_run):
        raise AssertionError("publish must not be called once the cap is reached")

    results = sched.run_due_posts(session, dry_run=True, publish_fn=stub_publish)
    assert len(results) == 1
    assert results[0]["status"] == "skipped_cap"
    assert "cap" in results[0]["message"]
    session.refresh(post)
    assert post.status == "approved"  # left scheduled, not failed


def test_post_idempotency_key_is_unique(session):
    session.add(_post("youtube", status="approved", idempotency_key="same-key"))
    session.commit()
    session.add(_post("tiktok", status="approved", idempotency_key="same-key"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
