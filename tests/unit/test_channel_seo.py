"""Tests for Channel SEO (``clipforge.channel_seo``).

URL normalising, yt-dlp flat-playlist parsing (mocked subprocess), the
top-videos / what-works / suggested-titles analysis, the optional
YouTube Data API enrichment (mocked HTTP), and the 24 h cache. No
network is touched by these tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import load_or_skip


@pytest.fixture(scope="module")
def seo_mod():
    return load_or_skip("clipforge.channel_seo")


# ---------------------------------------------------------------- URL input


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.youtube.com/@dailyclips", "https://www.youtube.com/@dailyclips"),
        ("https://www.youtube.com/@dailyclips/videos", "https://www.youtube.com/@dailyclips"),
        ("@dailyclips", "https://www.youtube.com/@dailyclips"),
        (
            "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
            "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
        ),
        ("UCabcdefghijklmnopqrstuv", "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv"),
        ("https://www.youtube.com/c/DailyClips", "https://www.youtube.com/c/DailyClips"),
        ("https://www.youtube.com/user/DailyClips", "https://www.youtube.com/user/DailyClips"),
        ("https://m.youtube.com/@dailyclips", "https://www.youtube.com/@dailyclips"),
    ],
)
def test_normalize_channel_url(seo_mod, raw, expected):
    assert seo_mod.normalize_channel_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.tiktok.com/@someone",
        "not a link at all",
        "https://www.youtube.com/playlist?list=PL123",
    ],
)
def test_normalize_rejects_video_and_other_links(seo_mod, raw):
    with pytest.raises(seo_mod.ChannelSeoError):
        seo_mod.normalize_channel_url(raw)


# ------------------------------------------------------- yt-dlp flat fetch


def _fake_run(payload, returncode=0, stderr=""):
    class Proc:
        def __init__(self):
            self.returncode = returncode
            self.stdout = json.dumps(payload)
            self.stderr = stderr

    def run(cmd, **kwargs):
        run.cmd = cmd
        return Proc()

    return run


def _sample_payload():
    return {
        "id": "UCabcdefghijklmnopqrstuv",
        "channel_id": "UCabcdefghijklmnopqrstuv",
        "uploader": "Daily Clips",
        "subscriber_count": 12000,
        "entries": [
            {
                "id": "aaa111",
                "title": "How to start your morning routine",
                "view_count": 50000,
                "duration": 55,
                "timestamp": 1763000000,
            },
            {
                "id": "bbb222",
                "title": "Morning routine: 3 mistakes to avoid",
                "view_count": 120000,
                "duration": 48,
                "timestamp": 1762900000,
            },
            {
                "id": "ccc333",
                "title": "Why your morning routine fails",
                "view_count": 8000,
                "duration": 60,
                "timestamp": 1762800000,
            },
            {
                "id": "ddd444",
                "title": "Coffee vs tea for your morning",
                "view_count": None,
                "duration": 50,
                "timestamp": 1762700000,
            },
        ],
    }


def test_fetch_channel_parses_flat_json(seo_mod):
    run = _fake_run(_sample_payload())
    data = seo_mod.fetch_channel("https://www.youtube.com/@dailyclips", _run=run, _ytdlp=["yt-dlp"])
    assert data["channel_name"] == "Daily Clips"
    assert data["subscriber_count"] == 12000
    assert len(data["videos"]) == 4
    assert data["videos"][0]["video_id"] == "aaa111"
    assert data["videos"][0]["views"] == 50000
    assert data["videos"][3]["views"] is None
    assert data["videos"][0]["url"] == "https://www.youtube.com/watch?v=aaa111"
    # The /videos tab is what gets read, flat, no download.
    assert run.cmd[-1] == "https://www.youtube.com/@dailyclips/videos"
    assert "--flat-playlist" in run.cmd
    assert "-J" in run.cmd


def test_fetch_channel_missing_ytdlp(seo_mod, monkeypatch):
    # No working binary (probe finds nothing) and no yt_dlp module.
    monkeypatch.setattr(seo_mod, "_ytdlp_command", lambda: None)
    with pytest.raises(seo_mod.ChannelSeoError, match="yt-dlp"):
        seo_mod.fetch_channel("https://www.youtube.com/@dailyclips")


def test_ytdlp_command_skips_broken_binary(seo_mod, monkeypatch):
    # A shim that exists but does not run must not shadow the working
    # `python -m yt_dlp` fallback (real case: ~/.local/bin/yt-dlp with a
    # system-python shebang on a machine where yt-dlp lives in a venv).
    monkeypatch.setattr(seo_mod, "probe_yt_dlp", lambda: {"found": False, "path": ""})
    command = seo_mod._ytdlp_command()
    assert command is not None
    assert command[-2:] == ["-m", "yt_dlp"]


def test_fetch_channel_failed_command(seo_mod):
    run = _fake_run({}, returncode=1, stderr="ERROR: this channel does not exist")
    with pytest.raises(seo_mod.ChannelSeoError, match="does not exist"):
        seo_mod.fetch_channel("https://www.youtube.com/@dailyclips", _run=run, _ytdlp=["yt-dlp"])


def test_fetch_channel_empty_channel(seo_mod):
    run = _fake_run({"entries": []})
    with pytest.raises(seo_mod.ChannelSeoError, match="no videos"):
        seo_mod.fetch_channel("https://www.youtube.com/@dailyclips", _run=run, _ytdlp=["yt-dlp"])


# ------------------------------------------------------------- analysis


def _analysis(seo_mod, topic=""):
    data = seo_mod.fetch_channel(
        "https://www.youtube.com/@dailyclips",
        _run=_fake_run(_sample_payload()),
        _ytdlp=["yt-dlp"],
    )
    return seo_mod.analyze_channel(data, topic=topic)


def test_top_videos_sorted_by_views(seo_mod):
    analysis = _analysis(seo_mod)
    ids = [v["video_id"] for v in analysis["top_videos"]]
    assert ids == ["bbb222", "aaa111", "ccc333", "ddd444"]  # no views -> last
    assert "no view count" in analysis["views_note"]


def test_what_works_common_words(seo_mod):
    analysis = _analysis(seo_mod)
    words = {w["word"]: w["count"] for w in analysis["what_works"]["common_words"]}
    assert words["morning"] == 4
    assert words["routine"] == 3
    assert "the" not in words  # stopwords are dropped


def test_title_patterns_counted(seo_mod):
    analysis = _analysis(seo_mod)
    patterns = analysis["what_works"]["title_patterns"]
    assert patterns["starts_with_how_to"] == 1
    assert patterns["starts_with_why"] == 1
    assert patterns["contains_number"] == 1
    assert patterns["contains_vs"] == 1


def test_suggested_titles_use_channel_shapes(seo_mod):
    analysis = _analysis(seo_mod, topic="evening routine")
    titles = [s["title"] for s in analysis["suggested_titles"]]
    assert len(titles) == 3
    assert titles[0] == "How to evening routine"  # channel's winning shape first
    assert all("evening routine" in t for t in titles)
    assert all(s["why"] for s in analysis["suggested_titles"])


def test_suggested_titles_without_topic_say_so(seo_mod):
    analysis = _analysis(seo_mod)
    assert any("morning" in s["title"] for s in analysis["suggested_titles"])
    assert any("type your own topic" in s["why"] for s in analysis["suggested_titles"])


def test_suggested_tags_from_top_titles(seo_mod):
    analysis = _analysis(seo_mod, topic="evening routine")
    assert "morning" in analysis["suggested_tags"]
    assert "routine" in analysis["suggested_tags"]
    assert "evening" in analysis["suggested_tags"]
    assert len(analysis["suggested_tags"]) <= 15


def test_no_views_at_all_says_so(seo_mod):
    payload = _sample_payload()
    for entry in payload["entries"]:
        entry["view_count"] = None
    data = seo_mod.fetch_channel(
        "https://www.youtube.com/@dailyclips", _run=_fake_run(payload), _ytdlp=["yt-dlp"]
    )
    analysis = seo_mod.analyze_channel(data)
    assert analysis["views_source"] == "unavailable"
    assert "channel order" in analysis["views_note"]


# ------------------------------------------------- YouTube API enrichment


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_enrich_with_youtube_api(seo_mod):
    videos = [
        {"video_id": "aaa111", "title": "How to start", "views": 10},
        {"video_id": "bbb222", "title": "Mistakes", "views": None},
    ]
    payload = {
        "items": [
            {
                "id": "aaa111",
                "statistics": {"viewCount": "50000"},
                "snippet": {"tags": ["morning routine", "habits"]},
            },
            {
                "id": "bbb222",
                "statistics": {"viewCount": "120000"},
                "snippet": {"tags": ["morning routine", "mistakes"]},
            },
        ]
    }
    calls = {}

    def fake_get(url, params=None, timeout=None):
        calls["params"] = params
        return _FakeResponse(200, payload)

    enriched, top_tags = seo_mod.enrich_with_youtube_api(videos, "key123", _http_get=fake_get)
    assert enriched[0]["views"] == 50000
    assert enriched[1]["views"] == 120000
    assert enriched[0]["tags"] == ["morning routine", "habits"]
    assert top_tags[0] == "morning routine"
    assert calls["params"]["id"] == "aaa111,bbb222"


def test_enrich_bad_key_raises_plain_error(seo_mod):
    with pytest.raises(seo_mod.ChannelSeoError, match="did not work"):
        seo_mod.enrich_with_youtube_api(
            [{"video_id": "aaa111", "title": "x", "views": 1}],
            "bad-key",
            _http_get=lambda *a, **k: _FakeResponse(403, {}),
        )


# ------------------------------------------------------------------- cache


def test_cache_round_trip_and_expiry(seo_mod, tmp_path):
    analysis = {"channel_name": "Daily Clips", "top_videos": []}
    seo_mod.save_cached(tmp_path, "UC123", analysis)
    loaded = seo_mod.load_cached(tmp_path, "UC123")
    assert loaded is not None
    assert loaded["channel_name"] == "Daily Clips"
    assert loaded["cached"] is True
    assert loaded["fetched_at"]
    assert seo_mod.load_cached(tmp_path, "OTHER") is None
    # Stale entries (older than 24 h) are ignored.
    cache_file = Path(tmp_path) / seo_mod.CACHE_FILENAME
    cache = json.loads(cache_file.read_text())
    cache["UC123"]["fetched_at"] = "2020-01-01T00:00:00+00:00"
    cache_file.write_text(json.dumps(cache))
    assert seo_mod.load_cached(tmp_path, "UC123") is None
