# Missing-tool audit (v0.8.1)

Audit of everything the ClipForge pipeline can need on a fresh Windows
machine, done after the Agent run blocked at Ingest with
`yt_dlp_missing`. The rule: a missing tool must either install itself
with one click inside the app, or fail with a plain-language
explanation plus a one-click guided path — never a bare error that
leaves the user to guess.

| Tool | Used by | On a fresh machine | Verdict |
| --- | --- | --- | --- |
| yt-dlp | Ingest (YouTube download), Agent channel fetch + pre-flight, live capture, trending channel match | Missing — the app only errored (`yt_dlp_missing`, job blocked at Ingest) | **One-click install added (v0.8.1)** — `GET /yt-dlp/status` + `POST /yt-dlp/install`, official release binary, SHA-256 verified against the release's `SHA2-256SUMS`, into `<data_dir>/yt-dlp/`, `yt-dlp --version` verified, Windows user PATH updated. Install button sits on the blocked Ingest stage (Pipeline), the Agent page, and Settings. |
| ffmpeg | Render, transcribe audio extraction, thumbnails | Missing until installed | **Already one-click (v0.4.1)** — bundled into `<data_dir>/ffmpeg/bin/`, used first with no restart. Verified still in place; worker blocked messages now point at the in-app Install button instead of only the download page. |
| ffprobe | Ingest validation, render verification, live capture validation | Ships with FFmpeg | **Bundled with FFmpeg** — same installer, same `bin` dir, same PATH-first resolution. No separate install needed. |
| faster-whisper (Python package) | Local transcription | Not installed (optional dep) | **Fixed in v0.10.2 — system-Python detection** — it is a pip package (plus a ctranslate2 native stack), not a single binary, so no one-click install inside the app and it is deliberately NOT bundled into the frozen `clipforge-server.exe` (native DLLs, AVX2, size). The v0.10.1 bug: the server only probed `import faster_whisper` *inside the frozen exe*, which can never see the system Python's `site-packages` — so a user who ran `python -m pip install faster-whisper` in PowerShell, restarted, and pressed Verify still saw it missing. Since v0.10.2 the server probes the Pythons on PATH / the `py` launcher and transcribes in the detected Python as a subprocess (`clipforge.whisper_system`): install with `python -m pip install faster-whisper`, press Re-check — no restart needed — and the panel shows the real version + which Python it was found in. A package that exists but fails to import (missing DLL, broken dep) is reported `broken`, never "Installed". Alternatives unchanged: `OPENAI_API_KEY` (Whisper API) or `transcript_text` for the demo fallback. |
| PyAV (`av`) vs faster-whisper | Local transcription (audio decode) | A fresh `pip install faster-whisper` pulls PyAV 19 | **Known upstream breakage (v0.10.2)** — faster-whisper 1.2.1 passes `metadata_errors=` to `av.open()`, which PyAV 19.0.0 removed, so *every* transcription crashes with `TypeError: open() got an unexpected keyword argument 'metadata_errors'` — the import itself succeeds, so a plain import check looks fine. The probe now exercises that exact call and reports such an install as `broken` with the fix: `pip install "av<19"` in the same Python, then Re-check. The `transcribe` extra in `pyproject.toml` pins `av>=11,<19` for the same reason. |
| Whisper model download | First real transcription (faster-whisper) | Downloads ~500 MB (default `small` model) from HuggingFace on first use, with no progress shown | **Shared cache since v0.10.2** — the transcription subprocess inherits the server's environment, so `HF_HUB_CACHE` / `HF_HOME` (default `%USERPROFILE%\.cache\huggingface`) resolves to the same directory in both paths: a model pre-downloaded with `python -c "from faster_whisper import WhisperModel; WhisperModel('small')"` is reused, not re-downloaded. If the model lives elsewhere, set `HF_HUB_CACHE` as a user environment variable so both Pythons see it. The download itself still happens inside faster-whisper with no progress hook to surface in the UI. The Whisper API key path skips it entirely. A progress-reporting pre-download is a follow-up, not part of this fix. |
| OpenAI Whisper API | Transcription (alternative backend) | Needs `OPENAI_API_KEY` (account key, not a tool) | **Guided path exists** — Settings → LLM key. Not installable, and not a missing-tool block: the job blocks with the key named. |
| drawtext fonts | Thumbnail title overlay | DejaVu on Linux, Arial on Windows (`C:\Windows\Fonts\arial.ttf` exists on a fresh install) | **No install needed** — when no font is found the frame is still extracted, just without the title overlay (graceful degradation, never a block). |
| libass (ASS caption burning) | Render (`ass=` filter) | Compiled into FFmpeg | **Bundled with FFmpeg** — the gyan.dev essentials and johnvansickle static builds both include libass. |
| cv2 (OpenCV) | Face-tracked 9:16 reframe | Not installed (optional dep) | **No install needed** — missing cv2 falls back to a centre crop for the whole clip (same shape, same contract). Face tracking is an enhancement, never a block. |
| nvidia-smi | Performance profile hardware detection only | Missing on machines without an NVIDIA GPU | **No action** — guarded by `shutil.which`; absence means CPU fallback. Never a pipeline block. |
| Platform accounts / API keys (YouTube, Meta, TikTok, X) | Publishing, trending search | Not connected until the user connects them | **Out of scope** — accounts, not tools. The guided connect wizard already walks the user through each one. |

## How the one-click installs resolve

Every call site resolves the tool in the same order — saved settings
path, installer-managed bundled binary, then PATH — so a bundled
install works with no restart and no PATH edit:

- FFmpeg/ffprobe: `clipforge.ffmpeg_install.resolve_ffmpeg_path` /
  `bundled_ffprobe_path`, plus `ensure_bundled_on_path` at API startup.
- yt-dlp: `clipforge.ytdlp_install.resolve_yt_dlp_path` /
  `ensure_bundled_yt_dlp_on_path` at API startup; `GET /dependencies`
  counts the bundled binary as present, and `require_tool("yt-dlp")`
  accepts it, so a job retries cleanly after the install.
