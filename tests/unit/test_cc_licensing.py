"""Unit tests for the v0.7.0 lawful CC pipeline: licensing + thumbnails + SEO attribution.

Shared field names (see ``clipforge.licensing.SOURCE_LICENSE_FIELDS``):
``source_license``, ``source_url``, ``source_title``, ``source_channel``,
``attribution_text``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clipforge.licensing import (
    append_attribution,
    build_attribution_text,
    is_creative_commons,
    is_lawful_source,
    normalize_license,
    publish_blocked_reason,
)
from clipforge.seo import generate_all, generate_seo
from clipforge.thumbnails import (
    ThumbnailError,
    generate_thumbnails,
    regenerate_thumbnail,
    wrap_title,
)

CC_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
CREDIT = (
    "Source: Sunset Timelapse by Free Nature Films — https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    " — Licensed under Creative Commons Attribution 3.0 Unported "
    "(https://creativecommons.org/licenses/by/3.0/)"
)


def _make_video(path: Path, duration: int = 4) -> Path:
    """A tiny real mp4 (testsrc) so ffmpeg frame extraction can run."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size=320x240:rate=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "10",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0 or not path.exists():
        pytest.skip(f"ffmpeg could not generate a test video: {proc.stderr}")
    return path


# ---------------------------------------------------------------- licensing
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("CC BY 4.0", "cc-by-4.0"),
        ("Creative Commons Attribution 3.0", "cc-by-3.0"),
        ("Creative Commons Attribution-ShareAlike 4.0 International", "cc-by-sa-4.0"),
        ("CC0", "cc0"),
        ("Public Domain", "public-domain"),
        ("YouTube Standard License", "standard"),
        ("standard youtube license", "standard"),
        (None, None),
        ("", None),
    ],
)
def test_normalize_license_maps_free_text(raw, expected):
    assert normalize_license(raw) == expected


def test_unknown_licence_is_never_silently_relabelled_cc():
    assert normalize_license("Some Weird Licence 2.0") == "some weird licence 2.0"
    assert not is_creative_commons("some weird licence 2.0")


def test_build_attribution_text_cc_by():
    text = build_attribution_text(
        "Sunset Timelapse", "Free Nature Films", CC_URL, "cc-by-3.0"
    )
    assert text == CREDIT


def test_build_attribution_text_empty_for_own_and_standard():
    assert build_attribution_text("T", "C", CC_URL, "own") == ""
    assert build_attribution_text("T", "C", CC_URL, "standard") == ""
    assert build_attribution_text("T", "C", CC_URL, None) == ""


def test_append_attribution_is_idempotent():
    desc = "A great clip\n\n#shorts"
    once = append_attribution(desc, CREDIT)
    assert once.count(CREDIT) == 1
    twice = append_attribution(once, CREDIT)
    assert twice.count(CREDIT) == 1
    assert twice == once


def test_append_attribution_replaces_stale_block_on_licence_change():
    old = build_attribution_text("Old Title", "Old Channel", CC_URL, "cc-by-4.0")
    desc = append_attribution("Clip text", old)
    new = build_attribution_text("New Title", "New Channel", CC_URL, "cc-by-3.0")
    updated = append_attribution(desc, new)
    assert updated.count("Licensed under") == 1
    assert new in updated
    assert old not in updated


def test_publish_blocked_reason_only_for_standard_licence():
    assert publish_blocked_reason("standard") is not None
    assert "Standard licence" in publish_blocked_reason("standard")
    assert publish_blocked_reason("cc-by-4.0") is None
    assert publish_blocked_reason("cc0") is None
    assert publish_blocked_reason("public-domain") is None
    assert publish_blocked_reason(None) is None  # unrecorded (own/local) passes
    assert publish_blocked_reason("standard", own_content=True) is None


def test_is_lawful_source():
    assert is_lawful_source("cc-by-4.0")
    assert is_lawful_source("public-domain")
    assert is_lawful_source(None, own_content=True)
    assert not is_lawful_source("standard")
    assert not is_lawful_source(None)


# ------------------------------------------------------- SEO attribution
def test_seo_description_carries_credit_block_for_cc_source():
    variants = generate_all("nature", "sunset timelapse", "sunset", 30.0, attribution_text=CREDIT)
    for platform, variant in variants.items():
        assert CREDIT in variant["description"], platform
        # Titles are never touched by attribution.
        assert "Licensed under" not in variant["title"]


