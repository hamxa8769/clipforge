// Trending page (v0.7.0) — live niche trending search + CC-licensed
// discovery + live clipping, and the full loop back into the Agent.
//
//   - Pick a niche (presets + free text) -> live YouTube search for
//     videos trending in that niche right now (search.list by niche
//     keywords, then videos.list for views / likes / published date),
//     ranked by trend score.
//   - "Licensed only (CC)" toggle (DEFAULT ON, remembered in
//     localStorage — the server has no settings field for it yet): when
//     ON, only Creative Commons videos are shown (an opt-in discovery
//     filter). When OFF, all trending videos show — every result says
//     plainly what licence it has, and any video can be clipped.
//     Advisory since v0.9.1: the licence is reported, never enforced —
//     the v0.7.0 hard block was removed at the user's explicit
//     instruction after they checked YouTube's terms. Videos whose
//     licence the search did not verify show "Licence not checked" with
//     a per-video check instead of a guessed verdict. The toggle is
//     never forced on: the user's last choice is remembered.
//   - "Check a video URL": paste a YouTube URL -> GET /api/videos/license
//     -> licence + reason, with "Clip this video" always available.
//   - Pick a trending video as a TOPIC -> the agent matches it to YOUR
//     OWN source video on that topic (own video URL, or a channel URL
//     matched by title overlap) -> transcribe -> score -> clip -> SEO
//     tuned to the trend -> approval queue -> schedule / publish. Or,
//     "Clip this video" creates a clip job from THAT video (POST /jobs
//     with the licence — own video or Creative Commons, with
//     attribution). /api/trending/run is NOT used for this: it refuses
//     the trending video itself as a source.
//   - "Live now" tab: live streams in the niche (GET /api/trending/live),
//     with viewers + started time + licence badge, and "Capture & clip"
//     (pick a capture duration, 60–300s) for live videos
//     (POST /api/trending/live/capture).
//   - Live progress: polls GET /api/trending/status while the run is
//     going, then links to the pipeline and the approval queue.
//
// Rights notice (advisory since v0.9.1, stated on the page): make sure
// you have the right to use a video — your own video, a licensed video,
// or a use YouTube permits. You are responsible for the rights to what
// you post. ClipForge does not guarantee rights clearance. A trending
// video URL is still refused as a clip source (422) — that is the
// topic-vs-source rule, not a licence verdict.
//
// Until the endpoints land, the page says so plainly and the search
// controls stay disabled — it never shows invented videos. With no
// YouTube connection and no API key, the search shows the exact
// needs-connection state instead of fake results.
"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  checkVideoLicense, createYoutubeClipJob, getJob, getTrendingCredentials, getTrendingJob, getTrendingStatus,
  jobSourceLicense, licenseBadgeLabel, matchTrendingTopic, RIGHTS_ADVISORY, saveYoutubeApiKey, searchLive, searchTrending,
  startLiveCapture, startTrendingRun, NICHE_PRESETS, type LicenseCheck, type TrendingCredentials, type TrendingJob,
  type TrendingVideo, type VideoLicense,
} from "@/lib/api";
import {
  Card, CardHeader, DemoBadge, EmptyState, ErrorBanner, PrimaryButton,
  ProgressBar, SecondaryButton, Skeleton, Toggle, fmtCompact,
} from "@/components/ui";
import { openExternal } from "@/lib/tauri";
import { useServerStatus } from "@/components/ServerStatusProvider";

const GUARDRAIL = RIGHTS_ADVISORY;

const LICENSED_ONLY_KEY = "clipforge:licensed-only";

