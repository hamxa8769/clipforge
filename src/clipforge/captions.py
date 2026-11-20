"""Word-by-word ASS captions for ClipForge.

Word timestamps from the transcript become an ASS subtitle file that
FFmpeg burns in with libass. Three styles (report §2 stage 6):

* ``bold-pop`` (Hormozi-style): big bold white text, yellow highlight
  on the current word, black outline.
* ``karaoke-fill``: words fill with colour as they are spoken
  (ASS ``\\k`` karaoke timing).
* ``boxed``: white text on a semi-opaque black box (ASS BorderStyle 3).

Meta safe zones (report §4): keep text out of the top 14%, bottom
35% and 6% sides of the 1080x1920 frame. Captions sit in the middle
band — vertical centre ~55–62% of the height — with 6% side margins.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from clipforge.transcribe import Word

# 1080x1920 canvas, Meta safe zones.
PLAY_RES_X = 1080
PLAY_RES_Y = 1920
SAFE_TOP = int(PLAY_RES_Y * 0.14)  # 268 px
SAFE_BOTTOM = int(PLAY_RES_Y * 0.35)  # 672 px from the bottom
SAFE_SIDE = int(PLAY_RES_X * 0.06)  # 64 px
CAPTION_Y = 1120  # inside the safe band (268 .. 1248), lower-middle

# ASS colour format is &HAABBGGRR (AA = alpha, 00 opaque).
WHITE = "&H00FFFFFF"
YELLOW = "&H0000D7FF"  # BGR for #FFD700-ish yellow
BLACK = "&H00000000"
SEMI_BLACK = "&H80000000"


@dataclass(frozen=True)
class CaptionStyle:
    """One ASS style definition."""

    name: str
    fontname: str
    fontsize: int
    primary_colour: str
    secondary_colour: str
    outline_colour: str
    back_colour: str
    bold: int  # -1 on, 0 off
    border_style: int  # 1 = outline+shadow, 3 = opaque box
    outline: int
    shadow: int
    alignment: int = 8  # 8 = top-centre; we position with \pos


STYLES: dict[str, CaptionStyle] = {
    "bold-pop": CaptionStyle(
        name="BoldPop",
        fontname="DejaVu Sans",
        fontsize=72,
        primary_colour=WHITE,
        secondary_colour=YELLOW,
        outline_colour=BLACK,
        back_colour=SEMI_BLACK,
        bold=-1,
        border_style=1,
        outline=4,
        shadow=2,
    ),
    "karaoke-fill": CaptionStyle(
        name="Karaoke",
        fontname="DejaVu Sans",
        fontsize=72,
        primary_colour=WHITE,
        secondary_colour=YELLOW,
        outline_colour=BLACK,
        back_colour=SEMI_BLACK,
        bold=-1,
        border_style=1,
        outline=4,
        shadow=2,
    ),
    "boxed": CaptionStyle(
        name="Boxed",
        fontname="DejaVu Sans",
        fontsize=64,
        primary_colour=WHITE,
        secondary_colour=YELLOW,
        outline_colour=BLACK,
        back_colour="&HCC000000",
        bold=-1,
        border_style=3,
        outline=2,
        shadow=0,
    ),
}

DEFAULT_STYLE = "bold-pop"
WORDS_PER_LINE = 3  # word-by-word highlight, 3 words per caption event


def _ass_time(seconds: float) -> str:
    """Seconds -> ASS H:MM:SS.cc (centiseconds)."""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _style_line(style: CaptionStyle) -> str:
    return (
        f"Style: {style.name},{style.fontname},{style.fontsize},"
        f"{style.primary_colour},{style.secondary_colour},"
        f"{style.outline_colour},{style.back_colour},"
        f"{style.bold},0,0,0,100,100,0,0,{style.border_style},"
        f"{style.outline},{style.shadow},8,10,10,10,1"
    )


def words_to_ass(
    words: list[Word],
    style_name: str = DEFAULT_STYLE,
    start_offset_s: float = 0.0,
) -> str:
    """Render word timestamps as ASS text.

    ``start_offset_s`` shifts all times (a clip starting at 40 s in the
    source renders captions from 0 s). Pure function — no I/O.
    """
    if style_name not in STYLES:
        raise ValueError(f"unknown caption style {style_name!r}; choose from {sorted(STYLES)}")
    style = STYLES[style_name]
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {PLAY_RES_X}\n"
        f"PlayResY: {PLAY_RES_Y}\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 0\n"
        "\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{_style_line(style)}\n"
        "\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    lines = [header]
    # Group words into short lines; each event highlights its words in
    # turn (karaoke-style for karaoke-fill, colour-swap for the others).
    for group_start in range(0, len(words), WORDS_PER_LINE):
        group = words[group_start : group_start + WORDS_PER_LINE]
        if not group:
            continue
        event_start = group[0].start - start_offset_s
        event_end = group[-1].end - start_offset_s
        if event_end <= 0:
            continue
        event_start = max(0.0, event_start)
        if style_name == "karaoke-fill":
            # One event, \k durations in centiseconds per word.
            parts = []
            for w in group:
                dur_cs = max(1, int(round((w.end - w.start) * 100)))
                parts.append(f"{{\\k{dur_cs}}}{_escape_ass(w.text)}")
            text = " ".join(parts)
            lines.append(
                f"Dialogue: 0,{_ass_time(event_start)},{_ass_time(event_end)},"
                f"{style.name},,0,0,0,,{{\\pos({PLAY_RES_X // 2},{CAPTION_Y})}}{text}"
            )
        else:
            # One event per word, current word in the highlight colour.
            for i, w in enumerate(group):
                parts = []
                for j, other in enumerate(group):
                    esc = _escape_ass(other.text)
                    if j == i:
                        parts.append(
                            f"{{\\1c{style.secondary_colour}}}{esc}{{\\1c{style.primary_colour}}}"
                        )
                    else:
                        parts.append(esc)
                text = " ".join(parts)
                lines.append(
                    f"Dialogue: 0,{_ass_time(max(0.0, w.start - start_offset_s))},"
                    f"{_ass_time(max(0.0, w.end - start_offset_s))},{style.name},,0,0,0,,"
                    f"{{\\pos({PLAY_RES_X // 2},{CAPTION_Y})}}{text}"
                )
    # One line per event: the header already ends with a newline, and
    # every Dialogue line needs its own. (A "".join here once glued all
    # events into a single line, so libass parsed only the first event
    # and the rest rendered as literal "Dialogue: ..." caption text.)
    return "\n".join(lines) + "\n"


def write_ass(
    words: list[Word],
    out_path: str | Path,
    style_name: str = DEFAULT_STYLE,
    start_offset_s: float = 0.0,
) -> Path:
    """Write the ASS file for ``words``; returns the path."""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        words_to_ass(words, style_name=style_name, start_offset_s=start_offset_s),
        encoding="utf-8",
    )
    return path


def clip_words(words: list[Word], start_s: float, end_s: float) -> list[Word]:
    """Words overlapping [start_s, end_s], times kept in source space."""
    return [w for w in words if w.end > start_s and w.start < end_s]
