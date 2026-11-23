# CapCut Export (v0.10.0)

Export a finished clip as a native CapCut Desktop project folder, so it
can be finished in CapCut with its filters, transitions, AI effects,
stickers and sound effects — instead of only as a flat mp4.

## How it works

`POST /clips/{candidate_id}/capcut` (see `docs/api.md`) builds a draft
folder with ClipForge's own minimal draft writer
(`src/clipforge/capcut.py`):

| File | What it is |
|---|---|
| `draft_content.json` | The project: 1080×1920 canvas, 30 fps, tracks, materials. Mirrored to `draft_content.json.bak` and `template-2.tmp`, plus a legacy `draft_info.json` copy for older CapCut builds. |
| `draft_meta_info.json` | Name, duration, create/modify times, folder paths. |
| `draft_settings` | Small INI file with the draft timings. |

Tracks in the draft:

1. **ClipForge Video** — the rendered 9:16 mp4, full clip, from 0s.
   The mp4 is linked by absolute path, **not copied** — the draft stays
   small, but the clip file must stay where it is.
2. **Hook Title** — the clip's hook line, first 3.5 seconds, bold white
   text with a black stroke.
3. **Captions** — the transcript words in 3-word chunks, timed to the
   clip (words outside the clip are dropped).

## Where the folder goes

- When CapCut Desktop is installed on the same machine, the draft goes
  straight into CapCut's own drafts folder
  (`%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft`) and
  shows up in CapCut's project list with no import step.
- Otherwise it goes to `<data_dir>/capcut_drafts/<DraftName>/` — open it
  in CapCut with File → Open project (or move the folder into CapCut's
  drafts folder).

The endpoint and the UI both report the folder path in plain language
(`CapCut draft ready: <name> (...). The project folder is <path>, ...`).
Re-exporting replaces the previous draft of the same name.

## Draft format — what was studied, what was written

The writer was built by studying how CapCut drafts are structured: the
public `pyJianYingDraft` library (MIT, as used by the ClipNova-AI
reference project) and a real CapCut draft folder
(`draft_content.json` keys, material/segment/track shapes, format
version `360000`, the `draft_meta_info.json` fields). No code or files
were copied — the writer is ClipForge's own implementation for
ClipForge's data model, and it only writes the three tracks above (no
effects, transitions, stickers or audio tracks yet).

## Limits — read this before calling it done

- **Structurally validated, not opened in CapCut.** The writer is
  verified by `clipforge.capcut.validate_draft` and
  `tests/unit/test_capcut.py` (JSON parses, tracks reference materials
  that exist, caption segments do not overlap, every media path
  resolves). CapCut Desktop only runs on Windows/macOS, so opening the
  draft in the real app still needs a check on the user's PC.
- Text lands centred on the canvas (the same as the reference
  exporter's output). Drag it where you want it in CapCut.
- The draft references the mp4 by absolute path. If the clip file is
  moved or deleted after export, CapCut shows the media as missing —
  export again.
- No thumbnail/cover image is written yet; CapCut generates its own
  cover from the first frame.
