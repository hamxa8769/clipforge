"""ClipForge Agent (v0.5.0) — ONE automation agent for a channel.

The agent takes a source the user provides — a video URL or a source
YouTube channel URL — fetches / transcribes it through the existing
pipeline, scores moments for the channel's niche, clips, writes
titles / descriptions / hashtags / SEO with Gemini, the
OpenAI-compatible LLM, or the rule engine (no key needed), and queues
posts for APPROVAL.

Approval-first is the default and is enforced here, not just in the
UI: the agent NEVER publishes without approval unless the user
explicitly enables ``auto_post`` in the config (default OFF). With
auto-post on, posts are still created first and only then published,
and live publishing additionally requires dry-run off + credentials
(the same gate as POST /posts/{id}/publish).

Rights guardrail: the agent is for the user's OWN videos or videos
they have rights / a licence to use. It is not presented as a tool to
re-upload other creators' videos. The config carries a required
``rights_confirmed`` acknowledgement; the agent refuses to run on a
source channel until the user confirms the source is theirs or
licensed.

State (config + run state) is persisted in ``<data_dir>/agent.json``,
so a restart keeps the config and the last run's outcome.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from clipforge.auth.settings_store import SettingsStore
from clipforge.config import settings as _env_settings
from clipforge.db import (
    CandidateSeo,
    ClipCandidate,
    Job,
    JobStatus,
    Post,
    PostStatus,
    SocialAccount,
)
from clipforge.highlights import find_candidates
from clipforge.llm import generate_copy_with_engine, resolve_engine
from clipforge.niches import (
    GENERAL_NICHE,
    PRESET_NAMES,
    niche_label,
    normalize_niche,
)
from clipforge.publishing import meta as meta_adapter
from clipforge.publishing import tiktok as tiktok_adapter
from clipforge.publishing import youtube as youtube_adapter
from clipforge.rule_engine import extract_keywords
from clipforge.seo import PLATFORMS
from clipforge.transcribe import fallback_transcript_from_text
from clipforge.trending import is_trending_source

RIGHTS_NOTICE = (
    "The ClipForge Agent only works with videos you own or have the "
    "rights / licence to use. It is not a tool to re-upload other "
    "creators' videos. Confirm the source is yours or licensed before "
    "the agent runs on it."
)

AGENT_FILE = "agent.json"
SOURCE_TYPES = ("video", "channel")
_URL_RE = re.compile(r"^https?://\S+$")
_YT_CHANNEL_RE = re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE)


class AgentError(ValueError):
    """Plain-language agent failure — safe to show to the user as-is."""


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class AgentConfig:
    """Agent configuration (persisted in agent.json)."""

    enabled: bool = False
    source_type: str = "video"  # "video" | "channel"
    source_url: str = ""
    niche: str = GENERAL_NICHE
    auto_post: bool = False  # default OFF — approval-first
    rights_confirmed: bool = False
    # Demo / test transcript: when set, the agent scores this text
    # instead of downloading a video (no network, no FFmpeg). Runs
    # made this way are flagged demo in the state and on the job.
    transcript_text: str | None = None
    top_n: int = 3
    platforms: list[str] = field(
        default_factory=lambda: ["youtube", "tiktok", "instagram", "facebook"]
    )
    # Trending loop (v0.6.0): the trending video is the TOPIC only — the
    # source is always the user's own video. ``trending_topic`` (usually
    # the trending title) seeds the SEO keywords; ``scheduled_for`` is
    # an ISO timestamp the queued posts are scheduled for (None = the
    # user schedules them from the approval queue).
    trending_video_id: str = ""
    trending_title: str = ""
    trending_topic: str = ""
    scheduled_for: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        # Never persist a transcript longer than needed for a demo run.
        return data

    @classmethod
    def from_dict(cls, data: dict | None) -> AgentConfig:
        if not isinstance(data, dict):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        config = cls(**known)
        return config

    def validate_structure(self) -> None:
        """Structural checks only (source, niche, platforms) — no rights
        flag, so the settings page can save a config before the user
        confirms rights. :meth:`validate` adds the rights check."""
        if self.source_type not in SOURCE_TYPES:
            raise AgentError('Source type must be "video" or "channel".')
        if not self.source_url or not _URL_RE.match(self.source_url.strip()):
            raise AgentError("Give the agent a source URL starting with http:// or https://.")
        if self.source_type == "channel" and not _YT_CHANNEL_RE.search(self.source_url):
            raise AgentError(
                "A channel source must be a YouTube channel URL "
                "(youtube.com/@handle, /channel/… or /c/…)."
            )
        if len(self.niche) > 60:
            raise AgentError("Niche must be 60 characters or less.")
        if not 1 <= int(self.top_n or 0) <= 10:
            raise AgentError("Clips per run (top_n) must be between 1 and 10.")
        bad = [p for p in (self.platforms or []) if p not in PLATFORMS]
        if bad:
            raise AgentError(f"Unknown platforms in agent config: {bad}.")

    def validate(self) -> None:
        """Full validation: structure + the rights acknowledgement.

        Raises AgentError (plain language) on the first problem.
        """
        self.validate_structure()
        if not self.rights_confirmed:
            raise AgentError(
                "Please confirm the source is yours or licensed first: "
                "tick the rights confirmation in the agent settings. " + RIGHTS_NOTICE
            )
        # Trending guardrail (v0.6.0): a trending video URL is never a
        # clip source — only the user's own video on that topic is.
        if self.trending_video_id and is_trending_source(self.source_url, self.trending_video_id):
            raise AgentError(
                "That source is the trending video itself — the agent "
                "cannot clip another creator's video. Use YOUR OWN video "
                "on this topic as the source instead."
            )


@dataclass
class AgentState:
    """Run state (persisted in agent.json next to the config)."""

    status: str = "idle"  # idle | running | done | error | stopped
    current_job: str | None = None
    progress_pct: float = 0.0
    queued: int = 0
    approved: int = 0
    published: int = 0
    engine: str = "rules"
    demo: bool = False
    last_error: str | None = None
    last_run_at: str | None = None
    # Trending loop (v0.6.0): which trending video the current/last run
    # was inspired by (topic only — never the clip source).
    trending_video_id: str = ""
    trending_title: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> AgentState:
        if not isinstance(data, dict):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class AgentStore:
    """JSON persistence for the agent config + state (data dir)."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self._path = self.data_dir / AGENT_FILE

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, payload: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    def load_config(self) -> AgentConfig:
        return AgentConfig.from_dict(self._read().get("config"))

    def save_config(self, config: AgentConfig) -> AgentConfig:
        payload = self._read()
        payload["config"] = config.to_dict()
        payload.setdefault("state", AgentState().to_dict())
        self._write(payload)
        return config

    def load_state(self) -> AgentState:
        return AgentState.from_dict(self._read().get("state"))

    def save_state(self, state: AgentState) -> AgentState:
        payload = self._read()
        payload["state"] = state.to_dict()
        payload.setdefault("config", AgentConfig().to_dict())
        self._write(payload)
        return state


