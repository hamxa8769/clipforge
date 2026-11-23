"""Unit tests for ASS caption generation (``clipforge.captions``).

Regression test (v0.3.0): ``words_to_ass`` once joined all Dialogue
events with ``"".join`` and no trailing newline, so the whole [Events]
section became a single line. libass then parsed only the first event
and the rest rendered as literal ``Dialogue: ...`` caption text on the
clip, with no captions at all after the first 0.24 s.
"""

from __future__ import annotations

from clipforge.captions import words_to_ass
from clipforge.transcribe import Word


def _words() -> list[Word]:
    # 6 words, 0.24 s each — two WORDS_PER_LINE groups of 3 (default).
    return [
        Word(text=text, start=i * 0.24, end=(i + 1) * 0.24)
        for i, text in enumerate(["Stop!", "This", "secret", "is", "shocking,", "and"])
    ]


def test_each_dialogue_event_is_on_its_own_line():
    ass = words_to_ass(_words())
    dialogue_lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogue_lines) == 6, ass
    # No event's text leaks the next event's marker into the caption.
    for line in dialogue_lines:
        assert line.count("Dialogue:") == 1, line


def test_events_carry_the_right_times_and_words():
    ass = words_to_ass(_words())
    lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert lines[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.24,BoldPop,")
    assert "Stop!" in lines[0]
    assert "shocking," in lines[4]
    # Karaoke-fill collapses each group into a single event.
    karaoke = words_to_ass(_words(), style_name="karaoke-fill")
    k_lines = [line for line in karaoke.splitlines() if line.startswith("Dialogue:")]
    assert len(k_lines) == 2, karaoke
    assert "\\k24" in k_lines[0]


def test_empty_word_list_is_header_only():
    ass = words_to_ass([])
    assert "[Events]" in ass
    assert "Dialogue:" not in ass


# --- Animated Facecam styles (v0.10.0) -------------------------------------


def test_animated_styles_exist_with_correct_colours():
    # ASS colours are &HBBGGRR: cyan must be blue-heavy (FFF000), not
    # the reference's channel-swapped yellow.
    from clipforge.captions import ANIMATED_STYLE_DEFS, STYLES

    assert set(ANIMATED_STYLE_DEFS) == {
        "bounce",
        "hormozi",
        "mrbeast",
        "karaoke",
        "hype",
        "clean",
    }
    assert ANIMATED_STYLE_DEFS["bounce"]["active"] == "&H00FFF000"  # cyan
    assert ANIMATED_STYLE_DEFS["mrbeast"]["base"] == "&H0000FFFF"  # yellow
    assert ANIMATED_STYLE_DEFS["mrbeast"]["active"] == "&H000066FF"  # orange
    for name in ANIMATED_STYLE_DEFS:
        assert name in STYLES  # pipeline validation accepts them


def test_bounce_style_pops_the_active_word():
    ass = words_to_ass(_words(), style_name="bounce")
    lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    # Every word gets before/active/after phases (minus group edges).
    assert len(lines) > len(_words())
    active = [line for line in lines if line.startswith("Dialogue: 2,")]
    assert active, ass
    assert all("\\t(0,60,\\fscx" in line for line in active)
    assert all("\\pos(" in line for line in lines)
    # Uppercase words, cyan active colour.
    assert "STOP!" in ass
    assert "\\c&H00FFF000" in ass


def test_karaoke_style_wipes_with_kf():
    ass = words_to_ass(_words(), style_name="karaoke")
    lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    # One event per group; "Stop!" ends a sentence, so the 6 words form
    # 3 groups ([Stop!] [This secret is] [shocking, and]).
    assert len(lines) == 3
    assert all("\\kf24" in line for line in lines)
    assert all("\\t(" not in line for line in lines)


def test_clean_style_keeps_sentence_case_and_smaller_size():
    ass = words_to_ass(_words(), style_name="clean")
    assert "Stop!" in ass  # not uppercased
    assert "STOP!" not in ass
    assert "Style: Clean,DejaVu Sans,76," in ass


def test_animated_caption_y_moves_the_line():
    ass = words_to_ass(_words(), style_name="hype", caption_y=1360)
    assert "\\pos(" in ass
    assert ",1360)" in ass
    default = words_to_ass(_words(), style_name="hype")
    assert ",1120)" in default


def test_unknown_animated_style_rejected():
    import pytest

    with pytest.raises(ValueError):
        words_to_ass(_words(), style_name="comic-sans")
