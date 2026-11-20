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
