"""Word-by-word ASS captions for ClipForge.

Word timestamps from the transcript become an ASS subtitle file that
FFmpeg burns in with libass. Three styles (report §2 stage 6):

* ``bold-pop`` (Hormozi-style): big bold white text, yellow highlight
  on the current word, black outline.
* ``karaoke-fill``: words fill with colour as they are spoken
  (ASS ``\\k`` karaoke timing).
* ``boxed``: white text on a semi-opaque black box (ASS BorderStyle 3).

Animated styles (v0.10.0, Facecam Studio) — word-by-word pop
animations in the style of the reference implementation's caption
styles, re-implemented here on ClipForge's ASS builder:

* ``bounce`` — white words, cyan pop on the active word.
* ``hormozi`` — white words, cyan pop, 2 words per group.
* ``mrbeast`` — yellow words, orange pop on the active word.
* ``karaoke`` — colour-wipe (``\\kf``) as each word is spoken.
* ``hype`` — white words, red pop, gold strong words.
* ``clean`` — smaller, sentence-case, light-blue pop.

Colour note: ASS colours are &HAABBGGRR. The reference writes its
colour strings as RRGGBB into that field, which swaps the channels
(its "cyan" renders yellow, its "orange" renders blue). The values
below are written as true BBGGRR, so the colours on screen match
their names.

Word widths are estimated (``len * fontsize * 0.62``) — ClipForge has
no font-metrics dependency — so per-word ``\\pos`` placement is
centred as a group with estimated widths. Good enough for centred
short lines; the group never exceeds the safe width.

Meta safe zones (report §4): keep text out of the top 14%, bottom
35% and 6% sides of the 1080x1920 frame. Captions sit in the middle
band — vertical centre ~55–62% of the height — with 6% side margins.
``caption_y`` (v0.10.0) lets a facecam layout move the line, e.g. onto
the gameplay half of a split-screen clip.
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
    # Animated styles (v0.10.0): the header row carries the same
    # colours the event builder uses; see ANIMATED_STYLE_DEFS.
    "bounce": CaptionStyle(
        name="Bounce",
        fontname="DejaVu Sans",
        fontsize=96,
        primary_colour=WHITE,
        secondary_colour="&H00FFF000",  # cyan (BBGGRR)
        outline_colour=BLACK,
        back_colour=SEMI_BLACK,
        bold=-1,
        border_style=1,
        outline=7,
        shadow=0,
    ),
    "hormozi": CaptionStyle(
        name="Hormozi",
        fontname="DejaVu Sans",
        fontsize=96,
        primary_colour=WHITE,
        secondary_colour="&H00FFFF00",  # cyan (BBGGRR)
        outline_colour=BLACK,
        back_colour=SEMI_BLACK,
        bold=-1,
        border_style=1,
        outline=8,
        shadow=0,
    ),
    "mrbeast": CaptionStyle(
        name="MrBeast",
        fontname="DejaVu Sans",
        fontsize=100,
        primary_colour="&H0000FFFF",  # yellow (BBGGRR)
        secondary_colour="&H000066FF",  # orange (BBGGRR)
        outline_colour=BLACK,
        back_colour=SEMI_BLACK,
        bold=-1,
        border_style=1,
        outline=8,
        shadow=0,
    ),
    "karaoke": CaptionStyle(
        name="Karaoke",
        fontname="DejaVu Sans",
        fontsize=92,
        primary_colour=WHITE,
        secondary_colour=WHITE,
        outline_colour=BLACK,
        back_colour=SEMI_BLACK,
        bold=-1,
        border_style=1,
        outline=7,
        shadow=0,
    ),
    "hype": CaptionStyle(
        name="Hype",
        fontname="DejaVu Sans",
        fontsize=100,
        primary_colour=WHITE,
        secondary_colour="&H003C3CFF",  # red (BBGGRR)
        outline_colour=BLACK,
        back_colour=SEMI_BLACK,
        bold=-1,
        border_style=1,
        outline=7,
        shadow=0,
    ),
    "clean": CaptionStyle(
        name="Clean",
        fontname="DejaVu Sans",
        fontsize=76,
        primary_colour=WHITE,
        secondary_colour="&H00FFD76B",  # light blue (BBGGRR)
        outline_colour=BLACK,
        back_colour=SEMI_BLACK,
        bold=-1,
        border_style=1,
        outline=5,
        shadow=0,
    ),
}

# Animated-style behaviour (v0.10.0). Colours are ASS &H00BBGGRR.
# ``per`` = words per caption group; ``pop`` = active-word scale %;
# ``anim`` = "bounce" (scale pop) or "karaoke" (\\kf colour wipe).
ANIMATED_STYLE_DEFS: dict[str, dict] = {
    "bounce": {
        "label": "Bounce",
        "base": "&H00FFFFFF",
        "active": "&H00FFF000",
        "strong": "&H00FFFFFF",
        "pop": 112,
        "per": 3,
        "upper": True,
        "anim": "bounce",
    },
    "hormozi": {
        "label": "Hormozi (Cyan Pop)",
        "base": "&H00FFFFFF",
        "active": "&H00FFFF00",
        "strong": "&H00FFFF00",
        "pop": 122,
        "per": 2,
        "upper": True,
        "anim": "bounce",
    },
    "mrbeast": {
        "label": "MrBeast (Yellow/Orange)",
        "base": "&H0000FFFF",
        "active": "&H000066FF",
        "strong": "&H000066FF",
        "pop": 124,
        "per": 2,
        "upper": True,
        "anim": "bounce",
    },
    "karaoke": {
        "label": "Karaoke (Color Wipe)",
        "base": "&H00FFFFFF",
        "active": "&H0066FF00",  # green (BBGGRR)
        "strong": "&H0066FF00",
        "pop": 100,
        "per": 3,
        "upper": True,
        "anim": "karaoke",
    },
    "hype": {
        "label": "Hype",
        "base": "&H00FFFFFF",
        "active": "&H003C3CFF",
        "strong": "&H0000D7FF",  # gold (BBGGRR)
        "pop": 114,
        "per": 2,
        "upper": True,
        "anim": "bounce",
    },
    "clean": {
        "label": "Clean",
        "base": "&H00FFFFFF",
        "active": "&H00FFD76B",
        "strong": "&H00FFFFFF",
        "pop": 106,
        "per": 4,
        "upper": False,
        "anim": "bounce",
    },
}

ANIMATED_STYLES = frozenset(ANIMATED_STYLE_DEFS)
CAPTION_MAX_W = 960  # animated captions never run wider than this

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


def _estimate_width(text: str, fontsize: int) -> float:
    """Estimated pixel width of ``text`` at ``fontsize``.

    No font-metrics dependency in ClipForge, so this is the standard
    ``len * size * 0.62`` estimate. It is consistent across words, so
    a group stays centred; individual word centres can be a few pixels
    off for unusual letter mixes.
    """
    return len(text) * fontsize * 0.62


def _animated_groups(words: list[Word], per: int) -> list[list[Word]]:
    """Group words into short caption lines.

    A group breaks at ``per`` words, at a >0.45 s gap, at a sentence
    end, or past ~16 characters — the same grouping the reference
    implementation uses, so one spoken burst stays on one line.
    """
    out: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (
            len(cur) >= per
            or sum(len(x.text) for x in cur) + len(w.text) > 16
            or w.start - cur[-1].end > 0.45
            or cur[-1].text[-1:] in ".?!"
        ):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def _animated_events(
    words: list[Word],
    style_name: str,
    start_offset_s: float,
    caption_y: int,
) -> list[str]:
    """ASS Dialogue lines for an animated style (v0.10.0).

    Each group is laid out by hand: every word gets its own ``\\pos``
    (``\\an5``, so the position is the word centre) and three phases —
    before it is spoken (base colour), while it is spoken (active
    colour + scale pop via ``\\t``), after it is spoken (base colour,
    settled scale). The karaoke style instead emits one event per
    group with ``\\kf`` durations, so the colour wipes across each
    word as it is spoken.
    """
    style = STYLES[style_name]
    spec = ANIMATED_STYLE_DEFS[style_name]
    size = style.fontsize
    lines: list[str] = []
    groups = _animated_groups(words, spec["per"])
    for gi, group in enumerate(groups):
        g_start = max(0.0, group[0].start - start_offset_s)
        g_end = max(g_start + 0.02, group[-1].end - start_offset_s)
        if gi + 1 < len(groups):
            g_end = min(g_end, max(g_start + 0.02, groups[gi + 1][0].start - start_offset_s))
        texts = [
            (w.text.upper() if spec["upper"] else w.text) for w in group
        ]
        widths = [_estimate_width(t, size) for t in texts]
        gap = size * 0.35
        total = sum(widths) + gap * (len(texts) - 1)
        if total > CAPTION_MAX_W:
            size_g = max(36, int(size * CAPTION_MAX_W / total))
            widths = [_estimate_width(t, size_g) for t in texts]
            gap = size_g * 0.35
            total = sum(widths) + gap * (len(texts) - 1)
        else:
            size_g = size
        if spec["anim"] == "karaoke":
            parts = []
            for w in group:
                dur_cs = max(1, int(round((w.end - w.start) * 100)))
                shown = w.text.upper() if spec["upper"] else w.text
                parts.append(f"{{\\kf{dur_cs}}}{_escape_ass(shown)}")
            text = " ".join(parts)
            lines.append(
                f"Dialogue: 0,{_ass_time(g_start)},{_ass_time(g_end)},"
                f"{style.name},,0,0,0,,{{\\an5\\pos({PLAY_RES_X // 2},{caption_y})"
                f"\\fs{size_g}\\c{spec['active']}}}{text}"
            )
            continue
        x = (PLAY_RES_X - total) / 2
        for i, (w, t) in enumerate(zip(group, texts)):
            wd = widths[i]
            cx = x + wd / 2
            x += wd + gap
            pop = max(104, min(spec["pop"], 100 + int(2200 / max(wd, 1))))
            pos = f"\\an5\\pos({cx:.0f},{caption_y})\\fs{size_g}"
            a = max(g_start, w.start - start_offset_s)
            b = min(g_end, group[i + 1].start - start_offset_s if i + 1 < len(group) else g_end)
            esc = _escape_ass(t)
            if a - g_start >= 0.02:
                lines.append(
                    f"Dialogue: 1,{_ass_time(g_start)},{_ass_time(a)},"
                    f"{style.name},,0,0,0,,{{{pos}\\c{spec['base']}}}{esc}"
                )
            if b - a >= 0.02:
                lines.append(
                    f"Dialogue: 2,{_ass_time(a)},{_ass_time(b)},"
                    f"{style.name},,0,0,0,,{{{pos}\\c{spec['active']}\\fscx92\\fscy92"
                    f"\\t(0,60,\\fscx{pop}\\fscy{pop})"
                    f"\\t(60,160,\\fscx{pop - 4}\\fscy{pop - 4})}}{esc}"
                )
            if g_end - b >= 0.02:
                lines.append(
                    f"Dialogue: 1,{_ass_time(b)},{_ass_time(g_end)},"
                    f"{style.name},,0,0,0,,{{{pos}\\c{spec['base']}"
                    f"\\fscx{pop - 4}\\fscy{pop - 4}\\t(0,80,\\fscx100\\fscy100)}}{esc}"
                )
    return lines


def words_to_ass(
    words: list[Word],
    style_name: str = DEFAULT_STYLE,
    start_offset_s: float = 0.0,
    caption_y: int | None = None,
) -> str:
    """Render word timestamps as ASS text.

    ``start_offset_s`` shifts all times (a clip starting at 40 s in the
    source renders captions from 0 s). Pure function — no I/O.
    ``caption_y`` moves the caption line (facecam layouts); None keeps
    the default safe-band line.
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
    y = caption_y or CAPTION_Y
    if style_name in ANIMATED_STYLES:
        lines.extend(
            _animated_events(words, style_name, start_offset_s, caption_y or CAPTION_Y)
        )
        return "\n".join(lines) + "\n"
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
                f"{style.name},,0,0,0,,{{\\pos({PLAY_RES_X // 2},{y})}}{text}"
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
                    f"{{\\pos({PLAY_RES_X // 2},{y})}}{text}"
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
    caption_y: int | None = None,
) -> Path:
    """Write the ASS file for ``words``; returns the path."""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        words_to_ass(
            words,
            style_name=style_name,
            start_offset_s=start_offset_s,
            caption_y=caption_y,
        ),
        encoding="utf-8",
    )
    return path


def clip_words(words: list[Word], start_s: float, end_s: float) -> list[Word]:
    """Words overlapping [start_s, end_s], times kept in source space."""
    return [w for w in words if w.end > start_s and w.start < end_s]
