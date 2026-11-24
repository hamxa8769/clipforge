"""Live stream monitoring (v0.10.0) — watch a live stream, spot the peaks, clip them.

How it works (re-implemented for ClipForge's architecture from the
ideas in ClipNova-AI's ``studio/live.py`` — studied, not copied):

1. **Ingest** — the stream's live edge is resolved with yt-dlp and
   recorded with one ffmpeg process that does two things at once:
   6-second ``.ts`` buffer segments (so a peak can be cut after the
   fact) plus a 16 kHz mono audio pipe for live analysis. This is the
   same shape as ClipNova's recorder, rebuilt on ClipForge's own
   ``capture_live_stream`` / ``ingest`` vocabulary.
2. **Peak detection** — :class:`PeakDetector` keeps a rolling baseline
   of loudness and chat over the last three minutes (leaving out the
   last few seconds) and flags a moment when the last five seconds
   jump at least 60% above it — loudness, chat velocity, or both.
   Chat is counted as *people*, not messages (ten people saying "GG"
   is a reaction; one person saying it ten times is a bot). A rolling
   transcript (faster-whisper, 7-second chunks, when available) adds
   keyword triggers: someone saying "clip it" / "clip that", or one
   of the user's own trigger phrases, marks the same kind of peak.
   A peak's clip window is the golden-retention shape ClipNova uses:
   12 seconds before the trigger, 28 seconds after it.
3. **Peak -> job** — a detected peak is cut from the buffer (or, in
   replay mode, trimmed from the source file) and becomes a normal
   ClipForge job (``source_type="local"``, the cut file as
   ``source_path``) in the EXISTING queue, so the normal pipeline
   (transcribe -> score -> clip -> SEO -> approval queue) runs on it.
   Nothing about the pipeline is duplicated; the monitor only feeds
   it. A notification is emitted per peak job.

Platforms, honestly:

* **YouTube (first)** — liveness + stream URL via yt-dlp (no key
  needed); live chat needs the YouTube Data API with OAuth
  (``liveChatMessages.list``) — an API key alone cannot read chat,
  so without a connected channel the monitor runs audio-only and
  says so. Liveness can also be checked with the Data API
  (``liveBroadcasts.list`` / ``search.list eventType=live``) when
  credentials exist.
* **Twitch** — stream URL via yt-dlp (no key); chat over anonymous
  IRC (``justinfan``), no key needed, exactly as ClipNova does.
* **Kick** — stream URL via yt-dlp (no key); Kick has no public chat
  API that works without auth, so Kick monitoring is audio-only and
  the UI says so.

**Replay mode** — when no live stream is available, a recorded file
can be fed through the same detector at real-time speed
(``mode="replay"``). The session record always says which mode ran,
and a replay is never reported as a live stream. This is also how
the peak detector is verified without a live stream.

Honest limits (also surfaced by ``GET /api/live/status``):

* yt-dlp records from the live edge, not from the stream's start —
  a moment that already passed cannot be captured.
* Without faster-whisper (or an OpenAI key) there is no rolling
  transcript: peaks still fire from audio + chat, keyword triggers
  do not, and the session says transcription is unavailable.
* A peak is only cut when its window is still in the rolling buffer
  (default 30 minutes); a peak whose window has already scrolled
  out is recorded as missed, never faked.
* DRM / age-restricted (unsigned-in) / geo-blocked streams cannot
  be recorded at all — yt-dlp refuses them and the session fails
  with the plain-language reason.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import uuid
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from clipforge.config import settings

# ------------------------------------------------------------------ limits
SEG_SEC = 6  # length of each buffer piece (ffmpeg segment muxer)
STALL_SEC = 90  # audio arriving but no picture for this long: restart ffmpeg
PAD_S = 2  # extra seconds kept around a peak window when cutting
BASELINE_WINDOW_S = 180  # trailing seconds the baseline is measured over
BASELINE_EXCLUDE_S = 10  # last seconds left out of the baseline
CURRENT_AUDIO_S = 5  # seconds averaged for the "now" loudness
CURRENT_CHAT_S = 8  # chat is bursty: a slightly longer "now" window
JUMP = 0.6  # 60% louder / busier than the baseline counts as a spike
MIN_BASELINE_S = 60  # warm-up: no peak before a minute of audio
PEAK_COOLDOWN_S = 60  # least time between two peaks
PRE_ROLL_S = 12.0  # seconds kept before a trigger (golden retention)
POST_ROLL_S = 28.0  # seconds kept after a trigger
BUFFER_MIN_DEFAULT = 30  # minutes of buffer kept on disk
MAX_SESSIONS = 2  # at most this many monitors at once
MAX_HOURS_DEFAULT = 2.0
MAX_HOURS_CAP = 8.0
TRANSCRIBE_CHUNK_S = 7.0  # rolling transcription chunk length
KEYWORD_WAIT_S = 15.0  # transcript must cover trigger + 15s before a keyword peak fires
PEAK_RENDER_WAIT_S = 60.0  # longest to wait for the buffer to cover a peak window
SR = 16000  # audio sample rate (Hz, mono, s16le)

# Phrases that mark a moment by themselves (ClipNova's built-in
# "clip" trigger, same idea, ClipForge wording). Custom trigger
# phrases from the session settings are matched the same way.
CLIP_PHRASES = (
    "clip it",
    "clip that",
    "clip this",
    "clip karo",
    "ye clip karo",
)
_CLIP_RE = re.compile(r"\bclip(\s+(it|that|this|karo))?\b", re.IGNORECASE)

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


class LiveMonitorError(ValueError):
    """Plain-language live-monitor failure — safe to show as-is."""


class NotLiveError(LiveMonitorError):
    """The stream is not live right now (or cannot be resolved)."""


# ------------------------------------------------------------------ platforms
def detect_platform(url: str) -> str | None:
    """``"youtube"`` / ``"twitch"`` / ``"kick"`` for a stream URL, else None."""
    text = (url or "").strip().lower()
    if "youtube.com" in text or "youtu.be" in text:
        return "youtube"
    if "twitch.tv" in text:
        return "twitch"
    if "kick.com" in text:
        return "kick"
    return None


def platform_capabilities(platform: str) -> dict:
    """What monitoring can do on each platform, and what it needs.

    Honest by design: chat on YouTube needs a connected channel
    (OAuth); Kick chat needs auth ClipForge does not have, so Kick
    is audio-only. No entry ever claims a capability that needs a
    key the user has not provided.
    """
    return {
        "youtube": {
            "platform": "youtube",
            "audio": True,
            "audio_note": "Live-edge audio via yt-dlp + ffmpeg. No key needed.",
            "chat": "oauth",
            "chat_note": (
                "Live chat needs your YouTube channel connected (OAuth) — "
                "an API key alone cannot read live chat. Without it the "
                "monitor runs audio-only."
            ),
            "transcript": True,
            "transcript_note": "Rolling transcription needs faster-whisper or an OpenAI key.",
        },
        "twitch": {
            "platform": "twitch",
            "audio": True,
            "audio_note": "Live-edge audio via yt-dlp + ffmpeg. No key needed.",
            "chat": True,
            "chat_note": "Chat over anonymous Twitch IRC (justinfan). No key needed.",
            "transcript": True,
            "transcript_note": "Rolling transcription needs faster-whisper or an OpenAI key.",
        },
        "kick": {
            "platform": "kick",
            "audio": True,
            "audio_note": "Live-edge audio via yt-dlp + ffmpeg. No key needed.",
            "chat": False,
            "chat_note": (
                "Kick has no public chat API that works without auth, so "
                "Kick monitoring is audio-only."
            ),
            "transcript": True,
            "transcript_note": "Rolling transcription needs faster-whisper or an OpenAI key.",
        },
    }.get(
        platform,
        {
            "platform": platform,
            "audio": False,
            "audio_note": "Unsupported platform — use a YouTube, Twitch or Kick live URL.",
            "chat": False,
            "chat_note": "Unsupported platform.",
            "transcript": False,
            "transcript_note": "Unsupported platform.",
        },
    )


# ------------------------------------------------------------------ stream resolving
def resolve_stream(url: str, quality: int = 720) -> dict:
    """Ask yt-dlp for a live stream's playable address.

    Returns ``{"urls", "title", "uploader", "video_id"}``. Raises
    :class:`NotLiveError` when the channel is offline or the URL
    cannot be resolved — never an invented stream.
    """
    import yt_dlp  # noqa: PLC0415 (optional at import time for tests)

    q = int(quality or 720)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": f"b[height<={q}]/bv*[height<={q}]+ba/b",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 — yt-dlp errors are plain text
        raise NotLiveError(f"Could not open this live stream: {str(exc)[-200:]}") from exc
    if not info or not info.get("is_live"):
        raise NotLiveError("This channel is not live right now.")
    fmts = info.get("requested_formats") or [info]
    urls = [f["url"] for f in fmts if isinstance(f, dict) and f.get("url")]
    if not urls:
        raise NotLiveError("Could not open the live stream — no playable format was found.")
    return {
        "urls": urls,
        "title": str(info.get("title") or ""),
        "uploader": str(info.get("uploader") or info.get("channel") or ""),
        "video_id": str(info.get("id") or ""),
    }


def youtube_live_chat_id(video_id: str, *, credentials, client: httpx.Client | None = None) -> str:
    """The ``activeLiveChatId`` for a live video ("" when there is none).

    ``videos.list`` ``liveStreamingDetails`` — needs any YouTube Data
    API credential (key or OAuth). The chat *messages* themselves
    need OAuth, so this only answers "is there a chat to read".
    """
    from clipforge.trending import _safe_json  # noqa: PLC0415 (avoid cycle)

    own_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        resp = http.get(
            f"{YOUTUBE_API_BASE}/videos",
            params={"part": "liveStreamingDetails", "id": video_id},
            headers=credentials.auth_headers(),
        )
        if resp.status_code != 200:
            return ""
        items = _safe_json(resp).get("items") or []
        details = (items[0].get("liveStreamingDetails") or {}) if items else {}
        return str(details.get("activeLiveChatId") or "")
    except httpx.HTTPError:
        return ""
    finally:
        if own_client:
            http.close()


# ------------------------------------------------------------------ peak detection
@dataclass
class Peak:
    """One detected peak moment, on the session clock (seconds)."""

    trigger_s: float
    start_s: float
    end_s: float
    reasons: list[str] = field(default_factory=list)
    score: int = 0
    kind: str = "spike"  # "spike" | "keyword" | "manual"
    transcript: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class PeakDetector:
    """Rolling peak detection over audio loudness + chat + transcript.

    Pure logic, no I/O: feed it one loudness value per second
    (:meth:`add_audio_second`), one chat event per message
    (:meth:`add_chat`), and transcribed words (:meth:`add_words`);
    :meth:`check` returns a :class:`Peak` when the last few seconds
    jump far enough above the trailing baseline, or None.

    The rule mirrors ClipNova's ``_watch_spikes``: the baseline is
    the mean amplitude over the last three minutes excluding the last
    ten seconds, and a spike is a >=60% jump in the last five seconds
    of audio, or chat running at >=1.5x (and >=60% above) its baseline
    over the last eight seconds. Chat is counted as unique people per
    second when usernames are known, else as messages per second.
    """

    def __init__(
        self,
        *,
        jump: float = JUMP,
        cooldown_s: float = PEAK_COOLDOWN_S,
        pre_roll_s: float = PRE_ROLL_S,
        post_roll_s: float = POST_ROLL_S,
        trigger_words: list[str] | None = None,
    ):
        self.jump = jump
        self.cooldown_s = cooldown_s
        self.pre_roll_s = pre_roll_s
        self.post_roll_s = post_roll_s
        self.db: list[float] = []  # loudness (dBFS) per second
        self.chat_msgs: dict[int, int] = {}  # second -> message count
        self.chat_users: dict[int, set[str]] = {}  # second -> usernames
        self.words: list[dict] = []  # {"w", "s", "e"}
        self.trigger_words = [w.strip().lower() for w in (trigger_words or []) if w.strip()]
        self.last_peak_s = -1e9
        self.peaks: list[Peak] = []
        self.keyword_triggers: list[dict] = []  # pending, waiting for transcript cover
        self._last_keyword_s = -1e9
        self.base: dict = {"warming": True}

    # ---------------------------------------------------------- feeding
    def add_audio_second(self, db_value: float) -> None:
        self.db.append(float(db_value))

    def add_chat(self, second: int, user: str | None = None) -> None:
        second = int(second)
        self.chat_msgs[second] = self.chat_msgs.get(second, 0) + 1
        if user:
            self.chat_users.setdefault(second, set()).add(user.lower())

    def add_words(self, words: list[dict]) -> None:
        """Add transcribed words (each ``{"w", "s", "e"}``) and look for triggers."""
        if not words:
            return
        self.words.extend(words)
        self._check_keyword_triggers(words)

    # ---------------------------------------------------------- baselines
    @staticmethod
    def _amp(db_value: float) -> float:
        return 10 ** (db_value / 20.0)

    def _audio_jump(self) -> tuple[float, float, float]:
        """(jump_ratio, baseline_amp, current_amp) for loudness, or (0,0,0) when warming up."""
        n = len(self.db)
        if n < MIN_BASELINE_S:
            return 0.0, 0.0, 0.0
        import statistics

        window = self.db[max(0, n - BASELINE_WINDOW_S) :]
        if len(window) > BASELINE_EXCLUDE_S:
            base = statistics.fmean(self._amp(v) for v in window[:-BASELINE_EXCLUDE_S])
        else:
            base = 0.0
        cur = statistics.fmean(self._amp(v) for v in window[-CURRENT_AUDIO_S:])
        if base <= 1e-6:
            return 0.0, base, cur
        return cur / base - 1.0, base, cur

    def _chat_series(self) -> list[float]:
        """Unique people per second (or messages/s when users are unknown)."""
        n = len(self.db)
        if self.chat_users:
            span = range(max(0, n - BASELINE_WINDOW_S), n)
            return [len(self.chat_users.get(t, ())) for t in span]
        return [float(self.chat_msgs.get(t, 0)) for t in range(max(0, n - BASELINE_WINDOW_S), n)]

    def _chat_jump(self) -> tuple[float, float, float]:
        """(jump_ratio, baseline_rate, current_rate) for chat, or (0,0,0) when no chat seen."""
        if not self.chat_msgs and not self.chat_users:
            return 0.0, 0.0, 0.0
        series = self._chat_series()
        if len(series) < MIN_BASELINE_S:
            return 0.0, 0.0, 0.0
        import statistics

        if len(series) > BASELINE_EXCLUDE_S:
            base = statistics.fmean(series[:-BASELINE_EXCLUDE_S])
        else:
            base = 0.0
        cur = statistics.fmean(series[-CURRENT_CHAT_S:])
        if base <= 0:
            # Chat started from silence: any real burst counts.
            return (1.0 if cur >= 1.5 else 0.0), base, cur
        return cur / base - 1.0, base, cur

    # ---------------------------------------------------------- checking
    def check(self, now_s: float | None = None) -> Peak | None:
        """Return a peak at ``now_s`` (default: latest audio second) or None.

        A peak fires at most once per cooldown window, and only after
        the warm-up minute. The returned window is trigger-12s ..
        trigger+28s.
        """
        n = len(self.db)
        now = float(now_s if now_s is not None else n)
        if n < MIN_BASELINE_S:
            self.base = {"warming": True, "seconds": n}
            return None
        loud_jump, loud_base, _loud_cur = self._audio_jump()
        chat_jump, chat_base, chat_now = self._chat_jump()
        self.base = {
            "warming": False,
            "loud_db": round(20 * math.log10(max(loud_base, 1e-6)), 1),
            "loud_jump_pct": round(loud_jump * 100),
            "chat_per_s": round(chat_base, 2),
            "chat_now_per_s": round(chat_now, 2),
            "jump_pct": round(self.jump * 100),
        }
        reasons: list[str] = []
        if loud_jump >= self.jump:
            reasons.append(f"loudness +{round(loud_jump * 100)}%")
        if chat_jump >= self.jump and chat_now >= max(1.5, chat_base * (1 + self.jump)):
            reasons.append(f"chat +{round(chat_jump * 100)}%")
        if not reasons:
            return None
        if now - self.last_peak_s < self.cooldown_s:
            return None
        trigger = float(n - 3)  # the jump is measured over the last seconds; centre it
        peak = Peak(
            trigger_s=trigger,
            start_s=max(0.0, trigger - self.pre_roll_s),
            end_s=trigger + self.post_roll_s,
            reasons=reasons,
            score=self._score(max(loud_jump, chat_jump)),
            kind="spike",
        )
        self.last_peak_s = now
        self.peaks.append(peak)
        return peak

    @staticmethod
    def _score(jump: float) -> int:
        """Heuristic peak score 50–95 from the biggest jump ratio."""
        return int(max(50, min(95, 50 + jump * 40)))

    # ---------------------------------------------------------- keyword triggers
    def _check_keyword_triggers(self, new_words: list[dict]) -> None:
        """ "clip it" / custom phrases in fresh words -> pending trigger."""
        if not new_words:
            return
        # Rolling window so a phrase split across chunks still matches.
        window = self.words[-60:]
        if not window:
            return
        earliest = new_words[0].get("s", 0.0)
        norm = [re.sub(r"[^\w\s']", " ", (w.get("w") or "").lower()).strip() for w in window]
        phrases: list[tuple[str, list[str]]] = []
        for phrase in list(CLIP_PHRASES) + self.trigger_words:
            parts = phrase.split()
            if len(parts) == 1 and len(parts[0]) < 4:
                continue  # a lone short word ("clip") is not a trigger by itself
            phrases.append((phrase, parts))
        for phrase, parts in phrases:
            plen = len(parts)
            for i in range(len(window) - plen + 1):
                if norm[i : i + plen] != parts:
                    continue
                t = float(window[i].get("s", 0.0))
                if t < earliest - 5.0:
                    continue
                if t - self._last_keyword_s <= 30.0:
                    continue  # same moment, said twice
                self._last_keyword_s = t
                self.keyword_triggers.append(
                    {"trigger_s": t, "why": f'Someone said "{phrase}"', "phrase": phrase}
                )
                return

    def drain_keyword_triggers(self) -> list[dict]:
        """Pending keyword triggers (the session fires them once the transcript covers them)."""
        pending, self.keyword_triggers = self.keyword_triggers, []
        return pending

    def keyword_peak(self, trigger: dict) -> Peak:
        """A keyword trigger as a peak window (same 12s/28s shape)."""
        t = float(trigger["trigger_s"])
        text = " ".join(
            w.get("w", "")
            for w in self.words
            if t - self.pre_roll_s <= w.get("s", 0) <= t + self.post_roll_s
        )
        return Peak(
            trigger_s=t,
            start_s=max(0.0, t - self.pre_roll_s),
            end_s=t + self.post_roll_s,
            reasons=[trigger["why"]],
            score=70,
            kind="keyword",
            transcript=text.strip(),
        )


# ------------------------------------------------------------------ chat readers
_NICK_RE = re.compile(r"^:([A-Za-z0-9_]{2,25})!")


class TwitchChatReader(threading.Thread):
    """Anonymous Twitch IRC chat (``justinfan`` — no key, as in ClipNova).

    Calls ``on_message(second, user, text)`` for each chat line, with
    ``second`` from the ``clock`` callable (session elapsed seconds).
    Reconnects on drop until ``stop_event`` is set.
    """

    def __init__(self, channel: str, clock, on_message, stop_event: threading.Event):
        super().__init__(daemon=True, name=f"twitch-chat-{channel}")
        self.channel = channel.lower()
        self.clock = clock
        self.on_message = on_message
        self.stop_event = stop_event
        self.connected = False

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=20)
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
                nick = f"justinfan{int(time.time()) % 90000 + 10000}"
                sock.sendall(f"NICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
                sock.settimeout(30)
                self.connected = True
                rest = b""
                while not self.stop_event.is_set():
                    try:
                        data = sock.recv(8192)
                    except TimeoutError:
                        continue
                    if not data:
                        break
                    *lines, rest = (rest + data).split(b"\r\n")
                    for line in lines:
                        if line.startswith(b"PING"):
                            sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                        elif b" PRIVMSG #" in line:
                            text = line.decode("utf-8", "replace")
                            who = _NICK_RE.findall(text)
                            if not who:
                                continue
                            parts = text.split(f" PRIVMSG #{self.channel} :", 1)
                            if len(parts) > 1 and parts[1].strip():
                                self.on_message(int(self.clock()), who[0], parts[1].strip()[:140])
                sock.close()
            except Exception:  # noqa: BLE001 — chat is best-effort; reconnect
                pass
            self.connected = False
            self.stop_event.wait(5)


class YoutubeChatReader(threading.Thread):
    """YouTube live-chat poller (Data API ``liveChatMessages.list``).

    Needs an OAuth credential — an API key cannot read live chat, so
    the session only starts this reader when one is available.
    ``on_message(second, user, text)`` per message; ``second`` from
    the ``clock`` callable. Polls every ~5s (the API's own interval).
    """

    def __init__(
        self, live_chat_id: str, credentials, clock, on_message, stop_event: threading.Event
    ):
        super().__init__(daemon=True, name="youtube-chat")
        self.live_chat_id = live_chat_id
        self.credentials = credentials
        self.clock = clock
        self.on_message = on_message
        self.stop_event = stop_event
        self.connected = False

    def run(self) -> None:
        from clipforge.trending import _safe_json  # noqa: PLC0415 (avoid cycle)

        page_token: str | None = None
        while not self.stop_event.is_set():
            try:
                with httpx.Client(timeout=30.0) as http:
                    params: dict = {
                        "part": "snippet,authorDetails",
                        "liveChatId": self.live_chat_id,
                        "maxResults": 200,
                    }
                    if page_token:
                        params["pageToken"] = page_token
                    resp = http.get(
                        f"{YOUTUBE_API_BASE}/liveChat/messages",
                        params=params,
                        headers=self.credentials.auth_headers(),
                    )
                    if resp.status_code != 200:
                        self.connected = False
                        self.stop_event.wait(10)
                        continue
                    self.connected = True
                    data = _safe_json(resp)
                    for item in data.get("items") or []:
                        snippet = item.get("snippet") or {}
                        author = item.get("authorDetails") or {}
                        text = (snippet.get("displayMessage") or "").strip()
                        if text:
                            self.on_message(
                                int(self.clock()),
                                str(author.get("displayName") or ""),
                                text[:140],
                            )
                    page_token = data.get("nextPageToken") or page_token
                    interval = int(data.get("pollingIntervalMillis") or 5000)
                self.stop_event.wait(max(2.0, interval / 1000.0))
            except Exception:  # noqa: BLE001 — chat is best-effort; retry
                self.connected = False
                self.stop_event.wait(10)


# ------------------------------------------------------------------ peak -> job
def _data_dir() -> Path:
    return Path(settings.data_dir)


def create_peak_job(
    *,
    session_id: str,
    platform: str,
    source_url: str,
    title: str,
    channel: str,
    peak: Peak,
    peak_file: Path,
    data_dir: str | Path | None = None,
) -> str | None:
    """Create a normal ClipForge job from a cut peak file and enqueue it.

    The job is a plain ``source_type="local"`` job whose
    ``source_path`` is the cut peak — the worker then runs the
    EXISTING pipeline on it (transcribe -> score -> clip -> SEO ->
    approval queue). Returns the job id, or None when the job could
    not be created/enqueued (never raises — a failed enqueue must
    not kill the monitor).
    """
    from clipforge.db import Job, JobStatus, session_factory  # noqa: PLC0415 (lazy: DB only)
    from clipforge.job_queue import get_job_queue  # noqa: PLC0415
    from clipforge.notifications import notify_best_effort  # noqa: PLC0415

    try:
        session = session_factory()()
        try:
            job = Job(
                source_type="local",
                source_path=str(peak_file),
                source_url=source_url,
                title=title or f"Live peak — {platform}",
                own_content=False,
                source_title=title or None,
                source_channel=channel or None,
                status=JobStatus.PENDING.value,
                meta_json={
                    "live_peak": True,
                    "live_monitor_id": session_id,
                    "live_platform": platform,
                    "live_source_url": source_url,
                    "live_peak_trigger_s": round(peak.trigger_s, 2),
                    "live_peak_start_s": round(peak.start_s, 2),
                    "live_peak_end_s": round(peak.end_s, 2),
                    "live_peak_kind": peak.kind,
                    "live_peak_reasons": list(peak.reasons),
                    "live_peak_score": peak.score,
                    "top_n": 3,
                    "caption_style": "bold-pop",
                },
            )
            session.add(job)
            session.commit()
            job_id = job.id
            try:
                get_job_queue().enqueue(session, job_id, priority=1)
                session.refresh(job)
            except Exception:  # noqa: BLE001 — job exists; POST /jobs/{id}/run can start it
                pass
        finally:
            session.close()
    except Exception:  # noqa: BLE001 — never kill the monitor over a job row
        return None
    notify_best_effort(
        "success",
        "Live peak clipped",
        f"Peak at {peak.trigger_s:.0f}s ({', '.join(peak.reasons)}) was cut "
        f"and sent to the pipeline — review it in the approval queue.",
        job_id=job_id,
    )
    return job_id


# ------------------------------------------------------------------ requirements
def check_monitor_requirements(data_dir: str | Path | None = None) -> dict:
    """Pre-flight for live monitoring: ffmpeg, yt-dlp, transcription.

    Returns ``{"requirements": [...], "all_ready": bool, "blocking": [...]}``
    in the same shape as :func:`clipforge.requirements.check_requirements`
    so the UI can reuse its panel. YouTube credentials / chat are
    checked by the router (they need the secrets store), not here.
    """
    from clipforge.requirements import check_requirements  # noqa: PLC0415

    checked = check_requirements(data_dir)
    wanted = {"ffmpeg", "yt-dlp"}
    reqs = [r for r in checked.get("requirements", []) if r.get("id") in wanted]
    # Same detection as the requirements panel (v0.10.2): a
    # system-Python faster-whisper counts — the monitor transcribes
    # through clipforge.transcribe, which reaches it via subprocess.
    from clipforge.whisper_system import detect_faster_whisper  # noqa: PLC0415

    _fw = detect_faster_whisper()
    api_key = bool(os.environ.get("OPENAI_API_KEY"))
    if _fw["available"]:
        whisper_ok = True
        whisper_detail = (
            f"faster-whisper {_fw['version'] or 'unknown version'} via "
            f"{_fw['source']} — rolling transcription works."
        )
    elif _fw["broken"] and api_key:
        whisper_ok = True
        whisper_detail = (
            f"faster-whisper install is broken ({_fw['broken_reason']}); "
            "rolling transcription uses the Whisper API key instead."
        )
    elif api_key:
        whisper_ok = True
        whisper_detail = "Whisper API key is configured — rolling transcription works."
    else:
        whisper_ok = False
        whisper_detail = (
            "faster-whisper is not installed and no OpenAI key is set — "
            "peaks still fire from audio + chat, but there is no rolling "
            "transcript and no keyword triggers."
        )
    reqs.append(
        {
            "id": "whisper",
            "label": "Rolling transcription (faster-whisper)",
            "status": "ok" if whisper_ok else "degraded",
            "version": _fw["version"] if _fw["available"] else "",
            "path": _fw["python"] if _fw["available"] or _fw["broken"] else "",
            "source": _fw["source"] if _fw["available"] or _fw["broken"] else "",
            "detail": whisper_detail,
        }
    )
    blocking = [r["id"] for r in reqs if r.get("status") in {"missing", "broken"}]
    return {"requirements": reqs, "all_ready": not blocking, "blocking": blocking}


# ------------------------------------------------------------------ the session
@dataclass
class MonitorSessionInfo:
    session_id: str
    mode: str  # "live" | "replay"
    platform: str
    url: str
    title: str = ""
    channel: str = ""
    status: str = "starting"  # starting | live | ended | failed
    error: str = ""
    started_at: float = 0.0
    ended_at: float | None = None
    elapsed_s: float = 0.0
    peaks: list[dict] = field(default_factory=list)
    missed_peaks: int = 0
    chat_on: bool = False
    chat_total: int = 0
    transcript_ok: bool | None = None  # None = not tried yet
    transcript_note: str = ""
    words: int = 0
    stalled: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class LiveMonitorSession:
    """One monitored stream: ingest + analyse + peak -> job.

    ``mode="live"`` records the live edge with ffmpeg (buffer
    segments + audio pipe); ``mode="replay"`` replays a local file
    at real-time speed through the same detector and cuts peaks
    from the file. Both modes share the detector, the transcript
    loop and the peak -> job path, so a replay verifies exactly
    what a live run does.
    """

    def __init__(
        self,
        session_id: str,
        *,
        mode: str,
        platform: str,
        url: str,
        data_dir: str | Path,
        max_hours: float = MAX_HOURS_DEFAULT,
        buffer_min: int = BUFFER_MIN_DEFAULT,
        trigger_words: list[str] | None = None,
        replay_path: str | Path | None = None,
        resolver=None,
        transcriber=None,
        chat_credentials=None,
        pace_replay: bool = True,
    ):
        self.info = MonitorSessionInfo(
            session_id=session_id,
            mode=mode,
            platform=platform,
            url=url,
            started_at=time.time(),
        )
        # ``pace_replay=False`` decodes a replay as fast as possible
        # instead of real-time: test/verification only — the router
        # always replays in real time, so a replay still behaves like
        # a live feed. The detector works on the audio clock either way.
        self.pace_replay = pace_replay
        self.dir = Path(data_dir) / "live_monitor" / session_id
        self.buf = self.dir / "buffer"
        self.peaks_dir = self.dir / "peaks"
        self.buf.mkdir(parents=True, exist_ok=True)
        self.peaks_dir.mkdir(parents=True, exist_ok=True)
        self.max_hours = max(0.1, min(MAX_HOURS_CAP, float(max_hours or MAX_HOURS_DEFAULT)))
        self.buffer_min = max(5, int(buffer_min or BUFFER_MIN_DEFAULT))
        self.detector = PeakDetector(trigger_words=trigger_words)
        self.replay_path = Path(replay_path) if replay_path else None
        self.resolver = resolver or resolve_stream
        self.transcriber = transcriber  # fn(wav_path, duration_s) -> list[{"w","s","e"}] | None
        self.chat_credentials = chat_credentials
        self.lock = threading.RLock()
        self.stop_flag = threading.Event()
        self.ended = False
        self.user_stopped = False
        self.end_note = ""
        self.proc: subprocess.Popen | None = None
        self.pending = bytearray()
        self.pending_base = 0  # sample index where pending starts
        self.samples = 0
        self.db: list[float] = []
        self.words: list[dict] = []
        self.t_done = 0.0
        self.segments: list[tuple[Path, float, float]] = []
        self.seg_clock = 0.0
        self.part = 0
        self.part_base: dict[int, float] = {}
        self.csv_seen: dict[int, int] = {}
        self.listed: set[str] = set()
        self.swept = 0.0
        self.chat_total = 0
        self.recent_chat: list[dict] = []
        self.chat_reader: threading.Thread | None = None
        self.stream_title = ""
        self.stream_channel = ""
        self.stream_video_id = ""
        self._peak_n = 0
        self._pending_keyword: list[dict] = []
        self._transcribe_disabled_until = 0.0
        self._threads: list[threading.Thread] = []

    # ---------------------------------------------------------- clock / status
    def elapsed(self) -> float:
        return self.samples / SR

    def status(self) -> dict:
        """Pollable session state for the UI (a copy, never live objects)."""
        with self.lock:
            info = self.info.to_dict()
            info["elapsed_s"] = round(self.elapsed(), 1)
            info["level"] = [round(v) for v in self.db[-60:]]
            info["base"] = dict(self.detector.base)
            info["transcript_tail"] = " ".join(w.get("w", "") for w in self.words[-40:])
            info["recent_chat"] = list(self.recent_chat[-20:])
            info["buffer_s"] = round(
                (self.segments[-1][2] - self.segments[0][1]) if self.segments else 0.0, 1
            )
            info["peaks"] = list(self.info.peaks)
            return info

    # ---------------------------------------------------------- lifecycle
    def start(self) -> None:
        ingest = threading.Thread(
            target=self._ingest_safe, daemon=True, name=f"live-ingest-{self.info.session_id}"
        )
        analyze = threading.Thread(
            target=self._analyze, daemon=True, name=f"live-analyze-{self.info.session_id}"
        )
        self._threads = [ingest, analyze]
        ingest.start()
        analyze.start()

    def stop(self, note: str = "Stopped by you") -> None:
        self.end_note = note
        self.user_stopped = True
        self.stop_flag.set()
        with self.lock:
            if self.info.status in {"starting", "live"}:
                self.info.status = "ended"
                self.info.error = ""
            proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()

    # ---------------------------------------------------------- ingest
    def _ingest_safe(self) -> None:
        try:
            if self.info.mode == "replay":
                self._ingest_replay()
            else:
                self._ingest_live()
        except NotLiveError as exc:
            self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 — session fails plainly, never hangs
            self._fail(f"Monitoring stopped: {exc}")
        finally:
            self.ended = True

    def _fail(self, message: str) -> None:
        with self.lock:
            self.info.status = "failed"
            self.info.error = message
            self.info.ended_at = time.time()

    def _ingest_live(self) -> None:
        try:
            resolved = self.resolver(self.info.url)
        except NotLiveError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise NotLiveError(f"Could not open this live stream: {str(exc)[-200:]}") from exc
        with self.lock:
            self.stream_title = str(resolved.get("title") or "")
            self.stream_channel = str(resolved.get("uploader") or "")
            self.stream_video_id = str(resolved.get("video_id") or "")
            self.info.title = self.stream_title
            self.info.channel = self.stream_channel
            self.info.status = "live"
        self._start_chat()
        urls = resolved["urls"]
        fails = 0
        while not self.stop_flag.is_set():
            try:
                resolved = self.resolver(self.info.url)
                urls = resolved["urls"]
            except NotLiveError:
                # The stream ended between recordings: a session that
                # already recorded is done, not failed.
                if self.samples > 0:
                    break
                raise
            except Exception as exc:  # noqa: BLE001
                if self.samples > 0:
                    break
                raise NotLiveError(f"Could not open this live stream: {str(exc)[-200:]}") from exc
            self.part += 1
            with self.lock:
                self.seg_clock = max(self.seg_clock, self.samples / SR)
            part = f"p{self.part:02d}"
            cmd = ["ffmpeg", "-v", "error"]
            for u in urls:
                cmd += [
                    "-reconnect",
                    "1",
                    "-reconnect_streamed",
                    "1",
                    "-reconnect_delay_max",
                    "10",
                    "-rw_timeout",
                    "15000000",
                    "-i",
                    u,
                ]
            a_in = "1:a:0" if len(urls) > 1 else "0:a:0"
            cmd += [
                "-map",
                "0:v:0",
                "-map",
                a_in,
                "-c",
                "copy",
                "-f",
                "segment",
                "-segment_time",
                str(SEG_SEC),
                "-reset_timestamps",
                "1",
                "-segment_format",
                "mpegts",
                "-segment_list",
                f"{part}.csv",
                "-segment_list_type",
                "csv",
                f"{part}_%05d.ts",
                "-map",
                a_in,
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(SR),
                "-f",
                "s16le",
                "pipe:1",
            ]
            with open(self.dir / "ffmpeg.log", "ab") as errlog:
                proc = subprocess.Popen(cmd, cwd=self.buf, stdout=subprocess.PIPE, stderr=errlog)
                with self.lock:
                    self.proc = proc
                    stopped = self.stop_flag.is_set()
                if stopped:
                    proc.terminate()
                got = 0
                while True:
                    if self.stop_flag.is_set():
                        if proc.poll() is None:
                            proc.terminate()
                        break
                    data = proc.stdout.read(SR * 2) if proc.stdout else b""
                    if not data:
                        break
                    data = data[: len(data) // 2 * 2]
                    got += len(data)
                    self._on_audio_bytes(data)
                    if self.samples / SR - self.seg_clock > STALL_SEC:
                        with self.lock:
                            self.info.stalled += 1
                        proc.terminate()
                        break
                proc.stdout.close()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            fails = 0 if got > SR * 2 * 10 else fails + 1
            if fails >= (3 if self.samples == 0 else 10):
                break
            self.stop_flag.wait(3)
        if self.samples == 0 and not self.user_stopped:
            self._fail("Could not record the live stream — ffmpeg got no audio from it.")

    def _ingest_replay(self) -> None:
        """Replay a local file at real-time speed through the same analysis."""
        if not self.replay_path or not self.replay_path.is_file():
            raise LiveMonitorError(f"Replay file not found: {self.replay_path}")
        with self.lock:
            self.info.title = self.replay_path.name
            self.stream_title = f"Replay — {self.replay_path.name}"
            self.info.channel = "replay"
            self.info.status = "live"
        cmd = ["ffmpeg", "-v", "error"]
        if self.pace_replay:
            cmd.append("-re")
        cmd += [
            "-i",
            str(self.replay_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SR),
            "-f",
            "s16le",
            "pipe:1",
        ]
        with open(self.dir / "ffmpeg.log", "ab") as errlog:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errlog)
            with self.lock:
                self.proc = proc
            while not self.stop_flag.is_set():
                data = proc.stdout.read(SR * 2) if proc.stdout else b""
                if not data:
                    break
                data = data[: len(data) // 2 * 2]
                self._on_audio_bytes(data)
            proc.stdout.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if self.samples == 0 and not self.user_stopped:
            self._fail("Replay file has no audio to monitor.")

    def _on_audio_bytes(self, data: bytes) -> None:
        """One second of 16-bit mono audio: level + pending transcript audio.

        The spike check runs here, on every audio second — not only
        in the (slower) analyze loop. A replay decoded faster than
        real time would otherwise deliver all 90 seconds before the
        loop's next tick, and a burst 20 seconds back would already
        be inside the baseline instead of above it.
        """
        import numpy as _np  # noqa: PLC0415 (numpy is a hard dep of the server)

        x = _np.frombuffer(data, dtype="<i2").astype(_np.float32) / 32768.0
        if len(x):
            rms = float(_np.sqrt((_np.float64(x) ** 2).mean() + 1e-10))
            level = float(20 * _np.log10(rms))
        else:
            level = -100.0
        with self.lock:
            self.pending += data
            self.samples += len(data) // 2
            self.db.append(level)
            self.detector.add_audio_second(level)
        peak = self.detector.check()
        if peak is not None:
            self._emit_peak(peak)

    # ---------------------------------------------------------- chat
    def _on_chat(self, second: int, user: str, text: str) -> None:
        with self.lock:
            self.chat_total += 1
            self.info.chat_total = self.chat_total
            self.detector.add_chat(second, user or None)
            self.recent_chat.append({"user": user, "msg": text, "t": round(self.elapsed(), 1)})
            del self.recent_chat[:-60]

    def _start_chat(self) -> None:
        """Start the platform's chat reader, if one is available."""
        platform = self.info.platform
        if platform == "twitch":
            m = re.match(r"^https?://(?:www\.)?twitch\.tv/([A-Za-z0-9_]+)", self.info.url)
            if m:
                reader = TwitchChatReader(m.group(1), self.elapsed, self._on_chat, self.stop_flag)
                self.chat_reader = reader
                reader.start()
                with self.lock:
                    self.info.chat_on = True
        elif (
            platform == "youtube"
            and self.chat_credentials is not None
            and getattr(self.chat_credentials, "kind", "") == "oauth"
        ):
            chat_id = ""
            if self.stream_video_id:
                chat_id = youtube_live_chat_id(
                    self.stream_video_id, credentials=self.chat_credentials
                )
            if chat_id:
                reader = YoutubeChatReader(
                    chat_id, self.chat_credentials, self.elapsed, self._on_chat, self.stop_flag
                )
                self.chat_reader = reader
                reader.start()
                with self.lock:
                    self.info.chat_on = True

    # ---------------------------------------------------------- segments (live buffer)
    def _scan_segments(self) -> None:
        with self.lock:
            for i in range(1, self.part + 1):
                f = self.buf / f"p{i:02d}.csv"
                if not f.exists():
                    continue
                text = f.read_text(encoding="utf-8", errors="ignore")
                rows = [r.split(",") for r in text.splitlines() if r.count(",") >= 2]
                fresh = rows[self.csv_seen.get(i, 0) :]
                if fresh and i not in self.part_base:
                    try:
                        first = fresh[0]
                        if float(first[1]) >= self.seg_clock - 5:
                            self.part_base[i] = 0.0
                        else:
                            self.part_base[i] = self.seg_clock
                    except (IndexError, ValueError):
                        self.part_base[i] = 0.0
                base = self.part_base.get(i, 0.0)
                for name, a, b in (r[:3] for r in fresh):
                    try:
                        start, end = base + float(a), base + float(b)
                    except ValueError:
                        continue
                    if end <= start:
                        continue
                    self.segments.append((self.buf / name, start, end))
                    self.seg_clock = max(self.seg_clock, end)
                self.listed.update(r[0] for r in rows if len(r) > 2)
                self.csv_seen[i] = len(rows)
            keep_from = self.seg_clock - self.buffer_min * 60
            while self.segments and self.segments[0][2] < keep_from:
                self.segments.pop(0)[0].unlink(missing_ok=True)
            if time.time() - self.swept > 60:
                self.swept = time.time()
                now = time.time()
                for p in self.buf.glob("p*.ts"):
                    try:
                        if p.name not in self.listed and now - p.stat().st_mtime > 60:
                            p.unlink(missing_ok=True)
                    except OSError:
                        pass

    # ---------------------------------------------------------- rolling transcription
    def _transcribe_step(self) -> bool:
        """Transcribe ~7s of pending audio (faster-whisper, tiny model)."""
        with self.lock:
            n = len(self.pending) // 2
            hurry = self.ended
            if n < (SR if hurry else int(SR * TRANSCRIBE_CHUNK_S)):
                return False
            audio = bytes(self.pending[: n * 2])
            base = self.pending_base
            cut = n
        if time.time() < self._transcribe_disabled_until:
            return False
        words = self._transcribe_bytes(audio[: cut * 2], cut)
        if words is None:
            return False
        off = base / SR
        for w in words:
            w["s"] = round(w["s"] + off, 2)
            w["e"] = round(w["e"] + off, 2)
        with self.lock:
            del self.pending[: cut * 2]
            self.pending_base += cut
            self.words += words
            self.detector.add_words(words)
            self.t_done = self.pending_base / SR
            self.info.words = len(self.words)
        return True

    def _transcribe_bytes(self, raw: bytes, n_samples: int) -> list[dict] | None:
        """Raw s16le mono -> timed words, or None when transcription is unavailable."""
        if self.transcriber is not None:
            try:
                return self.transcriber(raw, n_samples) or []
            except Exception:  # noqa: BLE001 — injected transcriber failed; disable briefly
                self._transcribe_disabled_until = time.time() + 30
                return None
        try:
            from clipforge.transcribe import transcribe  # noqa: PLC0415
        except ImportError:
            self._disable_transcript("transcription module unavailable")
            return None
        tmp = self.dir / f"chunk_{self.pending_base}.wav"
        try:
            with wave.open(str(tmp), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SR)
                wav.writeframes(raw)
            sha = hashlib.sha256(raw).hexdigest()
            result = transcribe(
                tmp,
                source_sha256=sha,
                duration_s=n_samples / SR,
                model="tiny",
                use_cache=False,
            )
            with self.lock:
                self.info.transcript_ok = True
                self.info.transcript_note = f"Rolling transcription on ({result.engine})."
            return [
                {"w": w.text, "s": w.start, "e": w.end}
                for seg in result.segments
                for w in seg.words
            ]
        except RuntimeError as exc:
            # No backend (faster-whisper missing, no key): audio+chat
            # peaks still work; transcription stays off for 5 minutes.
            self._disable_transcript(str(exc))
            self._transcribe_disabled_until = time.time() + 300
            return None
        except Exception:  # noqa: BLE001 — a bad chunk must not kill the monitor
            self._transcribe_disabled_until = time.time() + 30
            return None
        finally:
            tmp.unlink(missing_ok=True)

    def _disable_transcript(self, note: str) -> None:
        with self.lock:
            self.info.transcript_ok = False
            self.info.transcript_note = note

    # ---------------------------------------------------------- analysis loop
    def _analyze(self) -> None:
        while True:
            busy = False
            try:
                if not self.user_stopped and not self.ended:
                    if time.time() - self.info.started_at > self.max_hours * 3600:
                        self.stop(
                            f"Stopped after {self.max_hours:g} hours, "
                            "the longest one monitor can run"
                        )
                if self.info.mode == "live":
                    self._scan_segments()
                busy = self._transcribe_step()
                # Keyword triggers: fire once the transcript covers trigger + 15s.
                for trig in self.detector.drain_keyword_triggers():
                    self._pending_keyword.append(trig)
                for trig in list(self._pending_keyword):
                    covered = self.t_done >= trig["trigger_s"] + KEYWORD_WAIT_S
                    if covered or (self.ended and not busy):
                        self._pending_keyword.remove(trig)
                        peak = self.detector.keyword_peak(trig)
                        self._emit_peak(peak)
                peak = self.detector.check()
                if peak is not None:
                    self._emit_peak(peak)
            except Exception:  # noqa: BLE001 — analysis must never kill ingest
                pass
            if (self.user_stopped and not self._pending_keyword) or (
                self.ended and not busy and len(self.pending) < SR * 2 and not self._pending_keyword
            ):
                break
            if not busy:
                time.sleep(2)
        with self.lock:
            if self.info.status in {"starting", "live"}:
                self.info.status = "ended"
                self.info.error = ""
                self.info.ended_at = time.time()
            self.info.elapsed_s = round(self.elapsed(), 1)
            self.info.words = len(self.words)
        # Finished sessions stay in the registry so the UI can still
        # show them (and their error, when they failed) — only the
        # start path prunes old finished ones. A session that vanished
        # the moment it failed would leave the user with a 404
        # instead of the plain-language reason.

    # ---------------------------------------------------------- peak -> job
    def _emit_peak(self, peak: Peak) -> None:
        """Cut a peak window and feed it to the pipeline (on its own thread)."""
        threading.Thread(
            target=self._cut_and_submit,
            args=(peak,),
            daemon=True,
            name=f"live-peak-{self.info.session_id}-{peak.trigger_s:.0f}",
        ).start()

    def _cut_and_submit(self, peak: Peak) -> None:
        self._peak_n += 1
        n = self._peak_n
        out = self.peaks_dir / f"peak_{n:03d}.mp4"
        record: dict = {**peak.to_dict(), "n": n, "file": str(out), "job_id": None, "error": ""}
        with self.lock:
            self.info.peaks.append(record)
        try:
            self._cut_peak(peak, out)
        except Exception as exc:  # noqa: BLE001 — a failed cut is a missed peak, recorded honestly
            record["error"] = f"Could not cut this peak: {exc}"
            with self.lock:
                self.info.missed_peaks += 1
            return
        job_id = create_peak_job(
            session_id=self.info.session_id,
            platform=self.info.platform,
            source_url=self.info.url,
            title=self.stream_title or f"Live peak — {self.info.platform}",
            channel=self.stream_channel,
            peak=peak,
            peak_file=out,
        )
        record["job_id"] = job_id
        if job_id is None:
            record["error"] = "Peak was cut but its pipeline job could not be created."
            with self.lock:
                self.info.missed_peaks += 1

    def _cut_peak(self, peak: Peak, out: Path) -> None:
        """Cut ``peak``'s window to ``out`` (validated: a bad cut raises)."""
        from clipforge.ingest import validate_local_file  # noqa: PLC0415 (avoid cycle)

        if self.info.mode == "replay" and self.replay_path is not None:
            duration = self._probe_duration(self.replay_path)
            start = max(0.0, peak.start_s)
            end = min(duration, peak.end_s) if duration else peak.end_s
            if end - start < 5:
                raise LiveMonitorError("Peak window is shorter than 5 seconds — nothing to cut.")
            self._run_ffmpeg(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{start:.2f}",
                    "-to",
                    f"{end:.2f}",
                    "-i",
                    str(self.replay_path),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-c:a",
                    "aac",
                    str(out),
                ]
            )
        else:
            # Live: wait for the buffer to cover the window, then remux the segments.
            deadline = time.time() + PEAK_RENDER_WAIT_S
            segs: list[tuple[Path, float, float]] = []
            while time.time() < deadline:
                self._scan_segments()
                with self.lock:
                    segs = [
                        s
                        for s in self.segments
                        if s[2] > peak.start_s - PAD_S
                        and s[1] < peak.end_s + PAD_S
                        and s[0].exists()
                    ]
                    covered = bool(segs) and segs[-1][2] >= peak.end_s
                if covered or self.ended:
                    break
                time.sleep(1)
            if not segs or segs[-1][2] < peak.start_s:
                raise LiveMonitorError("That moment has already left the rolling buffer.")
            if any(b[1] > a[2] + 2 for a, b in zip(segs, segs[1:])):
                raise LiveMonitorError(
                    "That moment was not fully recorded — the stream dropped out there."
                )
            start = max(peak.start_s, segs[0][1])
            end = min(peak.end_s, segs[-1][2])
            if end - start < 5:
                raise LiveMonitorError("Peak window is shorter than 5 seconds — nothing to cut.")
            lst = self.dir / f"peak_{self._peak_n:03d}.txt"
            lst.write_text("".join(f"file '{s[0].as_posix()}'\n" for s in segs), encoding="utf-8")
            try:
                self._run_ffmpeg(
                    [
                        "ffmpeg",
                        "-y",
                        "-v",
                        "error",
                        "-fflags",
                        "+genpts",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(lst),
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        "-avoid_negative_ts",
                        "make_zero",
                        str(out),
                    ]
                )
            finally:
                lst.unlink(missing_ok=True)
        try:
            validate_local_file(out)
        except Exception as exc:  # noqa: BLE001 — unreadable cut
            out.unlink(missing_ok=True)
            raise LiveMonitorError(
                f"The cut peak file could not be read as a video ({exc})."
            ) from exc

    @staticmethod
    def _run_ffmpeg(cmd: list[str]) -> None:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            last = err.splitlines()[-1] if err else "unknown error"
            raise LiveMonitorError(f"ffmpeg could not cut the peak ({last}).")

    @staticmethod
    def _probe_duration(path: Path) -> float:
        from clipforge.ingest import validate_local_file  # noqa: PLC0415

        try:
            return float(validate_local_file(path).duration_s or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    def clip_now(self, seconds: float = 30.0) -> Peak:
        """Manual "clip the last N seconds" (ClipNova's ``clip_last``)."""
        end = self.elapsed()
        if self.info.mode == "live" and self.seg_clock and self.seg_clock < end:
            end = self.seg_clock
        if end < 5:
            raise LiveMonitorError("Nothing has been recorded yet.")
        seconds = max(5.0, min(120.0, float(seconds)))
        peak = Peak(
            trigger_s=end,
            start_s=max(0.0, end - seconds),
            end_s=end,
            reasons=["Clipped by you"],
            score=70,
            kind="manual",
        )
        self._emit_peak(peak)
        return peak


# ------------------------------------------------------------------ registry
SESSIONS: dict[str, LiveMonitorSession] = {}
_REGISTRY_LOCK = threading.Lock()


def start_monitor(
    *,
    url: str | None = None,
    replay_path: str | Path | None = None,
    data_dir: str | Path | None = None,
    max_hours: float = MAX_HOURS_DEFAULT,
    buffer_min: int = BUFFER_MIN_DEFAULT,
    trigger_words: list[str] | None = None,
    chat_credentials=None,
    resolver=None,
    transcriber=None,
    pace_replay: bool = True,
) -> LiveMonitorSession:
    """Start monitoring a live stream (or a replay file).

    ``url`` starts a live monitor (platform auto-detected);
    ``replay_path`` starts a replay monitor instead. Raises
    :class:`LiveMonitorError` in plain language when the platform
    is unsupported, the monitor limit is reached, or a required
    tool is missing.
    """
    if replay_path is not None:
        mode, platform, url = "replay", "replay", str(replay_path)
        path = Path(replay_path)
        if not path.is_file():
            raise LiveMonitorError(f"Replay file not found: {replay_path}")
    else:
        if not url or not str(url).strip():
            raise LiveMonitorError(
                "Give a live stream URL (YouTube, Twitch or Kick) or a replay file."
            )
        platform = detect_platform(str(url))
        if platform is None:
            raise LiveMonitorError(
                "That URL is not a YouTube, Twitch or Kick live stream. "
                "Live monitoring works on those three platforms."
            )
        mode = "live"
    if shutil.which("ffmpeg") is None:
        raise LiveMonitorError(
            "FFmpeg is missing — live monitoring needs it to record and "
            "analyse the stream. Press 'Install FFmpeg' in Settings."
        )
    if mode == "live":
        try:
            import yt_dlp  # noqa: F401
        except ImportError as exc:
            raise LiveMonitorError(
                "yt-dlp is missing — live monitoring needs it to open the "
                "stream. Press 'Install yt-dlp' in Settings."
            ) from exc
    with _REGISTRY_LOCK:
        live = [s for s in SESSIONS.values() if s.info.status in {"starting", "live"}]
        if len(live) >= MAX_SESSIONS:
            raise LiveMonitorError(
                f"Already monitoring {len(live)} streams — stop one first "
                f"(at most {MAX_SESSIONS} at once)."
            )
        # Prune old finished sessions (keep the 10 newest) so the
        # registry does not grow forever; active ones are never pruned.
        finished = sorted(
            (s for s in SESSIONS.values() if s.info.status not in {"starting", "live"}),
            key=lambda s: s.info.started_at,
        )
        for old in finished[:-10]:
            SESSIONS.pop(old.info.session_id, None)
        session_id = uuid.uuid4().hex[:12]
        session = LiveMonitorSession(
            session_id,
            mode=mode,
            platform=platform,
            url=str(url),
            data_dir=data_dir or _data_dir(),
            max_hours=max_hours,
            buffer_min=buffer_min,
            trigger_words=trigger_words,
            replay_path=replay_path,
            resolver=resolver,
            transcriber=transcriber,
            chat_credentials=chat_credentials,
            pace_replay=pace_replay,
        )
        SESSIONS[session_id] = session
    session.start()
    return session


def get_monitor(session_id: str) -> LiveMonitorSession | None:
    return SESSIONS.get(session_id)


def list_monitors() -> list[dict]:
    """All monitor sessions, newest first (finished ones included)."""
    ordered = sorted(SESSIONS.values(), key=lambda s: s.info.started_at, reverse=True)
    return [s.status() for s in ordered]


def stop_monitor(session_id: str, note: str = "Stopped by you") -> LiveMonitorSession | None:
    session = SESSIONS.get(session_id)
    if session is None:
        return None
    session.stop(note)
    return session


__all__ = [
    "BASELINE_WINDOW_S",
    "BUFFER_MIN_DEFAULT",
    "CLIP_PHRASES",
    "JUMP",
    "LIVE_MONITOR_NOTICE",
    "LiveMonitorError",
    "LiveMonitorSession",
    "MAX_HOURS_CAP",
    "MAX_HOURS_DEFAULT",
    "MAX_SESSIONS",
    "NotLiveError",
    "Peak",
    "PeakDetector",
    "POST_ROLL_S",
    "PRE_ROLL_S",
    "SESSIONS",
    "TwitchChatReader",
    "YoutubeChatReader",
    "check_monitor_requirements",
    "create_peak_job",
    "detect_platform",
    "get_monitor",
    "list_monitors",
    "platform_capabilities",
    "resolve_stream",
    "start_monitor",
    "stop_monitor",
    "youtube_live_chat_id",
]

LIVE_MONITOR_NOTICE = (
    "Live monitoring watches a stream's audio, chat and speech for peak moments "
    "and sends each peak to the normal clip pipeline. Peaks are cut from the live "
    "edge — a moment that already passed cannot be recovered. Make sure you have "
    "the right to use a stream before you clip it."
)