def test_seo_credit_block_survives_regeneration_without_duplication():
    first = generate_seo("youtube", "nature", "sunset", "sunset", 30.0, attribution_text=CREDIT)
    # Re-running SEO on the previous description (what the stored-SEO path
    # effectively does on regeneration) keeps exactly one block.
    second = generate_seo(
        "youtube", "nature", first["description"], "sunset", 30.0, attribution_text=CREDIT
    )
    assert second["description"].count(CREDIT) == 1
    assert second["description"].count("Licensed under") == 1


def test_seo_without_attribution_is_unchanged():
    variant = generate_seo("youtube", "nature", "sunset", "sunset", 30.0)
    assert "Licensed under" not in variant["description"]
    assert "Source:" not in variant["description"]


# ---------------------------------------------------------------- thumbnails
def test_wrap_title_splits_into_short_lines():
    lines = wrap_title("How to make the perfect sunset timelapse in five minutes", width=20)
    assert 1 < len(lines) <= 3
    assert all(len(line) <= 20 for line in lines)
    assert wrap_title("") == []


def test_generate_thumbnails_two_variants_deterministic(tmp_path: Path):
    video = _make_video(tmp_path / "clip.mp4", duration=4)
    out = tmp_path / "thumbs"
    variants = generate_thumbnails(video, out, "Sunset Timelapse", start_s=0.0, end_s=4.0)
    assert len(variants) == 2
    assert variants[0]["variant"] == 1
    assert variants[0]["filename"] == "thumbnail.jpg"
    assert variants[1]["filename"] == "thumbnail_2.jpg"
    # Deterministic frames: 25% and 75% of the 4s clip.
    assert variants[0]["frame_second"] == pytest.approx(1.0)
    assert variants[1]["frame_second"] == pytest.approx(3.0)
    for variant in variants:
        path = Path(variant["path"])
        assert path.is_file()
        assert path.read_bytes()[:3] == b"\xff\xd8\xff"  # JPEG magic
    # The two frames differ (testsrc changes over time).
    assert (out / "thumbnail.jpg").read_bytes() != (out / "thumbnail_2.jpg").read_bytes()
    # Same inputs again → same frame seconds (deterministic).
    again = generate_thumbnails(
        video, tmp_path / "thumbs2", "Sunset Timelapse", start_s=0.0, end_s=4.0
    )
    assert [v["frame_second"] for v in again] == [v["frame_second"] for v in variants]


def test_generate_thumbnails_title_overlay_present(tmp_path: Path):
    video = _make_video(tmp_path / "clip.mp4", duration=4)
    with_title = generate_thumbnails(
        video, tmp_path / "with", "Sunset Timelapse", start_s=0.0, end_s=4.0
    )
    generate_thumbnails(video, tmp_path / "without", "", start_s=0.0, end_s=4.0)
    # Same frame, different overlay → different bytes.
    assert (tmp_path / "with" / "thumbnail.jpg").read_bytes() != (
        tmp_path / "without" / "thumbnail.jpg"
    ).read_bytes()
    assert with_title[0]["title_text"] == "Sunset Timelapse"


def test_regenerate_thumbnail_single_variant_keeps_other(tmp_path: Path):
    video = _make_video(tmp_path / "clip.mp4", duration=4)
    out = tmp_path / "thumbs"
    generate_thumbnails(video, out, "Sunset Timelapse", start_s=0.0, end_s=4.0)
    before_v2 = (out / "thumbnail_2.jpg").read_bytes()
    updated = regenerate_thumbnail(
        video, out, "New Title", variant=2, frame_second=2.0, start_s=0.0, end_s=4.0
    )
    assert updated["variant"] == 2
    assert updated["filename"] == "thumbnail_2.jpg"
    assert updated["frame_second"] == pytest.approx(2.0)
    assert (out / "thumbnail_2.jpg").read_bytes() != before_v2
    # Variant 1 untouched, still a valid JPEG.
    assert (out / "thumbnail.jpg").read_bytes()[:3] == b"\xff\xd8\xff"


def test_generate_thumbnails_missing_clip_raises(tmp_path: Path):
    with pytest.raises(ThumbnailError):
        generate_thumbnails(tmp_path / "nope.mp4", tmp_path / "out", "Title")


def test_generate_thumbnails_frame_second_clamped_to_clip(tmp_path: Path):
    video = _make_video(tmp_path / "clip.mp4", duration=4)
    variants = generate_thumbnails(
        video,
        tmp_path / "out",
        "Title",
        start_s=0.0,
        end_s=4.0,
        frame_seconds=[99.0, 3.0],
    )
    assert variants[0]["frame_second"] == pytest.approx(3.9)  # clamped 0.1s before EOF
