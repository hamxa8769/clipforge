# Live stream monitoring (v0.10.0)

Watch a live stream for peak moments and send each peak to the normal
clip pipeline. The monitor never invents a stream or a peak: a URL
that is not live fails plainly, and a peak only fires when the
stream measurably jumps.

## What it does

1. **Ingest** — the stream's live edge is resolved with yt-dlp and
   recorded with one ffmpeg process: 6-second `.ts` buffer segments
   (default 30 minutes kept) plus a 16 kHz mono audio pipe for live
   analysis.
2. **Peak detection** (`clipforge.live_monitor.PeakDetector`) — a
   rolling baseline of loudness and chat over the last 3 minutes
   (excluding the last 10 seconds). A peak fires when the last 5
   seconds of audio are >= 60% louder than the baseline, or chat
   runs >= 60% busier (and >= 1.5x) over the last 8 seconds. Chat
   is counted as **people**, not messages: ten people saying "GG"
   is a reaction, one person saying it ten times is a bot. At most
   one peak per 60 seconds. A rolling transcript (faster-whisper,
   7-second chunks) adds keyword triggers: someone saying "clip
   it" / "clip that" / "clip this", or one of your own trigger
   phrases. A peak's clip window is 12 seconds before the trigger
   and 28 seconds after it.
3. **Peak → job** — the peak is cut from the buffer and becomes a
   normal ClipForge job (`source_type="local"`, the cut file as
   `source_path`, `meta.live_peak=true`) in the existing queue, so
   the normal pipeline (transcribe → score → clip → SEO → approval
   queue) runs on it. A notification is emitted per peak job. The
   "Clip last 30s" button does the same thing manually.

## Platforms

| Platform | Audio | Chat | Notes |
| --- | --- | --- | --- |
| YouTube | yes, no key | OAuth only | An API key alone cannot read live chat — without a connected channel the monitor runs audio-only and says so. |
| Twitch | yes, no key | yes, no key | Anonymous IRC (`justinfan`). |
| Kick | yes, no key | no | Kick has no public chat API without auth — audio-only. |

Liveness can also be checked with the YouTube Data API
(`liveBroadcasts.list` / `search.list eventType=live`, see
`clipforge.live`) when credentials exist.

## API

| Endpoint | What it does |
| --- | --- |
| `GET /api/live/platforms` | Per-platform capabilities, honestly stated. |
| `GET /api/live/status` | Pre-flight: ffmpeg, yt-dlp (blocking), rolling transcription (degraded without faster-whisper / OpenAI key — peaks still fire from audio + chat), YouTube chat availability. |
| `POST /api/live/monitor` | Start monitoring `{url, max_hours, buffer_min, trigger_words}` or replay `{replay_path}` → 202 `{session_id, mode}`. |
| `GET /api/live/monitor` | All sessions, newest first (finished ones stay visible). |
| `GET /api/live/monitor/{id}` | One session: elapsed, audio levels, baseline, chat, transcript tail, peaks with job ids. |
| `POST /api/live/monitor/{id}/stop` | Stop a monitor. Peaks already cut keep their jobs. |
| `POST /api/live/monitor/{id}/clip-now` | Clip the last N seconds (5–120s) now. |

At most 2 monitors run at once. A monitor stops itself after
`max_hours` (default 2, cap 8).

## Replay mode

When no stream is live, a recorded file can be replayed at
real-time speed through the same detector (`mode="replay"`). The
session record always says which mode ran — a replay is never
reported as a live stream. This is also how the detector is
verified without a live stream (see `tests/unit/test_live_monitor.py`).

## Honest limits

- yt-dlp records from the live edge, not from the stream's start —
  a moment that already passed cannot be captured.
- Without faster-whisper (or an OpenAI key) there is no rolling
  transcript: peaks still fire from audio + chat, keyword triggers
  do not, and the session says transcription is unavailable.
- A peak is only cut when its window is still in the rolling
  buffer; a peak whose window has scrolled out is recorded as
  missed, never faked.
- DRM / age-restricted (unsigned-in) / geo-blocked streams cannot
  be recorded at all.
- The detector needs a minute of audio before it can judge
  anything (warm-up).

## UI

Sidebar → **Live**: pre-flight + platform cards, a URL + trigger
phrase form, the active session (audio-level strip, baseline,
chat, transcript tail, peaks with job links, "Clip last 30s" /
"Stop"), and the session list. A replay session carries an amber
"this is a replay, not a live stream" banner.

## Reference

Studied, not copied: ClipNova-AI `studio/live.py` (live recording,
spike detection, chat velocity, rolling speech-to-text, "clip it"
triggers), `studio/asr_worker.py` (remote speech-to-text service),
`studio/audio.py` (audio track management — not needed here:
ClipForge monitors the stream's own audio, it does not mix tracks).
ClipForge's implementation lives in `src/clipforge/live_monitor.py`
and reuses ClipForge's own `ingest`, `transcribe`, `job_queue`,
`notifications`, and `requirements` modules.