function fmtDate(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function fmtStarted(iso: string | null): string {
  if (!iso) return "started —";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "started —";
  const mins = Math.max(0, Math.round((Date.now() - d.getTime()) / 60000));
  if (mins < 1) return "started just now";
  if (mins < 60) return `started ${mins} min ago`;
  const hours = Math.floor(mins / 60);
  return `started ${hours}h ${mins % 60}m ago`;
}

function scoreTone(score: number): string {
  if (score >= 70) return "bg-emerald-500/15 text-emerald-300";
  if (score >= 40) return "bg-amber-500/15 text-amber-300";
  return "bg-neutral-700/50 text-neutral-300";
}

function licenseTone(license: VideoLicense): string {
  if (license === "cc") return "bg-emerald-500/15 text-emerald-300 border-emerald-500/40";
  if (license === "own") return "bg-sky-500/15 text-sky-300 border-sky-500/40";
  if (license === "standard") return "bg-neutral-700/50 text-neutral-300 border-neutral-600";
  return "bg-neutral-700/50 text-neutral-400 border-neutral-700";
}

function LicenseBadge({ license }: { license: VideoLicense }) {
  return (
    <span className={`shrink-0 rounded-full border px-2 py-0.5 text-xs font-medium ${licenseTone(license)}`}>
      {licenseBadgeLabel(license)}
    </span>
  );
}

function ClipVerdict({ video }: { video: TrendingVideo }) {
  // Advisory since v0.9.1: every video can be clipped — the licence is
  // reported, never a refusal.
  if (!video.license_checked) {
    return (
      <p className="mt-1 text-xs text-amber-200">
        Licence not checked yet — check it to see its licence. The search only verifies licences when “Licensed only” is on.
      </p>
    );
  }
  return (
    <p className="mt-1 text-xs font-medium text-emerald-300">
      ✓ Can clip{video.license === "cc" ? " — Creative Commons — attribution is added automatically for CC sources." : video.license === "own" ? " — your video." : video.license === "public-domain" ? " — public domain." : video.license === "standard" ? ` — ${video.reason}` : "."}
    </p>
  );
}

const CAPTURE_DURATIONS = [60, 120, 180, 300];

export default function TrendingPage() {
  const [creds, setCreds] = useState<TrendingCredentials | null>(null);
  // Liveness is the shared server status (v0.9.3).
  const server = useServerStatus();
  const live = server.live;
  const [loaded, setLoaded] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  // "Licensed only (CC)" — DEFAULT ON, remembered in localStorage.
  // (The server has no settings field for this yet, so localStorage is
  // the store — the choice survives restarts on this device, and the
  // toggle is never forced on.)
  const [licensedOnly, setLicensedOnly] = useState(true);

  // Search state.
  const [tab, setTab] = useState<"trending" | "live">("trending");
  const [niche, setNiche] = useState("");
  const [customNiche, setCustomNiche] = useState("");
  const [results, setResults] = useState<TrendingVideo[]>([]);
  const [liveResults, setLiveResults] = useState<TrendingVideo[]>([]);
  const [searchSource, setSearchSource] = useState("");
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);

  // Licence check (paste a URL).
  const [checkUrl, setCheckUrl] = useState("");
  const [checkResult, setCheckResult] = useState<LicenseCheck | null>(null);
  const [checkError, setCheckError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  // API-key form.
  const [apiKey, setApiKey] = useState("");
  const [savingKey, setSavingKey] = useState(false);

  // Trending -> agent flow.
  const [picked, setPicked] = useState<TrendingVideo | null>(null);
  // When set, the clip is cut from THIS video (own video or CC) instead of
  // from the user's own source video on the topic.
  const [ccSource, setCcSource] = useState<TrendingVideo | null>(null);
  const [ownSourceType, setOwnSourceType] = useState<"video" | "channel">("video");
  const [ownSourceUrl, setOwnSourceUrl] = useState("");
  const [matched, setMatched] = useState<{ url: string; title: string; score: number } | null>(null);
  const [matching, setMatching] = useState(false);
  const [rights, setRights] = useState(false);
  const [autoPost, setAutoPost] = useState(false);
  const [scheduledFor, setScheduledFor] = useState("");
  const [starting, setStarting] = useState(false);

  // Live capture state.
  const [captureDurations, setCaptureDurations] = useState<Record<string, number>>({});
  const [capture, setCapture] = useState<{ videoId: string; title: string; jobId: string | null; status: string; progress: number; error: string | null } | null>(null);
  const [capturing, setCapturing] = useState(false);

  // Live run progress.
  const [runState, setRunState] = useState<string>("");
  const [runProgress, setRunProgress] = useState(0);
  const [runQueued, setRunQueued] = useState(0);
  const [runJobId, setRunJobId] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [runJob, setRunJob] = useState<TrendingJob | null>(null);
  // Clip job created from a CC / own video (POST /jobs).
  const [clipJobId, setClipJobId] = useState<string | null>(null);

  const nicheValue = niche === "__custom" ? customNiche.trim() : niche;

  const loadCreds = useCallback(async () => {
    const fetched = await getTrendingCredentials();
    setCreds(fetched);
    setLoaded(true);
  }, []);

  useEffect(() => { loadCreds().catch(() => setLoaded(true)); }, [loadCreds]);

  // When the server comes back after being down, reload on our own.
  useEffect(() => { if (live) loadCreds().catch(() => setLoaded(true)); }, [live, loadCreds]);

  // Remembered "Licensed only" choice: localStorage (default ON).
  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(LICENSED_ONLY_KEY);
      if (stored === "false") setLicensedOnly(false);
      // Default is ON: stored "true" or nothing both mean ON.
    } catch { /* private mode — default ON stands */ }
  }, []);

  function onToggleLicensedOnly(value: boolean) {
    setLicensedOnly(value);
    try { window.localStorage.setItem(LICENSED_ONLY_KEY, String(value)); } catch { /* ignore */ }
  }

  // Live status refresh while a trending run is going.
  const running = runState === "running";
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(async () => {
      const s = await getTrendingStatus().catch(() => null);
      if (!s) return;
      setRunState(s.state);
      setRunProgress(s.progress_pct);
      setRunQueued(s.queued);
      if (s.current_job) setRunJobId(s.current_job);
      if (s.last_error) setRunError(s.last_error);
    }, 5000);
    return () => window.clearInterval(t);
  }, [running]);

  // Live capture progress: poll the capture job while it is running.
  const captureRunning = capture !== null && capture.jobId !== null && (capture.status === "running" || capture.status === "capturing" || capture.status === "pending");
  useEffect(() => {
    if (!captureRunning || !capture?.jobId) return;
    const t = window.setInterval(async () => {
      const j = await getJob(capture.jobId as string).catch(() => null);
      if (!j) return;
      setCapture((c) => c ? { ...c, status: j.status, progress: j.progress_pct } : c);
    }, 5000);
    return () => window.clearInterval(t);
  }, [captureRunning, capture?.jobId]);

  // Once the run finishes, load the job record (posts + provider ids).
  useEffect(() => {
    if (runState !== "done" || !runJobId) return;
    getTrendingJob(runJobId).then((j) => { if (j) setRunJob(j); }).catch(() => {});
  }, [runState, runJobId]);

  async function onSearch(eventType: "trending" | "live") {
    if (!nicheValue) { setSearchError("Pick a niche first."); return; }
    setSearching(true);
    setSearchError(null);
    if (eventType === "trending") setResults([]); else setLiveResults([]);
    setPicked(null);
    setCcSource(null);
    // Live streams come from their own endpoint (GET /api/trending/live),
    // not event_type on search.
    const { result, error } = eventType === "live"
      ? await searchLive(nicheValue, 10, { licensedOnly })
      : await searchTrending(nicheValue, 10, { licensedOnly });
    setSearching(false);
    if (error || !result) {
      setSearchError(error ?? "Trending search failed.");
      return;
    }
    // Defence in depth: if an older server ignored licensed_only, filter
    // client-side so ON really means only Creative Commons videos.
    const shown = licensedOnly
      ? result.results.filter((v) => v.license === "cc" || v.license === "own" || v.license === "public-domain")
      : result.results;
    if (eventType === "trending") setResults(shown); else setLiveResults(shown);
    setSearchSource(result.source === "oauth" ? `your connected YouTube channel${result.account ? ` (${result.account})` : ""}` : "your saved YouTube Data API key");
    if (shown.length === 0) {
      setSearchError(licensedOnly
        ? "No Creative Commons licensed videos found trending in this niche in the last 7 days — try a broader niche, or turn “Licensed only” off to see all trending videos."
        : "No trending videos found in this niche in the last 7 days — try a broader niche.");
    }
  }

  async function onCheckUrl() {
    const url = checkUrl.trim();
    if (!url) { setCheckError("Paste a YouTube video URL first."); return; }
    if (!/youtu\.?be/.test(url)) { setCheckError("That does not look like a YouTube URL — paste a youtube.com or youtu.be link."); return; }
    setChecking(true);
    setCheckError(null);
    setCheckResult(null);
    const { check, error } = await checkVideoLicense(url);
    setChecking(false);
    if (error || !check) {
      setCheckError(error ?? "Licence check failed.");
      return;
    }
    setCheckResult(check);
  }

  function clipVideoFromCheck(check: LicenseCheck): TrendingVideo {
    return {
      video_id: check.video_id, title: check.title, channel_title: check.channel, channel_id: "",
      published_at: "", view_count: 0, like_count: 0, comment_count: 0, trend_score: 0,
      url: check.url || `https://www.youtube.com/watch?v=${check.video_id}`,
      license: check.license, license_key: check.license_key, license_checked: true,
      license_url: check.license_url, can_clip: check.can_clip,
      reason: check.reason, concurrent_viewers: null, started_at: null, event_type: "video",
    };
  }

  // Per-result licence check (for videos the search did not verify):
  // updates the result in place with the real verdict.
  const [checkingVideo, setCheckingVideo] = useState<string | null>(null);
  async function onCheckResultVideo(video: TrendingVideo) {
    setCheckingVideo(video.video_id);
    const { check, error } = await checkVideoLicense(video.url);
    setCheckingVideo(null);
    if (error || !check) {
      setNotice(error ?? "Licence check failed.");
      return;
    }
    const updated = clipVideoFromCheck(check);
    const patch = (vs: TrendingVideo[]) => vs.map((v) => (v.video_id === video.video_id ? { ...v, ...updated } : v));
    setResults(patch);
    setLiveResults(patch);
    if (picked?.video_id === video.video_id) setPicked(updated);
    setNotice(`“${check.title}” can be clipped — ${check.reason}`);
  }

  async function onClipVideo(video: TrendingVideo) {
    // Unchecked video (search without licensed_only): check the licence
    // first so the clip carries the true licence record — the check
    // reports the licence, it never refuses (advisory since v0.9.1).
    if (!video.license_checked) {
      setNotice("Checking this video's licence first…");
      const { check, error } = await checkVideoLicense(video.url);
      if (error || !check) {
        setNotice(error ?? "Licence check failed — the clip was not started.");
        return;
      }
      const updated = clipVideoFromCheck(check);
      const patch = (vs: TrendingVideo[]) => vs.map((v) => (v.video_id === video.video_id ? { ...v, ...updated } : v));
      setResults(patch);
      setLiveResults(patch);
      video = updated;
    }
    setPicked(video);
    setCcSource(video);
    setMatched(null);
    setRights(false);
    setRunJob(null);
    setRunState("");
    setClipJobId(null);
    setNotice(`Clipping from “${video.title}” (${licenseBadgeLabel(video.license)}). Attribution will be added to the description automatically for Creative Commons sources.`);
  }

  async function onCapture(video: TrendingVideo) {
    const durationS = captureDurations[video.video_id] ?? 120;
    setCapturing(true);
    setNotice(null);
    const res = await startLiveCapture(video.video_id, durationS);
    setCapturing(false);
    if (!res.ok) {
      setCapture({ videoId: video.video_id, title: video.title, jobId: null, status: "error", progress: 0, error: res.error ?? "Live capture is not available in this build yet." });
      return;
    }
    const data = (res.data ?? {}) as Record<string, unknown>;
    const jobId = typeof data.job_id === "string" ? data.job_id : (typeof data.id === "string" ? data.id : null);
    setCapture({ videoId: video.video_id, title: video.title, jobId, status: typeof data.status === "string" ? data.status : "capturing", progress: 5, error: null });
    setNotice(`Capturing “${video.title}” for ${durationS}s — the clip job will appear below and in the pipeline.`);
  }

  async function onSaveKey() {
    if (!apiKey.trim()) { setNotice("Paste a YouTube Data API key first (Google Cloud Console > APIs & Services > Credentials)."); return; }
    setSavingKey(true);
    setNotice(null);
    const res = await saveYoutubeApiKey(apiKey.trim());
    setSavingKey(false);
    if (!res.ok) { setNotice(res.error ?? "Could not save the API key."); return; }
    setApiKey("");
    setNotice("YouTube Data API key saved — trending search can use it now.");
    await loadCreds();
  }

  async function onMatch() {
    if (!picked) return;
    if (!ownSourceUrl.trim()) { setNotice("Add your own channel URL first."); return; }
    setMatching(true);
    setNotice(null);
    const { match, error } = await matchTrendingTopic(ownSourceUrl.trim(), picked.video_id, picked.title);
    setMatching(false);
    if (error || !match) { setNotice(error ?? "Could not match the channel."); return; }
    setMatched({ url: match.own_video_url, title: match.own_title, score: match.match_score });
  }

  async function onStartRun() {
    if (!picked) return;
    if (!rights) {
      setNotice(ccSource
        ? "Confirm the licence acknowledgement first — Creative Commons clips always carry attribution in the description."
        : "Confirm the rights checkbox first — the agent only works with videos you own or have the rights to use.");
      return;
    }
    // Clipping a video goes through POST /jobs — NOT /api/trending/run,
    // which refuses the trending video itself as a source. The clips
    // land in the approval queue.
    if (ccSource) {
      setStarting(true);
      setNotice(null);
      setClipJobId(null);
      const res = await createYoutubeClipJob({
        source_url: ccSource.url,
        title: ccSource.title,
        source_license: jobSourceLicense(ccSource),
        source_title: ccSource.title,
        source_channel: ccSource.channel_title,
        own_content_declaration: ccSource.license === "own",
      });
      setStarting(false);
      if (!res.ok) {
        setNotice(res.error ?? "Could not start the clip job.");
        return;
      }
      const data = (res.data ?? {}) as Record<string, unknown>;
      const jobId = typeof data.id === "string" ? data.id : null;
      setClipJobId(jobId);
      setNotice(jobId
        ? `Clip job ${jobId} started — cutting clips from the ${licenseBadgeLabel(ccSource.license)} video now (attribution included). Find the clips in the approval queue when they are ready.`
        : "Clip job started — cutting clips from the video now (attribution included).");
      return;
    }
    const sourceUrl = ownSourceType === "channel" && matched ? matched.url : ownSourceUrl.trim();
    if (!sourceUrl) { setNotice("Add your own source video first (or match your channel to the topic)."); return; }
    setStarting(true);
    setNotice(null);
    setRunError(null);
    setRunJob(null);
    const res = await startTrendingRun({
      niche: nicheValue || "general",
      trending_video_id: picked.video_id,
      trending_title: picked.title,
      own_source_url: sourceUrl,
      own_source_type: ownSourceType === "channel" && matched ? "video" : ownSourceType,
      rights_confirmed: rights,
      top_n: 3,
      platforms: ["youtube"],
      auto_post: autoPost,
      scheduled_for: scheduledFor ? new Date(scheduledFor).toISOString() : null,
    });
    setStarting(false);
    if (!res.ok) { setNotice(res.error ?? "Could not start the trending run."); return; }
    setRunState("running");
    setRunProgress(5);
    setRunQueued(0);
    setNotice("Trending run started — the agent is working through your own video now.");
  }

  if (!loaded) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" /><Skeleton className="h-48" /><Skeleton className="h-64" />
      </div>
    );
  }

  const canSearch = live && (creds?.usable ?? false) && nicheValue.length > 0;
  const shownResults = tab === "live" ? liveResults : results;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Trending</h1>
        <p className="mt-1 text-sm text-neutral-400">
          What is trending in your niche right now, live from YouTube — then the agent turns the topic into clips from your own videos, or clips a Creative Commons video directly.
        </p>
      </div>

      <div className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-100" role="note">
        {GUARDRAIL}
      </div>

      {!live && <DemoBadge />}
      {notice && (
        <div className="rounded-xl border border-sky-500/40 bg-sky-500/10 p-4 text-sm text-sky-100">{notice}</div>
      )}

      <Card>
        <CardHeader title="YouTube connection" hint="Trending search needs one of the two" />
        {!live ? (
          <EmptyState
            title="Trending service not installed yet"
            body="The Trending page is ready, but the trending service itself has not landed in this build. Once it does, this card shows whether your YouTube channel or API key is connected."
          />
        ) : creds ? (
          <div className="space-y-4">
            <p className="text-sm text-neutral-300">{creds.message}</p>
            <ul className="space-y-1 text-sm text-neutral-400">
              <li>
                YouTube channel:{" "}
                <span className="font-medium text-neutral-100">
                  {creds.oauth_connected ? `Connected${creds.oauth_account ? ` (${creds.oauth_account})` : ""}` : creds.oauth_expired ? "Expired — reconnect it" : "Not connected"}
                </span>
              </li>
              <li>
                YouTube Data API key:{" "}
                <span className="font-medium text-neutral-100">
                  {creds.api_key_configured ? `Saved (…${creds.api_key_last4}, ${creds.api_key_source})` : "Not saved"}
                </span>
              </li>
            </ul>
            {!creds.usable && (
              <p className="text-sm text-neutral-400">
                Connect your channel on the <Link className="text-amber-300 underline" href="/channels">Channels page</Link>, or save an API key below — search stays off until one of them is set up.
              </p>
            )}
            <div className="flex flex-wrap items-end gap-2">
              <label className="min-w-64 flex-1">
                <span className="text-xs font-medium text-neutral-300">YouTube Data API key (optional if a channel is connected)</span>
                <input
                  type="password"
                  className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="AIza…"
                  aria-label="YouTube Data API key"
                  autoComplete="off"
                />
              </label>
              <PrimaryButton onClick={onSaveKey} disabled={savingKey}>
                {savingKey ? "Saving…" : "Save API key"}
              </PrimaryButton>
            </div>
            <p className="text-xs text-neutral-500">
              The key is stored in the OS keyring on this machine and is never shown again. Get one at Google Cloud Console → APIs &amp; Services → Credentials → Create API key (enable the YouTube Data API v3).
            </p>
          </div>
        ) : (
          <p className="text-sm text-neutral-400">Could not read the connection status.</p>
        )}
      </Card>

      <Card>
        <CardHeader title="Search trending in a niche" hint="Live from YouTube Data API v3 — last 7 days, most views first" />
        <div className="space-y-4">
          <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-4">
            <Toggle
              checked={licensedOnly}
              onChange={onToggleLicensedOnly}
              label="Licensed only (CC)"
              hint={licensedOnly
                ? "ON — only Creative Commons / licensed videos are shown. Turn it off any time to see all trending videos."
                : "OFF — showing all trending videos. Every result reports its licence; any video can be clipped."}
            />
            <p className="mt-2 text-xs text-neutral-500">
              Your choice is remembered on this device — the toggle is never forced on.
            </p>
          </div>

          <div className="flex gap-1 border-b border-neutral-800" role="tablist" aria-label="Trending or live">
            {(["trending", "live"] as const).map((t) => (
              <button
                key={t}
                type="button"
                role="tab"
                aria-selected={tab === t}
                onClick={() => setTab(t)}
                className={`rounded-t-md px-3 py-2 text-sm ${tab === t ? "bg-neutral-800 font-medium text-white" : "text-neutral-400 hover:text-white"}`}
              >
                {t === "trending" ? "Trending videos" : "🔴 Live now"}
              </button>
            ))}
          </div>

          <fieldset>
            <legend className="text-sm font-medium text-neutral-100">Niche</legend>
            <div className="mt-2 flex flex-wrap gap-2">
              {NICHE_PRESETS.map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => setNiche(n)}
                  aria-pressed={niche === n}
                  className={`rounded-full border px-3 py-1 text-sm capitalize ${niche === n ? "border-amber-400 bg-amber-400/10 text-neutral-100" : "border-neutral-700 bg-neutral-950 text-neutral-300 hover:border-neutral-500"}`}
                >
                  {n}
                </button>
              ))}
              <button
                type="button"
                onClick={() => setNiche("__custom")}
                aria-pressed={niche === "__custom"}
                className={`rounded-full border px-3 py-1 text-sm ${niche === "__custom" ? "border-amber-400 bg-amber-400/10 text-neutral-100" : "border-neutral-700 bg-neutral-950 text-neutral-300 hover:border-neutral-500"}`}
              >
                Other…
              </button>
            </div>
            {niche === "__custom" && (
              <input
                className="mt-2 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm"
                value={customNiche}
                onChange={(e) => setCustomNiche(e.target.value)}
                placeholder="Your niche — e.g. cricket highlights"
                aria-label="Custom niche"
              />
            )}
          </fieldset>
          <div className="flex flex-wrap gap-2">
            <PrimaryButton onClick={() => { void onSearch(tab); }} disabled={!live || searching || !canSearch}>
              {searching ? "Searching YouTube…" : tab === "live" ? "Search live now" : "Search trending"}
            </PrimaryButton>
            {live && !creds?.usable && (
              <span className="self-center text-sm text-amber-200">Connect your YouTube channel or save an API key above to search.</span>
            )}
          </div>
          {searchError && <ErrorBanner message={searchError} />}
          {shownResults.length > 0 && (
            <div className="space-y-3">
              <p className="text-xs text-neutral-500">
                Live results via {searchSource} · {tab === "live" ? "live streams, by concurrent viewers" : "ranked by trend score (views/hour + engagement)"} · {shownResults.length} video{shownResults.length === 1 ? "" : "s"}
                {licensedOnly && " · Licensed only (CC) — every result below can be clipped"}
              </p>
              <ul className="space-y-3">
                {shownResults.map((v) => (
                  <li key={v.video_id} className={`rounded-lg border p-4 ${picked?.video_id === v.video_id ? "border-amber-400 bg-amber-400/5" : "border-neutral-800"}`}>
                    <div className="flex flex-wrap items-start gap-3">
                      <div className="min-w-0 flex-1">
                        <p className="font-semibold text-neutral-50">
                          {tab === "live" && <span className="mr-2 rounded bg-red-600 px-1.5 py-0.5 text-[11px] font-bold text-white">LIVE</span>}
                          {v.title}
                        </p>
                        <p className="mt-0.5 text-sm text-neutral-400">
                          {v.channel_title} · {tab === "live" && v.concurrent_viewers !== null ? `${fmtCompact(v.concurrent_viewers)} watching now · ${fmtStarted(v.started_at)} · ` : `${fmtCompact(v.view_count)} views · ${fmtCompact(v.like_count)} likes · `}{fmtDate(v.published_at)}
                        </p>
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                          <LicenseBadge license={v.license} />
                          {v.license_url && (
                            <button type="button" className="text-xs text-sky-300 underline" onClick={() => { const url = v.license_url; if (url) void openExternal(url); }}>Licence details</button>
                          )}
                        </div>
                        <ClipVerdict video={v} />
                      </div>
                      {tab !== "live" && (
                        <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${scoreTone(v.trend_score)}`}>
                          Trend {v.trend_score}
                        </span>
                      )}
                    </div>
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <SecondaryButton href={v.url}>Watch on YouTube</SecondaryButton>
                      {tab === "live" ? (
                        <>
                          <label className="flex items-center gap-1 text-xs text-neutral-400">
                            Capture
                            <select
                              className="rounded-md border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-sm text-neutral-100"
                              value={captureDurations[v.video_id] ?? 120}
                              onChange={(e) => setCaptureDurations((d) => ({ ...d, [v.video_id]: Number(e.target.value) }))}
                              aria-label={`Capture duration for ${v.title}`}
                            >
                              {CAPTURE_DURATIONS.map((s) => (
                                <option key={s} value={s}>{s}s</option>
                              ))}
                            </select>
                          </label>
                          <PrimaryButton onClick={() => { void onCapture(v); }} disabled={capturing}>
                            {capturing ? "Starting capture…" : "Capture & clip"}
                          </PrimaryButton>
                        </>
                      ) : (
                        <>
                          <PrimaryButton onClick={() => { setPicked(v); setCcSource(null); setMatched(null); setRunJob(null); setRunState(""); setClipJobId(null); }}>
                            {picked?.video_id === v.video_id && !ccSource ? "Topic picked ✓" : "Use as topic"}
                          </PrimaryButton>
                          {!v.license_checked && (
                            <SecondaryButton onClick={() => { void onCheckResultVideo(v); }} disabled={checkingVideo === v.video_id}>
                              {checkingVideo === v.video_id ? "Checking…" : "Check licence"}
                            </SecondaryButton>
                          )}
                          <button
                            type="button"
                            onClick={() => { void onClipVideo(v); }}
                            title="Cut clips from this video"
                            className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500"
                          >
                            Clip this video
                          </button>
                          {!v.license_checked && (
                            <span className="text-xs text-neutral-500">Licence not checked — “Clip this video” checks it first.</span>
                          )}
                        </>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </Card>

      {capture && (
        <Card>
          <CardHeader title="Live capture" hint="Live progress" />
          <div className="space-y-3">
            <p className="text-sm text-neutral-300">
              Capturing: <span className="font-medium text-neutral-100">{capture.title}</span>
              {capture.jobId && <> · Job: <span className="font-mono text-sm">{capture.jobId}</span></>}
            </p>
            {capture.error ? (
              <ErrorBanner message={capture.error} />
            ) : (
              <>
                <ProgressBar value={capture.progress} />
                <p className="text-xs text-neutral-400">
                  Status: {capture.status} · {capture.progress}% — the captured clip appears in the pipeline and the approval queue when it is ready.
                </p>
                {capture.jobId && (
                  <div className="flex flex-wrap gap-2">
                    <SecondaryButton href={`/jobs/view/?id=${encodeURIComponent(capture.jobId)}`}>Open the capture job</SecondaryButton>
                    <SecondaryButton href="/queue">Approval queue</SecondaryButton>
                  </div>
                )}
              </>
            )}
          </div>
        </Card>
      )}

      <Card>
        <CardHeader title="Check a video URL" hint="Licence check — what licence does this video have?" />
        <div className="space-y-3">
          <p className="text-sm text-neutral-400">
            Paste any YouTube video URL to check its licence before clipping. {RIGHTS_ADVISORY}
          </p>
          <div className="flex flex-wrap gap-2">
            <input
              className="min-w-64 flex-1 rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm"
              value={checkUrl}
              onChange={(e) => { setCheckUrl(e.target.value); setCheckResult(null); setCheckError(null); }}
              placeholder="https://www.youtube.com/watch?v=…"
              aria-label="YouTube video URL to check"
            />
            <PrimaryButton onClick={() => { void onCheckUrl(); }} disabled={checking}>
              {checking ? "Checking…" : "Check licence"}
            </PrimaryButton>
          </div>
          {checkError && <ErrorBanner message={checkError} />}
          {checkResult && (
            <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-4">
              <div className="flex flex-wrap items-start gap-3">
                <div className="min-w-0 flex-1">
                  <p className="font-semibold text-neutral-50">{checkResult.title || checkResult.video_id}</p>
                  <p className="mt-0.5 text-sm text-neutral-400">{checkResult.channel}</p>
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <LicenseBadge license={checkResult.license} />
                    {checkResult.license_url && (
                      <button type="button" className="text-xs text-sky-300 underline" onClick={() => { const url = checkResult.license_url; if (url) void openExternal(url); }}>Licence details</button>
                    )}
                  </div>
                  <p className="mt-2 text-sm font-medium text-emerald-300">
                    ✓ Can clip — {checkResult.reason}
                  </p>
                </div>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                <SecondaryButton href={checkResult.url || `https://www.youtube.com/watch?v=${checkResult.video_id}`}>Watch on YouTube</SecondaryButton>
                <button
                  type="button"
                  onClick={() => onClipVideo(clipVideoFromCheck(checkResult))}
                  title="Cut clips from this video"
                  className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500"
                >
                  Clip this video
                </button>
              </div>
            </div>
          )}
        </div>
      </Card>

      {picked && (
        <Card>
          <CardHeader title={ccSource ? "Clip this video" : "Turn this topic into clips from YOUR video"} hint="Approval-first by default" />
          <div className="space-y-5">
            <p className="text-sm text-neutral-300">
              {ccSource ? "Source: " : "Topic: "}<span className="font-medium text-neutral-100">{picked.title}</span>{" "}
              <span className="text-neutral-400">({picked.channel_title}{picked.view_count ? ` · ${fmtCompact(picked.view_count)} views` : ""})</span>
            </p>
            {ccSource && (
              <div className="rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <LicenseBadge license={ccSource.license} />
                  <span className="text-neutral-300">
                    Clips are cut from this {ccSource.license === "cc" ? "Creative Commons" : "own"} video — not from a standard-licence video.
                  </span>
                </div>
                <p className="mt-2 text-xs text-neutral-400">
                  Attribution is added to the description automatically and cannot be removed for Creative Commons sources — see the attribution block in the clip review.
                </p>
              </div>
            )}
            {!ccSource && (
              <fieldset>
                <legend className="text-sm font-medium text-neutral-100">Your own source</legend>
                <p className="mt-0.5 text-xs text-neutral-400">
                  The agent clips your own video on this topic — never the trending video itself.
                </p>
                <div className="mt-2 flex flex-wrap gap-2" role="radiogroup" aria-label="Own source type">
                  {(["video", "channel"] as const).map((t) => (
                    <button
                      key={t}
                      type="button"
                      role="radio"
                      aria-checked={ownSourceType === t}
                      onClick={() => { setOwnSourceType(t); setMatched(null); }}
                      className={`rounded-md border px-3 py-1.5 text-sm ${ownSourceType === t ? "border-amber-400 bg-amber-400/10 text-neutral-100" : "border-neutral-700 bg-neutral-950 text-neutral-300 hover:border-neutral-500"}`}
                    >
                      {t === "video" ? "My own video URL" : "My own channel URL (match it)"}
                    </button>
                  ))}
                </div>
                <input
                  className="mt-2 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm"
                  value={ownSourceUrl}
                  onChange={(e) => { setOwnSourceUrl(e.target.value); setMatched(null); }}
                  placeholder={ownSourceType === "video" ? "https://www.youtube.com/watch?v=… (your own video)" : "https://www.youtube.com/@yourchannel"}
                  aria-label={ownSourceType === "video" ? "Your own video URL" : "Your own channel URL"}
                />
                {ownSourceType === "channel" && (
                  <div className="mt-2">
                    <SecondaryButton onClick={() => { void onMatch(); }} disabled={matching}>
                      {matching ? "Matching…" : "Find my video on this topic"}
                    </SecondaryButton>
                  </div>
                )}
                {matched && (
                  <p className="mt-2 rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm text-emerald-100">
                    Best match in your channel: <span className="font-medium">{matched.title}</span> (topic match {Math.round(matched.score * 100)}%).
                    The run will clip this video, not the trending one.
                  </p>
                )}
              </fieldset>
            )}

            {!ccSource && (
              <>
                <label className="block">
                  <span className="text-sm font-medium text-neutral-100">Schedule for (optional)</span>
                  <span className="mt-0.5 block text-xs text-neutral-400">When the clips should go out. Leave empty to schedule them from the approval queue later.</span>
                  <input
                    type="datetime-local"
                    className="mt-1 w-full max-w-xs rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm"
                    value={scheduledFor}
                    onChange={(e) => setScheduledFor(e.target.value)}
                    aria-label="Schedule for"
                  />
                </label>

                <Toggle
                  checked={autoPost}
                  onChange={(v) => setAutoPost(v)}
                  label="Auto-post (publish without review)"
                  hint="OFF by default. When on, clips publish straight to your channels with no review step. Approval-first is the safe default."
                />
                {autoPost && (
                  <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200" role="alert">
                    Warning: with auto-post on, the agent publishes clips without asking you. Only use it with a source and niche you trust.
                  </p>
                )}
              </>
            )}
            {ccSource && (
              <p className="text-sm text-neutral-400">
                The clip job goes through the normal pipeline — clips land in the approval queue first, where you review them and set the schedule. Nothing publishes without approval.
              </p>
            )}

            <label className="flex items-start gap-3 text-sm">
              <input
                type="checkbox"
                className="mt-0.5 h-4 w-4 accent-amber-400"
                checked={rights}
                onChange={(e) => setRights(e.target.checked)}
              />
              <span>
                <span className="font-medium text-neutral-100">{ccSource ? "Licence acknowledgement (required)" : "Rights acknowledgement (required)"}</span>
                <span className="mt-0.5 block text-xs text-neutral-400">
                  {ccSource
                    ? "This video is Creative Commons licensed (or my own). Attribution will be included in the description and must stay there."
                    : "This source is my own video/channel or I have a licence/rights to use it. The trending video itself is never clipped."}
                </span>
              </span>
            </label>

            <div className="flex flex-wrap gap-2">
              <PrimaryButton onClick={onStartRun} disabled={starting || running}>
                {starting ? "Starting…" : running ? "Run in progress…" : ccSource ? "Start clipping this video" : "Start agent run on my video"}
              </PrimaryButton>
              {runJobId && (
                <SecondaryButton href="/pipeline">See the pipeline</SecondaryButton>
              )}
            </div>
          </div>
        </Card>
      )}

      {clipJobId && (
        <Card>
          <CardHeader title="Clip job started" hint="CC / own video" />
          <div className="space-y-3">
            <p className="text-sm text-neutral-300">
              Job: <span className="font-mono text-sm">{clipJobId}</span> — the clips appear in the pipeline and the approval queue when they are ready.
            </p>
            <div className="flex flex-wrap gap-2">
              <SecondaryButton href={`/jobs/view/?id=${encodeURIComponent(clipJobId)}`}>Open the clip job</SecondaryButton>
              <SecondaryButton href="/pipeline">See the pipeline</SecondaryButton>
              <SecondaryButton href="/queue">Approval queue</SecondaryButton>
            </div>
          </div>
        </Card>
      )}

      {(runState === "running" || runState === "done" || runState === "error") && (
        <Card>
          <CardHeader title="Trending run" hint="Live progress" />
          <div className="space-y-3">
            <p className="text-sm text-neutral-300">
              Status: <span className="font-medium text-neutral-100">{runState}</span>
              {runJobId && <> · Job: <span className="font-mono text-sm">{runJobId}</span></>}
            </p>
            <ProgressBar value={runProgress} />
            <p className="text-xs text-neutral-400">{runProgress}% · {runQueued} post{runQueued === 1 ? "" : "s"} queued for approval</p>
            {runError && <ErrorBanner message={runError} />}
            {runState === "done" && (
              <div className="flex flex-wrap gap-2">
                <SecondaryButton href="/queue">Review in the approval queue</SecondaryButton>
                {runJobId && <SecondaryButton href={`/jobs/view/?id=${encodeURIComponent(runJobId)}`}>Open the job</SecondaryButton>}
              </div>
            )}
            {runState === "done" && runJob && runJob.posts.length > 0 && (
              <ul className="space-y-2">
                {runJob.posts.map((p) => (
                  <li key={p.id} className="rounded-lg border border-neutral-800 p-3 text-sm">
                    <span className="font-medium capitalize">{p.platform}</span> · {p.status}
                    {p.title && <span className="text-neutral-400"> · {p.title}</span>}
                    {p.provider_post_id && <span className="text-neutral-400"> · video id <span className="font-mono text-xs">{p.provider_post_id}</span></span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}
