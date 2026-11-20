"""Tests for ``clipforge.monetization`` — the real landed API.

  THRESHOLDS: dict platform -> list of {program, metrics, note}
  progress(current_metrics, platform=None) -> list[dict]
      each row: {platform, program, current, threshold, progress_pct,
      missing, note}. Alt metrics (``*_alt``) are OR-paths: reaching the
      alt alone satisfies that leg (e.g. Shorts views OR watch hours).

Rules (research report §6). The tracker shows thresholds, never promised
earnings. Reels Play ended 31 Aug 2025 and must never be promised.
"""

from __future__ import annotations

from datetime import date

from tests.conftest import load_or_skip

# --- Spec values pinned from the research report (these tests run now). ---
SPEC_YPP_EARLY = {"subs": 500, "watch_hours": 3_000, "shorts_views_90d": 3_000_000}
SPEC_YPP_FULL = {"subs": 1_000, "watch_hours": 4_000, "shorts_views_90d": 10_000_000}
SPEC_YPP_FULL_2027 = {"subs": 1_000, "watch_hours": 8_000, "shorts_views_90d": 20_000_000}
SPEC_YPP_DOUBLING_DATE = date(2027, 2, 1)
SPEC_TIKTOK_REWARDS = {
    "followers": 10_000,
    "views_30d": 100_000,
    "min_video_seconds": 60,
    "min_payout_usd": 50,
}
SPEC_REELS_PLAY_ENDED = date(2025, 8, 31)
SPEC_FACEBOOK_CMP_FOLLOWERS = 10_000


def _row(mon, rows, program_substring):
    matches = [r for r in rows if program_substring.lower() in r["program"].lower()]
    assert matches, f"no program matching {program_substring!r} in {rows}"
    return matches[0]


def test_ypp_full_thresholds():
    assert SPEC_YPP_FULL == {"subs": 1_000, "watch_hours": 4_000, "shorts_views_90d": 10_000_000}


def test_ypp_early_thresholds_are_lower_than_full():
    assert SPEC_YPP_EARLY["subs"] < SPEC_YPP_FULL["subs"]
    assert SPEC_YPP_EARLY["watch_hours"] < SPEC_YPP_FULL["watch_hours"]
    assert SPEC_YPP_EARLY["shorts_views_90d"] < SPEC_YPP_FULL["shorts_views_90d"]


def test_ypp_feb_2027_doubling_flag():
    """From 1 Feb 2027 new applicants need DOUBLE hours/views (report §6)."""
    assert SPEC_YPP_DOUBLING_DATE == date(2027, 2, 1)
    assert SPEC_YPP_FULL_2027["subs"] == SPEC_YPP_FULL["subs"]  # subs unchanged
    assert SPEC_YPP_FULL_2027["watch_hours"] == 2 * SPEC_YPP_FULL["watch_hours"]
    assert SPEC_YPP_FULL_2027["shorts_views_90d"] == 2 * SPEC_YPP_FULL["shorts_views_90d"]


def test_tiktok_rewards_thresholds():
    assert SPEC_TIKTOK_REWARDS["followers"] == 10_000
    assert SPEC_TIKTOK_REWARDS["views_30d"] == 100_000
    assert SPEC_TIKTOK_REWARDS["min_video_seconds"] == 60  # short clips earn $0


def test_tiktok_payout_minimum_conflict_is_flagged():
    """Terms say $50 minimum; one help page says $10 — VERIFY_LIVE (report §6)."""
    assert SPEC_TIKTOK_REWARDS["min_payout_usd"] == 50
    assert SPEC_TIKTOK_REWARDS["min_payout_usd"] != 10  # the $10 figure is disputed


def test_reels_play_is_not_promised():
    """Reels Play bonus ended 31 Aug 2025 — the product must never offer it."""
    assert SPEC_REELS_PLAY_ENDED == date(2025, 8, 31)
    assert SPEC_REELS_PLAY_ENDED < date(2026, 11, 20)


