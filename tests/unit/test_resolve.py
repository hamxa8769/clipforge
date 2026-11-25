"""URL resolution for the Dashboard one-box (v0.11.0).

``POST /resolve`` turns a pasted link into a video (one click to clip)
or a channel's video list (pick one or several). These tests drive
:mod:`clipforge.resolve` through a fake yt-dlp binary (flat-playlist
extraction + metadata probe), so they run without network or a real
YouTube fetch — the live behaviour is verified separately in the
v0.11.0 E2E run (real yt-dlp against a public channel + video).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipforge.resolve import (
    ResolveError,
    _channel_videos_url,
    classify_url,
    list_channel_videos_api,
    list_channel_videos_ytdlp,
    resolve_url,
    resolve_video,
)

VIDEO_URL = "https://www.youtube.com/watch?v=M7lc1UVf-VE"
CHANNEL_URL = "https://www.youtube.com/@TestChannel"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLtest123"

FAKE_YTDLP = '''#!/usr/bin/env python3
"""Fake yt-dlp for resolve tests: probe + flat-playlist modes."""
import json, os, sys

log_path = os.environ["FAKE_YTDLP_LOG"]
mode = os.environ.get("FAKE_YTDLP_MODE", "ok")
argv = sys.argv[1:]
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")

if "--simulate" in argv:
    if mode == "unavailable":
        sys.stderr.write("ERROR: [youtube] M7lc1UVf-VE: This video is unavailable\\n")
        sys.exit(1)
    if mode == "botcheck" and "youtube:player_client=android" not in argv:
        sys.stderr.write("ERROR: [youtube] M7lc1UVf-VE: Sign in to confirm you\\u2019re "
                         "not a bot. Use --cookies-from-browser or --cookies for the "
                         "authentication.\\n")
        sys.exit(1)
    if mode == "live":
        print(json.dumps({"id": "M7lc1UVf-VE", "title": "Live stream",
                          "duration": 0, "is_live": True, "_type": "video"}))
        sys.exit(0)
    print(json.dumps({
        "id": "M7lc1UVf-VE", "title": "Fake video", "duration": 272,
        "uploader": "Test Channel", "channel_id": "UCtest",
        "upload_date": "20240115",
        "thumbnail": "https://i.ytimg.com/vi/M7lc1UVf-VE/hqdefault.jpg",
        "is_live": False, "_type": "video"}))
    sys.exit(0)

if "--flat-playlist" in argv:
    if mode == "empty":
        print(json.dumps({"id": "UCtest", "channel": "Test Channel", "entries": []}))
        sys.exit(0)
    if mode == "botcheck" and "youtube:player_client=android" not in argv:
        sys.stderr.write("ERROR: [youtube] UCtest: Sign in to confirm you\\u2019re "
                         "not a bot. Use --cookies-from-browser or --cookies for the "
                         "authentication.\\n")
        sys.exit(1)
    if mode == "unavailable":
        sys.stderr.write("ERROR: [youtube] UCtest: This channel does not exist.\\n")
        sys.exit(1)
    end = 25
    if "--playlist-end" in argv:
        end = int(argv[argv.index("--playlist-end") + 1])
    entries = [
        {"id": f"vid{i:02d}", "title": f"Video {i}", "uploader": "Test Channel",
         "channel_id": "UCtest", "duration": 60 + i, "upload_date": "20240115",
         "thumbnails": [{"url": f"https://i.ytimg.com/vi/vid{i:02d}/hq.jpg"}]}
        for i in range(1, 31)
    ]
    print(json.dumps({"id": "UCtest", "channel": "Test Channel",
                      "entries": entries[:end]}))
    sys.exit(0)

sys.stderr.write("unexpected fake yt-dlp call: " + " ".join(argv) + "\\n")
sys.exit(2)
'''


@pytest.fixture()
def fake_ytdlp(tmp_path: Path, monkeypatch) -> Path:
    script = tmp_path / "fake-ytdlp.py"
    script.write_text(FAKE_YTDLP, encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "argv.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("FAKE_YTDLP_LOG", str(log))
    return script


def _argv_runs(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "argv.log"
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# ------------------------------------------------------------ classification
@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://www.youtube.com/watch?v=M7lc1UVf-VE", "video"),
        ("https://youtu.be/M7lc1UVf-VE", "video"),
        ("https://www.youtube.com/shorts/abc123XYZ", "video"),
        ("https://www.youtube.com/embed/abc123", "video"),
        ("https://www.youtube.com/live/abc123", "video"),
        ("https://m.youtube.com/watch?v=abc123", "video"),
        # A watch link with a list= param is still the one video.
        ("https://www.youtube.com/watch?v=abc123&list=PL999", "video"),
        ("https://www.youtube.com/@SomeChannel", "channel"),
        ("https://www.youtube.com/@SomeChannel/videos", "channel"),
        ("https://www.youtube.com/@SomeChannel/shorts", "channel"),
        ("https://www.youtube.com/channel/UC123456", "channel"),
        ("https://www.youtube.com/c/SomeName", "channel"),
        ("https://www.youtube.com/user/SomeName", "channel"),
        ("https://www.youtube.com/playlist?list=PL123", "playlist"),
        ("https://example.com/watch?v=abc", "unknown"),
        ("https://www.youtube.com/", "unknown"),
        ("not a url", "unknown"),
        ("", "unknown"),
    ],
)
def test_classify_url(url: str, kind: str) -> None:
    assert classify_url(url) == kind


def test_channel_videos_url_appends_videos_tab() -> None:
    assert _channel_videos_url("https://www.youtube.com/@Ch") == "https://www.youtube.com/@Ch/videos"
    assert _channel_videos_url("https://www.youtube.com/channel/UCx") == "https://www.youtube.com/channel/UCx/videos"
    # A link that already names a tab is used as-is.
    assert _channel_videos_url("https://www.youtube.com/@Ch/shorts") == "https://www.youtube.com/@Ch/shorts"
    assert _channel_videos_url(PLAYLIST_URL) == PLAYLIST_URL


# ------------------------------------------------------------ video resolve
def test_resolve_video_returns_title_channel_duration(fake_ytdlp: Path) -> None:
    video = resolve_video(VIDEO_URL, str(fake_ytdlp))
    assert video.video_id == "M7lc1UVf-VE"
    assert video.title == "Fake video"
    assert video.channel_title == "Test Channel"
    assert video.duration_s == 272
    assert video.url == VIDEO_URL


def test_resolve_video_unavailable_is_plain_and_classified(
    fake_ytdlp: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_YTDLP_MODE", "unavailable")
    with pytest.raises(ResolveError) as excinfo:
        resolve_video(VIDEO_URL, str(fake_ytdlp))
    assert excinfo.value.code == "youtube_unavailable"
    assert "no longer available" in str(excinfo.value)


def test_resolve_video_live_stream_is_rejected(fake_ytdlp: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_YTDLP_MODE", "live")
    with pytest.raises(ResolveError) as excinfo:
        resolve_video(VIDEO_URL, str(fake_ytdlp))
    assert excinfo.value.code == "youtube_live"


def test_resolve_video_bot_check_retries_android_client(
    fake_ytdlp: Path, tmp_path: Path, monkeypatch
) -> None:
    """Web client bot-blocked → one Android retry, like ingest
    (v0.10.3) — the one-box must not be stricter than the job path."""
    monkeypatch.setenv("FAKE_YTDLP_MODE", "botcheck")
    video = resolve_video(VIDEO_URL, str(fake_ytdlp))
    assert video.video_id == "M7lc1UVf-VE"
    runs = _argv_runs(tmp_path)
    assert len(runs) == 2
    assert "youtube:player_client=android" in runs[1]


def test_list_channel_videos_bot_check_retries_android_client(
    fake_ytdlp: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_YTDLP_MODE", "botcheck")
    channel, videos = list_channel_videos_ytdlp(CHANNEL_URL, str(fake_ytdlp), max_results=2)
    assert channel["title"] == "Test Channel"
    assert len(videos) == 2
    runs = _argv_runs(tmp_path)
    assert len(runs) == 2
    assert "youtube:player_client=android" in runs[1]


# ------------------------------------------------------------ channel listing
def test_list_channel_videos_newest_first_capped(
    fake_ytdlp: Path, tmp_path: Path
) -> None:
    channel, videos = list_channel_videos_ytdlp(CHANNEL_URL, str(fake_ytdlp), max_results=5)
    assert channel["title"] == "Test Channel"
    assert len(videos) == 5
    assert videos[0].video_id == "vid01"
    assert videos[0].duration_s == 61
    assert videos[0].thumbnail_url == "https://i.ytimg.com/vi/vid01/hq.jpg"
    runs = _argv_runs(tmp_path)
    assert len(runs) == 1
    assert "--flat-playlist" in runs[0] and "--dump-single-json" in runs[0]
    assert runs[0][runs[0].index("--playlist-end") + 1] == "5"
    assert runs[0][-1] == CHANNEL_URL + "/videos"


def test_list_channel_videos_empty_is_channel_empty(
    fake_ytdlp: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_YTDLP_MODE", "empty")
    with pytest.raises(ResolveError) as excinfo:
        list_channel_videos_ytdlp(CHANNEL_URL, str(fake_ytdlp))
    assert excinfo.value.code == "channel_empty"


def test_list_channel_videos_unavailable_is_plain(
    fake_ytdlp: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_YTDLP_MODE", "unavailable")
    with pytest.raises(ResolveError) as excinfo:
        list_channel_videos_ytdlp(CHANNEL_URL, str(fake_ytdlp))
    assert excinfo.value.code == "channel_unavailable"


# ------------------------------------------------------------ API-key listing
def _fake_api_get(api_key: str, path: str, params: dict) -> dict:
    assert api_key == "test-key"
    if path == "channels":
        assert params.get("forHandle") == "@TestChannel"
        return {
            "items": [
                {
                    "id": "UCtest",
                    "snippet": {"title": "Test Channel"},
                    "contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}},
                }
            ]
        }
    if path == "playlistItems":
        assert params.get("playlistId") == "UUtest"
        return {
            "items": [
                {
                    "snippet": {
                        "title": "API video 1",
                        "channelTitle": "Test Channel",
                        "publishedAt": "2024-01-15T10:00:00Z",
                        "resourceId": {"videoId": "apiv01"},
                        "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/apiv01/hq.jpg"}},
                    }
                }
            ]
        }
    raise AssertionError(f"unexpected API path {path}")


def test_api_listing_used_when_key_saved(monkeypatch) -> None:
    monkeypatch.setattr("clipforge.resolve._api_get", _fake_api_get)
    channel, videos = list_channel_videos_api(CHANNEL_URL, "test-key")
    assert channel["id"] == "UCtest"
    assert channel["title"] == "Test Channel"
    assert len(videos) == 1
    assert videos[0].video_id == "apiv01"
    assert videos[0].published_at == "2024-01-15T10:00:00Z"


def test_api_listing_skipped_for_c_slash_links() -> None:
    # /c/ and /user/ have no direct API lookup — None means "fall
    # back to the yt-dlp listing", never an error.
    assert list_channel_videos_api("https://www.youtube.com/c/Name", "k") is None
    assert list_channel_videos_api("https://www.youtube.com/user/Name", "k") is None


def test_resolve_url_falls_back_to_ytdlp_when_api_fails(
    fake_ytdlp: Path, monkeypatch
) -> None:
    def _boom(api_key: str, path: str, params: dict) -> dict:
        raise ResolveError("api down", code="youtube_api_failed")

    monkeypatch.setattr("clipforge.resolve._api_get", _boom)
    out = resolve_url(CHANNEL_URL, str(fake_ytdlp), api_key="test-key", max_results=3)
    assert out["kind"] == "channel"
    assert out["source"] == "ytdlp"
    assert len(out["videos"]) == 3


def test_resolve_url_api_source_when_api_works(fake_ytdlp: Path, monkeypatch) -> None:
    monkeypatch.setattr("clipforge.resolve._api_get", _fake_api_get)
    out = resolve_url(CHANNEL_URL, str(fake_ytdlp), api_key="test-key")
    assert out["kind"] == "channel"
    assert out["source"] == "api"
    assert out["videos"][0]["video_id"] == "apiv01"


def test_resolve_url_video_shape(fake_ytdlp: Path) -> None:
    out = resolve_url(VIDEO_URL, str(fake_ytdlp))
    assert out["kind"] == "video"
    assert out["video"]["video_id"] == "M7lc1UVf-VE"


def test_resolve_url_rejects_non_youtube() -> None:
    with pytest.raises(ResolveError) as excinfo:
        resolve_url("https://example.com/watch?v=abc", "/nonexistent/yt-dlp")
    assert excinfo.value.code == "unknown_url"


def test_resolve_url_without_ytdlp_names_the_install(fake_ytdlp: Path) -> None:
    with pytest.raises(ResolveError) as excinfo:
        resolve_url(VIDEO_URL, None)
    assert excinfo.value.code == "yt_dlp_missing"