def _trending_meta(cfg: AgentConfig) -> dict:
    """Job meta for a trending run: the inspiration (topic only) + the
    own source it was matched to. The posted clips' platform + provider
    video id live on the Post records (publish_result_json) — the job
    meta is what ties them back to the trending topic for monitoring."""
    if not cfg.trending_video_id and not cfg.trending_title:
        return {}
    return {
        "source": "trending",
        "trending_video_id": cfg.trending_video_id,
        "trending_title": cfg.trending_title,
        "trending_topic": cfg.trending_topic or cfg.trending_title,
        "trending_url": (
            f"https://www.youtube.com/watch?v={cfg.trending_video_id}"
            if cfg.trending_video_id
            else ""
        ),
    }


def _latest_channel_video_url(channel_url: str) -> str:
    """Latest video URL of a YouTube channel, via yt-dlp (flat playlist).

    Raises AgentError with a plain message when yt-dlp is missing or
    the channel cannot be read — the agent never pretends it fetched
    a channel it did not.
    """
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        raise AgentError(
            "Channel fetch needs yt-dlp, which is not installed here. "
            "Install yt-dlp (pip install yt-dlp) or give the agent a "
            "single video URL instead."
        )
    try:
        proc = subprocess.run(
            [yt_dlp, "--flat-playlist", "--playlist-end", "1", "--get-id", channel_url],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentError(
            f"Could not read the channel ({exc}). Check the URL and try again."
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise AgentError(
            "Could not read the channel — "
            f"{detail[-1] if detail else 'yt-dlp failed'}. "
            "Check the channel URL and try again."
        )
    video_id = (proc.stdout or "").strip().splitlines()
    if not video_id or not video_id[0].strip():
        raise AgentError("The channel has no videos the agent could read.")
    return f"https://www.youtube.com/watch?v={video_id[0].strip()}"


class Agent:
    """The single ClipForge Agent: one runner, toggleable per config."""

    def __init__(
        self,
        data_dir: str | Path,
        session_factory,
        settings_store: SettingsStore | None = None,
    ):
        self.store = AgentStore(data_dir)
        self.data_dir = Path(data_dir)
        self._session_factory = session_factory
        self._settings_store = settings_store
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ plumbing
    def _settings(self) -> SettingsStore:
        return self._settings_store or SettingsStore(self.data_dir)

    def _set_state(self, **patch) -> AgentState:
        state = self.store.load_state()
        for key, value in patch.items():
            setattr(state, key, value)
        return self.store.save_state(state)

    def status(self) -> dict:
        """Current agent status (config + run state, no secrets)."""
        config = self.store.load_config()
        state = self.store.load_state()
        running = state.status == "running" and bool(
            self._thread is not None and self._thread.is_alive()
        )
        return {
            "enabled": config.enabled,
            "running": running,
            "state": "running" if running else state.status,
            "current_job": state.current_job,
            "progress_pct": state.progress_pct,
            "queued": state.queued,
            "approved": state.approved,
            "published": state.published,
            "engine": state.engine,
            "demo": state.demo,
            "last_error": state.last_error,
            "last_run_at": state.last_run_at,
            "niche": normalize_niche(config.niche),
            "niche_label": niche_label(config.niche),
            "auto_post": config.auto_post,
            "rights_confirmed": config.rights_confirmed,
            "rights_notice": RIGHTS_NOTICE,
            "trending_video_id": state.trending_video_id,
            "trending_title": state.trending_title,
        }

    # ------------------------------------------------------------ start/stop
    def start(self, config: AgentConfig | None = None) -> dict:
        """Start a run in a background thread. Raises AgentError plainly."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise AgentError("The agent is already running — stop it first.")
            cfg = config or self.store.load_config()
            if not cfg.enabled:
                raise AgentError("The agent is off — turn it on in the agent settings first.")
            cfg.validate()  # rights flag, source URL, niche, platforms
            self.store.save_config(cfg)
            self._stop.clear()
            self._set_state(
                status="running",
                current_job=None,
                progress_pct=0.0,
                queued=0,
                approved=0,
                published=0,
                last_error=None,
                last_run_at=_utcnow_iso(),
            )
            thread = threading.Thread(
                target=self._run_thread, args=(cfg,), daemon=True, name="clipforge-agent"
            )
            self._thread = thread
            thread.start()
            return self.status()

    def start_trending(self, config: AgentConfig) -> dict:
        """Start a one-off trending run in a background thread.

        Like :meth:`start`, but the config is NOT saved over the user's
        stored agent config — a trending run is a single loop, not a
        new standing setup. The trending video id / title are recorded
        in the run state so the UI can show what the run was inspired
        by. Raises AgentError plainly (incl. the trending guardrail).
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise AgentError("The agent is already running — stop it first.")
            if not config.enabled:
                raise AgentError("The agent is off — turn it on in the agent settings first.")
            config.validate()  # rights flag, source URL, trending guardrail
            self._stop.clear()
            self._set_state(
                status="running",
                current_job=None,
                progress_pct=0.0,
                queued=0,
                approved=0,
                published=0,
                last_error=None,
                last_run_at=_utcnow_iso(),
                trending_video_id=config.trending_video_id,
                trending_title=config.trending_title,
            )
            thread = threading.Thread(
                target=self._run_thread, args=(config,), daemon=True, name="clipforge-agent"
            )
            self._thread = thread
            thread.start()
            return self.status()

    def stop(self) -> dict:
        """Ask a running agent to stop; idle agents just report stopped."""
        with self._lock:
            self._stop.set()
            state = self.store.load_state()
            if state.status == "running":
                self._set_state(status="stopped", progress_pct=state.progress_pct)
            elif state.status not in {"stopped", "done", "error"}:
                self._set_state(status="stopped")
            return self.status()

    def _run_thread(self, config: AgentConfig) -> None:
        try:
            self.run_once(config)
        except AgentError as exc:
            self._set_state(status="error", last_error=str(exc))
        except Exception as exc:  # noqa: BLE001 — thread must not die silently
            self._set_state(status="error", last_error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ the run
    def run_once(self, config: AgentConfig | None = None, *, client=None) -> dict:
        """One synchronous agent run: source -> clips -> SEO -> approval queue.

        Returns the state dict. Raises AgentError (plain language) when
        the run cannot start or the source cannot be read; a run that
        produces no qualifying clips ends in state "error" with the
        reason in ``last_error`` instead of raising.
        """
        cfg = config or self.store.load_config()
        if not cfg.enabled:
            raise AgentError("The agent is off — turn it on in the agent settings first.")
        cfg.validate()
        engine, provider = resolve_engine(self._settings())
        self._set_state(
            status="running",
            progress_pct=5.0,
            engine=engine,
            demo=bool(cfg.transcript_text),
            last_error=None,
            last_run_at=_utcnow_iso(),
            trending_video_id=cfg.trending_video_id,
            trending_title=cfg.trending_title,
        )
        session: Session = self._session_factory()
        try:
            job, candidates, demo = self._process_source(session, cfg, engine)
            self._set_state(current_job=job.id, progress_pct=60.0)
            queued, approved, published = self._queue_posts(
                session, job, candidates, cfg, engine, provider, client=client
            )
            session.commit()
            self._set_state(
                status="done",
                progress_pct=100.0,
                queued=queued,
                approved=approved,
                published=published,
                demo=demo,
            )
            return self.status()
        except AgentError as exc:
            session.rollback()
            self._set_state(status="error", last_error=str(exc))
            raise
        finally:
            session.close()

    def _process_source(
        self, session: Session, cfg: AgentConfig, engine: str
    ) -> tuple[Job, list, bool]:
        """Fetch/transcribe/score the source. Returns (job, candidates, demo)."""
        niche = normalize_niche(cfg.niche)
        if cfg.transcript_text:
            # Demo / test path: supplied text, no download, no FFmpeg.
            transcript = fallback_transcript_from_text(
                cfg.transcript_text, duration_s=max(60.0, len(cfg.transcript_text.split()) * 0.5)
            )
            job = Job(
                source_type="youtube",
                source_url=cfg.source_url,
                title=f"Agent run — {niche_label(cfg.niche)}",
                own_content=True,
                status=JobStatus.REVIEW.value,
                meta_json={
                    "agent": True,
                    "demo": True,
                    "niche": niche,
                    "engine": engine,
                    "top_n": cfg.top_n,
                    **_trending_meta(cfg),
                },
            )
            session.add(job)
            session.commit()
            found = find_candidates(
                transcript, top_n=max(cfg.top_n, 10), use_llm=False, niche=niche
            )
            candidates = self._store_candidates(session, job, found, transcript, cfg.top_n)
            if not candidates:
                raise AgentError(
                    "No clips scored above the threshold for this source — "
                    "try a longer video or a different niche."
                )
            return job, candidates, True
        # Real source: channel -> latest video, then the full pipeline.
        video_url = cfg.source_url
        if cfg.source_type == "channel":
            video_url = _latest_channel_video_url(cfg.source_url)
        if self._stop.is_set():
            raise AgentError("Agent stopped before the source was fetched.")
        return self._run_pipeline(session, cfg, video_url, engine)

    def _run_pipeline(
        self, session: Session, cfg: AgentConfig, video_url: str, engine: str
    ) -> tuple[Job, list, bool]:
        """Real video path: the existing worker pipeline (ingest -> score).

        The worker needs yt-dlp / FFmpeg; when a dependency is missing
        the job goes blocked and the agent reports the missing thing in
        plain language instead of failing silently.
        """
        from clipforge.worker import run_job  # noqa: PLC0415  (avoid import cycle)

        job = Job(
            source_type="youtube",
            source_url=video_url,
            title=f"Agent run — {niche_label(cfg.niche)}",
            own_content=True,
            status=JobStatus.PENDING.value,
            meta_json={
                "agent": True,
                "demo": False,
                "niche": normalize_niche(cfg.niche),
                "engine": engine,
                "top_n": cfg.top_n,
                **_trending_meta(cfg),
            },
        )
        session.add(job)
        session.commit()
        summary = run_job(session, job.id)
        session.refresh(job)
        if job.status in {JobStatus.BLOCKED.value, JobStatus.FAILED.value}:
            reason = job.error or summary.get("error") or "the pipeline stopped"
            raise AgentError(f"Could not process the video: {reason}")
        candidates = (
            session.query(ClipCandidate)
            .filter_by(job_id=job.id)
            .order_by(ClipCandidate.n.asc())
            .all()
        )
        if not candidates:
            raise AgentError(
                "No clips scored above the threshold for this video — "
                "try a longer video or a different niche."
            )
        return job, candidates, False

    def _store_candidates(self, session: Session, job: Job, found, transcript, top_n: int) -> list:
        """Persist the top-N scored candidates on the job (demo path)."""
        words = transcript.words
        rows: list[ClipCandidate] = []
        for rank, cand in enumerate(found[:top_n], start=1):
            excerpt = " ".join(w.text for w in words[cand.start_word_index : cand.end_word_index])[
                :400
            ]
            row = ClipCandidate(
                job_id=job.id,
                n=rank,
                start_s=cand.start_s,
                end_s=cand.end_s,
                score=cand.score,
                hook_rating=float(cand.hook),
                hook_line=cand.hook_line,
                reason=cand.reason,
                transcript_excerpt=excerpt,
                status="pending",
            )
            session.add(row)
            rows.append(row)
        session.commit()
        return rows

    def _queue_posts(
        self,
        session: Session,
        job: Job,
        candidates,
        cfg: AgentConfig,
        engine: str,
        provider,
        *,
        client=None,
    ) -> tuple[int, int, int]:
        """SEO per platform (engine-aware) + posts queued for approval.

        Returns (queued, approved, published). Approval-first: posts
        start ``pending_approval``. Only when ``auto_post`` is on are
        they approved and published by the agent itself — and live
        publishing still needs dry-run off + credentials.
        """
        niche = normalize_niche(cfg.niche)
        dry_run = self._settings().effective_dry_run(_env_settings.dry_run)
        # Trending runs: SEO is tuned to the trending topic, not just the
        # clip's own words — the trending title seeds the keywords so
        # titles / descriptions / hashtags ride the topic that is working.
        topic_text = (cfg.trending_topic or cfg.trending_title or "").strip()
        scheduled_for = _parse_scheduled_for(cfg.scheduled_for)
        queued = approved = published = 0
        for cand in candidates[: cfg.top_n]:
            if self._stop.is_set():
                raise AgentError("Agent stopped while queueing posts.")
            keywords = extract_keywords(
                f"{topic_text} {cand.hook_line} {cand.transcript_excerpt or ''}", niche
            )
            keyword = keywords[0] if keywords else niche_label(niche)
            for platform in cfg.platforms:
                variant = generate_copy_with_engine(
                    engine,
                    provider,
                    keyword,
                    cand.hook_line or keyword,
                    niche,
                    platform,
                    cand.duration_s or 30.0,
                    client=client,
                )
                session.add(
                    CandidateSeo(
                        clip_candidate_id=cand.id,
                        platform=platform,
                        seo_json=variant,
                        generated_by="agent",
                    )
                )
                post = Post(
                    clip_candidate_id=cand.id,
                    platform=platform,
                    status=PostStatus.PENDING_APPROVAL.value,
                    seo_json=variant,
                    video_path=cand.file_path,
                    video_url=job.source_url,
                    scheduled_for=scheduled_for,
                )
                session.add(post)
                session.flush()
                queued += 1
                if cfg.auto_post:
                    approved += 1
                    post.status = PostStatus.APPROVED.value
                    if self._publish_one(session, post, dry_run):
                        published += 1
            total_posts = max(1, cfg.top_n * len(cfg.platforms))
            self._set_state(progress_pct=60.0 + 35.0 * (queued / total_posts))
        session.commit()
        return queued, approved, published

    def _publish_one(self, session: Session, post: Post, dry_run: bool) -> bool:
        """Publish one auto-approved post. True when it (dry-)published.

        Any adapter failure marks the post failed with a plain message
        — it never raises out of the agent run.
        """
        seo = post.seo_json or {}
        title = seo.get("title") or "ClipForge clip"
        description = seo.get("description") or seo.get("caption") or title
        try:
            if post.platform == "youtube":
                result = youtube_adapter.publish(
                    title=title,
                    description=description,
                    video_path=post.video_path or "",
                    publish_at=post.scheduled_for,
                    dry_run=dry_run,
                )
            elif post.platform == "instagram":
                result = meta_adapter.publish_instagram(
                    caption=description,
                    video_url=post.video_url,
                    video_path=post.video_path,
                    dry_run=dry_run,
                )
            elif post.platform == "facebook":
                result = meta_adapter.publish_facebook(
                    caption=description,
                    video_url=post.video_url,
                    video_path=post.video_path,
                    dry_run=dry_run,
                )
            elif post.platform == "tiktok":
                result = tiktok_adapter.publish_tiktok(
                    title=title,
                    video_url=post.video_url,
                    video_path=post.video_path,
                    mode="upload",
                    dry_run=dry_run,
                )
            else:
                return False
        except Exception as exc:  # noqa: BLE001 — adapter failure = failed post
            post.status = PostStatus.FAILED.value
            post.error = f"{type(exc).__name__}: {exc}"
            session.commit()
            return False
        post.publish_result_json = result.to_dict()
        post.attempts += 1
        if result.status == "dry_run":
            post.status = PostStatus.DRY_RUN.value
            post.published_at = datetime.now(UTC)
        elif result.status in {"published", "draft_created"}:
            post.status = PostStatus.PUBLISHED.value
            post.published_at = datetime.now(UTC)
        else:
            post.status = PostStatus.FAILED.value
            post.error = result.error
        session.commit()
        return post.status in {PostStatus.DRY_RUN.value, PostStatus.PUBLISHED.value}


def _parse_scheduled_for(value: str | None) -> datetime | None:
    """ISO timestamp -> aware datetime for Post.scheduled_for.

    Raises AgentError in plain language on a bad value (the router
    validates first, so this is a backstop, not the user-facing check).
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentError(f"scheduled_for is not a valid date/time: {value!r}.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ------------------------------------------------------------ shared runner
_RUNNERS: dict[str, Agent] = {}


def get_agent(
    data_dir: str | Path,
    session_factory,
    settings_store: SettingsStore | None = None,
) -> Agent:
    """The one shared Agent per data dir (start/stop share the thread)."""
    key = str(Path(data_dir).resolve())
    agent = _RUNNERS.get(key)
    if agent is None:
        agent = Agent(data_dir, session_factory, settings_store)
        _RUNNERS[key] = agent
    return agent


def accounts_summary(db: Session, state: dict) -> list[dict]:
    """Per-account roll-up for GET /api/agent/status.

    One row per connected account (latest SocialAccount per platform),
    plus a row for each platform with queued posts but no account yet.
    Counts come from the posts table — never guessed.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for account in db.query(SocialAccount).order_by(SocialAccount.created_at.desc()).all():
        if account.provider in seen:
            continue
        seen.add(account.provider)
        rows.append(_account_row(db, account.provider, account, state))
    for platform in PLATFORMS:
        if platform in seen:
            continue
        count = db.query(Post).filter_by(platform=platform).count()
        if count:
            rows.append(_account_row(db, platform, None, state))
    return rows


def _account_row(db: Session, platform: str, account: SocialAccount | None, state: dict) -> dict:
    queued = (
        db.query(Post)
        .filter_by(platform=platform, status=PostStatus.PENDING_APPROVAL.value)
        .count()
    )
    approved = db.query(Post).filter_by(platform=platform, status=PostStatus.APPROVED.value).count()
    published = (
        db.query(Post).filter_by(platform=platform, status=PostStatus.PUBLISHED.value).count()
        + db.query(Post).filter_by(platform=platform, status=PostStatus.DRY_RUN.value).count()
    )
    return {
        "platform": platform,
        "account": (account.label or account.account_ref) if account else "",
        "status": (account.status if account else "not_connected"),
        "progress_pct": float(state.get("progress_pct") or 0.0),
        "queued": queued,
        "approved": approved,
        "published": published,
    }


__all__ = [
    "AGENT_FILE",
    "Agent",
    "AgentConfig",
    "AgentError",
    "AgentState",
    "AgentStore",
    "PRESET_NAMES",
    "RIGHTS_NOTICE",
    "accounts_summary",
    "get_agent",
]