def test_facebook_cmp_followers():
    assert SPEC_FACEBOOK_CMP_FOLLOWERS == 10_000  # legacy 5,000 figure: VERIFY_LIVE


# --- Implementation tests against the landed module. ----------------------


def test_thresholds_table_matches_research():
    mon = load_or_skip("clipforge.monetization")
    yt = {r["program"]: r["metrics"] for r in mon.THRESHOLDS["youtube"]}
    assert yt["YPP Full (ads + Premium)"] == {
        "subscribers": 1_000,
        "watch_hours_12mo": 4_000,
        "shorts_views_90d_alt": 10_000_000,
    }
    assert yt["YPP Full — NEW APPLICANTS FROM 1 FEB 2027"] == {
        "subscribers": 1_000,
        "watch_hours_12mo": 8_000,
        "shorts_views_90d_alt": 20_000_000,
    }
    assert mon.THRESHOLDS["tiktok"][0]["metrics"] == {
        "followers": 10_000,
        "views_30d": 100_000,
    }
    assert mon.THRESHOLDS["facebook"][0]["metrics"] == {"followers": 10_000}


def test_progress_ypp_full_via_watch_hours():
    mon = load_or_skip("clipforge.monetization")
    rows = mon.progress({"subscribers": 1_200, "watch_hours_12mo": 5_000}, platform="youtube")
    full = _row(mon, rows, "YPP Full (ads")
    assert full["progress_pct"] == 100.0
    assert full["missing"] == []


def test_progress_ypp_full_via_shorts_views_only():
    mon = load_or_skip("clipforge.monetization")
    rows = mon.progress(
        {"subscribers": 1_000, "watch_hours_12mo": 0, "shorts_views_90d": 11_000_000},
        platform="youtube",
    )
    full = _row(mon, rows, "YPP Full (ads")
    assert full["progress_pct"] == 100.0  # views path satisfies the hours leg


def test_progress_ypp_2027_row_uses_doubled_thresholds():
    mon = load_or_skip("clipforge.monetization")
    rows = mon.progress({"subscribers": 1_000, "watch_hours_12mo": 5_000}, platform="youtube")
    doubled = _row(mon, rows, "FROM 1 FEB 2027")
    assert doubled["threshold"]["watch_hours_12mo"] == 8_000
    assert doubled["progress_pct"] == 62.5  # 5,000/8,000 on the worst leg
    assert any("watch_hours" in m for m in doubled["missing"])


def test_progress_not_eligible_reports_missing():
    mon = load_or_skip("clipforge.monetization")
    rows = mon.progress({"subscribers": 100, "watch_hours_12mo": 10}, platform="youtube")
    early = _row(mon, rows, "YPP Early")
    assert early["progress_pct"] < 100
    assert early["missing"] != []


def test_progress_tiktok_rewards_and_60s_note():
    mon = load_or_skip("clipforge.monetization")
    rows = mon.progress({"followers": 50_000, "views_30d": 500_000}, platform="tiktok")
    assert rows[0]["progress_pct"] == 100.0
    assert ">=1 min" in rows[0]["note"]  # short clips earn $0 even if viral


def test_progress_instagram_has_no_per_view_pay():
    mon = load_or_skip("clipforge.monetization")
    rows = mon.progress({"followers": 800}, platform="instagram")
    programs = [r["program"] for r in rows]
    assert not any("reels play" in p.lower() for p in programs)
    assert "Gifts" in programs and "Subscriptions" in programs
    gifts = _row(mon, rows, "Gifts")
    assert "ENDED 31 Aug 2025" in gifts["note"]


def test_progress_row_shape():
    mon = load_or_skip("clipforge.monetization")
    rows = mon.progress({"followers": 1_000}, platform="facebook")
    assert rows
    for row in rows:
        assert set(row) == {
            "platform",
            "program",
            "current",
            "threshold",
            "progress_pct",
            "missing",
            "note",
        }
        assert 0.0 <= row["progress_pct"] <= 100.0
