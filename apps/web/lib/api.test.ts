// Unit tests for the API status contract (lib/api.ts).
// Run with `npm run test:unit` — compiles this file + api.ts with tsc and
// runs it under Node's built-in test runner. No extra test dependency.
import test from "node:test";
import assert from "node:assert/strict";
import {
  activeStageIndex,
  clipVideoUrl,
  failedStageIndex,
  latestSnapshotPerPlatform,
  deriveRedirectUri,
  explainConnectionError,
  maskSecret,
  normalizeConnectGuide,
  normalizeJobLogEntry,
  normalizeOnboarding,
  normalizeAccount,
  normalizeAnalytics,
  normalizeAnalyticsSnapshot,
  normalizeAutomationRule,
  ffmpegInstallRunning,
  ytdlpInstallRunning,
  normalizeChannelStatus,
  normalizeChannels,
  normalizeFfmpegInstallStatus,
  normalizeYtDlpInstallStatus,
  normalizeJob,
  normalizeJobs,
  normalizeJobStatus,
  normalizePost,
  normalizePostStatus,
  normalizeSettings,
  pipelineProgress,
  normalizeAgentStatus,
  normalizeAgentConfig,
  normalizeLlmProviders,
  activeEngineName,
  normalizeSetupState,
  normalizeTrendingCredentials,
  normalizeTrendingJob,
  normalizeTrendingVideo,
  normalizeTrendingVideos,
  normalizeVideoLicense,
  normalizeLicenseCheck,
  normalizeCandidate,
  normalizeCandidates,
  normalizeThumbnailVariants,
  jobSourceLicense,
  licenseBadgeLabel,
  deriveCanClip,
  attributionForClip,
  normalizeWorkerQueue,
  normalizeNotifications,
  normalizeNotification,
  normalizePerfProfile,
  normalizeBatchStatus,
  normalizeDetectionCandidate,
  pipelineStageStates,
  resolveApiUrl,
  RUNNING_STATUSES,
  sourceVideoUrl,
  startAgentOnSource,
  API_URL,
} from "./api.js";

test("normalizeJobStatus passes backend statuses through unchanged", () => {
  for (const s of ["pending", "queued", "running", "paused", "transcribing", "scoring", "rendering", "review", "ready", "failed", "blocked", "cancelled"]) {
    assert.equal(normalizeJobStatus(s), s);
  }
});

test("normalizeJobStatus keeps the v0.9.0 queue statuses (queued/running/paused) — they are real, not aliases", () => {
  assert.equal(normalizeJobStatus("queued"), "queued");
  assert.equal(normalizeJobStatus("paused"), "paused");
});

test("normalizeJobStatus falls back to 'pending' for unknown or non-string values", () => {
  for (const bad of ["bogus", "", null, undefined, 42, {}, ["pending"]]) {
    assert.equal(normalizeJobStatus(bad), "pending");
  }
});

test("normalizeJob normalises the status of a raw job payload", () => {
  const job = normalizeJob({ id: "j1", title: "t", source: "s.mp4", status: "queued", created_at: "2026-11-20", candidates: 2 });
  assert.equal(job.status, "queued");
  assert.equal(job.candidates, 2);
  const empty = normalizeJob({ status: "ready" });
  assert.equal(empty.id, "");
  assert.equal(empty.candidates, 0);
});

test("normalizeJob carries the v0.9.0 queue position + plain-language failure", () => {
  const queued = normalizeJob({ id: "j2", status: "queued", queue_position: 2, queue_priority: 1 });
  assert.equal(queued.queue_position, 2);
  assert.equal(queued.queue_priority, 1);
  const failed = normalizeJob({
    id: "j3", status: "failed",
    error: "FFmpeg is missing — press 'Install FFmpeg' in ClipForge.",
    error_title: "FFmpeg is missing",
    error_action: "Press 'Install FFmpeg' in Settings, then retry the job.",
    error_code: "ffmpeg_missing",
  });
  assert.equal(failed.error_title, "FFmpeg is missing");
  assert.equal(failed.error_action, "Press 'Install FFmpeg' in Settings, then retry the job.");
  assert.equal(failed.error_code, "ffmpeg_missing");
  // Missing fields stay null/0 — never undefined, never a crash.
  const bare = normalizeJob({ id: "j4", status: "pending" });
  assert.equal(bare.queue_position, null);
  assert.equal(bare.queue_priority, 0);
  assert.equal(bare.error_title, null);
  assert.equal(bare.error_action, null);
  assert.equal(bare.error_code, null);
});

test("normalizeJobs maps an array and drops non-arrays", () => {
  assert.deepEqual(normalizeJobs([{ status: "queued" }, { status: "ready" }]).map((j) => j.status), ["queued", "ready"] );
  assert.deepEqual(normalizeJobs(null), []);
  assert.deepEqual(normalizeJobs({}), []);
});

test("normalizePostStatus maps legacy 'pending'/'queued' to 'pending_approval'", () => {
  assert.equal(normalizePostStatus("pending"), "pending_approval");
  assert.equal(normalizePostStatus("queued"), "pending_approval");
  assert.equal(normalizePostStatus("published"), "published");
  assert.equal(normalizePostStatus("dry_run"), "dry_run");
  assert.equal(normalizePostStatus("nope"), "pending_approval");
  assert.equal(normalizePostStatus(null), "pending_approval");
});

test("normalizePost keeps the clip id from either field name", () => {
  assert.equal(normalizePost({ id: "p1", clip_candidate_id: "c1", status: "pending" }).clip_id, "c1");
  assert.equal(normalizePost({ id: "p1", clip_id: "c2", status: "approved" }).status, "approved");
});

test("resolveApiUrl keeps an empty string (same-origin) and only defaults when unset", () => {
  // Empty string is the static-export / desktop case: API_URL "" makes
  // fetches relative ("/jobs"), served from the same origin as the UI.
  // A `??` fallback would wrongly turn "" back into localhost:8000.
  assert.equal(resolveApiUrl(""), "");
  assert.equal(resolveApiUrl(undefined), "http://localhost:8000");
  assert.equal(resolveApiUrl("http://localhost:8000"), "http://localhost:8000");
  assert.equal(resolveApiUrl("https://api.example.test"), "https://api.example.test");
});

test("clipVideoUrl builds the media endpoint URL from a candidate file_path", () => {
  assert.equal(
    clipVideoUrl({ job_id: "job-1", file_path: "data/clips/job-1/clip_01.mp4" }),
    `${API_URL}/media/clips/job-1/clip_01.mp4`,
  );
  assert.equal(clipVideoUrl({ job_id: "job-1", file_path: null }), null);
  assert.equal(clipVideoUrl({ job_id: "job-1", file_path: "" }), null);
});

test("sourceVideoUrl only serves bare filenames from the uploads dir", () => {
  assert.equal(sourceVideoUrl({ source: "sample_60s.mp4" }), `${API_URL}/media/uploads/sample_60s.mp4`);
  assert.equal(sourceVideoUrl({ source: "/abs/path/video.mp4" }), null);
  assert.equal(sourceVideoUrl({ source: "sub/dir/video.mp4" }), null);
  assert.equal(sourceVideoUrl({ source: "" }), null);
});

test("normalizeJob keeps the error, source_type and own_content from the wire", () => {
  const job = normalizeJob({ id: "j1", status: "failed", error: "boom", source_type: "youtube", source: "https://youtu.be/x", own_content: true });
  assert.equal(job.error, "boom");
  assert.equal(job.source_type, "youtube");
  assert.equal(job.own_content, true);
  const bare = normalizeJob({ status: "ready" });
  assert.equal(bare.error, null);
  assert.equal(bare.source_type, "local");
  assert.equal(bare.own_content, false);
});

test("normalizePost keeps the error and attempts from the wire", () => {
  const post = normalizePost({ id: "p1", status: "failed", error: "quota", attempts: 2 });
  assert.equal(post.error, "quota");
  assert.equal(post.attempts, 2);
  assert.equal(normalizePost({ id: "p2", status: "published" }).attempts, 0);
});

test("normalizeAccount keeps token_configured and never invents a token", () => {
  const a = normalizeAccount({ id: "a1", provider: "youtube", label: "@x", status: "active", token_env_var: "YOUTUBE_REFRESH_TOKEN", token_configured: true });
  assert.equal(a.provider, "youtube");
  assert.equal(a.token_configured, true);
  assert.equal(a.token_env_var, "YOUTUBE_REFRESH_TOKEN");
  assert.equal(normalizeAccount({ provider: "tiktok" }).token_configured, false);
  assert.equal(normalizeAccount(null).id, "");
});

test("normalizeAnalyticsSnapshot keeps signals and drops non-numbers", () => {
  const s = normalizeAnalyticsSnapshot({ platform: "youtube", views: 100, followers: 50, watch_hours: 12.5, shorts_views_90d: 90, completion_pct: 62, viewed_pct: 74, signals: ["good hook", 42, null] });
  assert.equal(s.views, 100);
  assert.equal(s.watch_hours, 12.5);
  assert.deepEqual(s.signals, ["good hook"]);
  assert.equal(normalizeAnalyticsSnapshot({ platform: "instagram", views: "lots" }).views, 0);
  assert.deepEqual(normalizeAnalytics(null), []);
});

test("latestSnapshotPerPlatform keeps the first (newest) row per platform", () => {
  const snaps = normalizeAnalytics([
    { platform: "youtube", views: 10 },
    { platform: "youtube", views: 5 },
    { platform: "tiktok", views: 7 },
  ]);
  const latest = latestSnapshotPerPlatform(snaps);
  assert.equal(latest.get("youtube")?.views, 10);
  assert.equal(latest.get("tiktok")?.views, 7);
  assert.equal(latest.size, 2);
});

test("pipelineStageStates marks stages before the active one done", () => {
  assert.deepEqual(pipelineStageStates("pending"), ["active", "pending", "pending", "pending", "pending", "pending", "pending", "pending"]);
  assert.deepEqual(pipelineStageStates("scoring"), ["done", "done", "active", "pending", "pending", "pending", "pending", "pending"]);
  assert.deepEqual(pipelineStageStates("review"), ["done", "done", "done", "active", "pending", "pending", "pending", "pending"]);
  assert.deepEqual(pipelineStageStates("ready"), ["done", "done", "done", "done", "done", "active", "pending", "pending"]);
});

test("normalizeJob keeps the v0.3.0 detail fields from GET /jobs/{id}", () => {
  const job = normalizeJob({
    id: "j1", status: "failed", error: "boom", stage: "transcribe", progress_pct: 30,
    eta_s: null, demo: true, blocked_by: null, failed_stage: "transcribe",
    started_at: "2026-11-20T10:00:00Z", finished_at: "2026-11-20T10:00:30Z", duration_s: 30,
    stages: [{ stage: "ingest", status: "done", progress_pct: 10, message: "ok", error_code: null, duration_s: 1, created_at: "2026-11-20T10:00:01Z" }],
    worker_runs: [{ id: "r1", status: "failed", outcome: "err", error: "boom", duration_s: 30, started_at: "2026-11-20T10:00:00Z", finished_at: "2026-11-20T10:00:30Z" }],
  });
  assert.equal(job.failed_stage, "transcribe");
  assert.equal(job.duration_s, 30);
  assert.equal(job.stages.length, 1);
  assert.equal(job.stages[0].stage, "ingest");
  assert.equal(job.worker_runs[0].status, "failed");
  const bare = normalizeJob({ status: "pending" });
  assert.equal(bare.failed_stage, null);
  assert.equal(bare.duration_s, null);
  assert.deepEqual(bare.stages, []);
  assert.deepEqual(bare.worker_runs, []);
});

test("pipelineStageStates never guesses a failed stage", () => {
  // No failed_stage reported — all stages stay unmarked and the UI shows
  // the error banner + retry instead.
  assert.deepEqual(pipelineStageStates("failed"), ["pending", "pending", "pending", "pending", "pending", "pending", "pending", "pending"]);
  assert.deepEqual(pipelineStageStates("failed", "bogus-stage"), ["pending", "pending", "pending", "pending", "pending", "pending", "pending", "pending"]);
  assert.equal(activeStageIndex("failed"), -1);
});

test("RUNNING_STATUSES is only the stages where the worker is active", () => {
  // "pending" / "queued" have not started yet, "paused" waits, and
  // "review" has finished rendering and waits for a human — none of
  // those is "running". "running" itself (claimed, pre-stage) is.
  assert.deepEqual([...RUNNING_STATUSES], ["running", "transcribing", "scoring", "rendering"]);
});

test("pipelineStageStates marks the reported failed stage (contract §2)", () => {
  assert.equal(failedStageIndex("transcribe"), 1);
  assert.equal(failedStageIndex("seo"), 4);
  assert.equal(failedStageIndex("review"), 3);
  assert.equal(failedStageIndex(null), -1);
  assert.equal(failedStageIndex("bogus"), -1);
  assert.deepEqual(
    pipelineStageStates("failed", "transcribe"),
    ["done", "failed", "pending", "pending", "pending", "pending", "pending", "pending"],
  );
  assert.deepEqual(
    pipelineStageStates("failed", "seo"),
    ["done", "done", "done", "done", "failed", "pending", "pending", "pending"],
  );
});

test("normalizeAutomationRule maps the GET /automation/rules row", () => {
  const rule = normalizeAutomationRule({
    id: "r1", key: "retention_feedback", kind: "auto_pick_next_clip",
    label: "Retention-feedback clip picking", enabled: true, description: "Prefer hooks that scored well.",
  });
  assert.ok(rule);
  assert.equal(rule?.key, "retention_feedback");
  assert.equal(rule?.kind, "auto_pick_next_clip");
  assert.equal(rule?.enabled, true);
  assert.equal(normalizeAutomationRule({ key: "x" }), null);
  assert.equal(normalizeAutomationRule({ key: "x", enabled: "yes" }), null);
  assert.equal(normalizeAutomationRule(null), null);
});

test("pipelineProgress is 100 for ready and grows with the active stage", () => {
  assert.equal(pipelineProgress("ready"), 100);
  assert.equal(pipelineProgress("pending"), 0);
  assert.ok(pipelineProgress("transcribing") > pipelineProgress("pending"));
  assert.ok(pipelineProgress("scoring") > pipelineProgress("transcribing"));
});

test("normalizeChannelStatus maps the backend channel card", () => {
  const card = normalizeChannelStatus({
    platform: "youtube", display_name: "YouTube", status: "expired",
    account_name: "My Channel", avatar_url: "https://i.yt/a.jpg", account_ref: "UC1",
    scopes: ["https://www.googleapis.com/auth/youtube.upload"], daily_cap: 6,
    expires_at: "2026-11-21T00:00:00Z", source: "oauth",
    review_notice: "n", fallback: "f", can_refresh: true,
    health: "disconnected", health_reason: "Token expired 2 hours ago.",
    health_next_step: "Press Refresh token for YouTube.",
    expires_in_s: -7200, needs_refresh: true,
    last_error: "refresh token revoked", last_checked_at: "2026-11-21T00:00:00Z",
    checked_at: "2026-11-21T01:00:00Z",
  });
  assert.equal(card.status, "expired");
  assert.equal(card.daily_cap, 6);
  assert.equal(card.source, "oauth");
  assert.equal(card.can_refresh, true);
  assert.equal(card.health, "disconnected");
  assert.equal(card.health_reason, "Token expired 2 hours ago.");
  assert.equal(card.health_next_step, "Press Refresh token for YouTube.");
  assert.equal(card.expires_in_s, -7200);
  assert.equal(card.needs_refresh, true);
  assert.equal(card.last_error, "refresh token revoked");
  assert.equal(card.checked_at, "2026-11-21T01:00:00Z");
});

test("normalizeChannelStatus maps health expiring_soon and distrusts unknown health", () => {
  const soon = normalizeChannelStatus({
    platform: "tiktok", status: "connected", health: "expiring_soon",
    health_reason: "Token expires in 10 minutes.", expires_in_s: 600, needs_refresh: true,
  });
  assert.equal(soon.health, "expiring_soon");
  assert.equal(soon.expires_in_s, 600);
  const hacked = normalizeChannelStatus({ platform: "tiktok", status: "connected", health: "hacked" });
  assert.equal(hacked.health, "not_connected");
  assert.equal(hacked.expires_in_s, null);
  assert.equal(hacked.needs_refresh, false);
  assert.equal(hacked.last_error, null);
});

test("normalizeChannelStatus never trusts an unknown status or source", () => {
  const card = normalizeChannelStatus({ platform: "tiktok", status: "hacked", source: "cookie" });
  assert.equal(card.status, "not_connected");
  assert.equal(card.source, "none");
  assert.equal(card.account_name, "");
  assert.deepEqual(card.scopes, []);
  assert.deepEqual(normalizeChannels(null), []);
  assert.deepEqual(normalizeChannels([{ platform: "youtube", status: "connected" }]).map((c) => c.status), ["connected"]);
});

test("maskSecret shows only the last four characters", () => {
  assert.equal(maskSecret("sk-abcdef123456"), "••••••••3456");
  assert.equal(maskSecret("abc"), "•••");
  assert.equal(maskSecret(""), "");
  assert.equal(maskSecret(null), "");
});

test("normalizeSettings keeps masked views and drops non-objects", () => {
  assert.equal(normalizeSettings(null), null);
  assert.equal(normalizeSettings({ dry_run: "yes" }), null);
  const view = normalizeSettings({
    dry_run: false, dry_run_source: "settings",
    default_privacy: { youtube: "private" }, default_platforms: ["youtube"],
    llm: { provider: "openai-compatible", base_url: "https://x/v1", model: "m", api_key: { configured: true, last4: "7890", masked: "••••••••7890", source: "stored" } },
    credentials: { youtube: { client_id: { configured: true, last4: "1111", masked: "", source: "stored" }, client_secret: { configured: false, last4: "", masked: "", source: "none" } } },
    data_dir: "/data", secret_backend: "encrypted-file",
    ffmpeg: { found: true, path: "/usr/bin/ffmpeg", install_url: "https://ffmpeg.org/download.html" },
    monetization_thresholds: [{ platform: "youtube", program: "YPP", threshold: { subscribers: 1000 }, note: "" }],
  });
  assert.ok(view);
  assert.equal(view?.dry_run, false);
  assert.equal(view?.llm.api_key.last4, "7890");
  assert.equal(view?.credentials.youtube.client_secret.configured, false);
  assert.equal(view?.ffmpeg.found, true);
  assert.equal(view?.monetization_thresholds.length, 1);
});

test("deriveRedirectUri uses the connected API's own port, never a hardcoded host", () => {
  // In this test env NEXT_PUBLIC_API_URL is unset, so API_URL defaults to
  // http://localhost:8000 — the redirect base is the API's own port.
  assert.equal(deriveRedirectUri("youtube"), "http://127.0.0.1:8000/auth/callback/youtube");
  assert.equal(deriveRedirectUri("tiktok"), "http://127.0.0.1:8000/auth/callback/tiktok");
});

test("explainConnectionError translates cryptic OAuth failures into plain language", () => {
  const cases: [string, string, string][] = [
    ["redirect_uri_mismatch", "redirect", "Copy the redirect URI"],
    ["invalid_client: bad id", "did not recognise", "re-copy the client ID"],
    ["token expired", "expired", "Reconnect"],
    ["not connected yet", "not connected", "Connect button"],
    ["YouTube client ID is not saved yet — the app credentials have not been entered. Next step: Save the Client ID in Settings (Step 3 of the Connect guide, GET /connect/guide/youtube)", "not saved yet", "step 3 of the Connect wizard"],
    ["No LLM API key saved — add one in Settings first.", "No AI key saved yet", "built-in templates"],
    ["", "no reason", "Try again"],
  ];
  for (const [input, summaryPart, nextPart] of cases) {
    const out = explainConnectionError(input);
    assert.ok(out.summary.toLowerCase().includes(summaryPart.toLowerCase()), `${input} -> ${out.summary}`);
    assert.ok(out.nextStep.includes(nextPart), `${input} -> ${out.nextStep}`);
  }
  // Unknown messages pass through unchanged — never swallowed.
  const unknown = explainConnectionError("weird provider 500");
  assert.equal(unknown.summary, "weird provider 500");
});

test("normalizeJobLogEntry maps GET /jobs/{id}/logs rows", () => {
  const entry = normalizeJobLogEntry({ ts: "2026-11-21T10:00:00", stage: "transcribe", level: "error", message: "boom [E1]" });
  assert.equal(entry.stage, "transcribe");
  assert.equal(entry.level, "error");
  assert.equal(entry.message, "boom [E1]");
  const empty = normalizeJobLogEntry(null);
  assert.equal(empty.stage, "");
  assert.equal(empty.level, "info");
});

test("normalizeConnectGuide maps GET /connect/guide/{platform}", () => {
  const g = normalizeConnectGuide({
    platform: "youtube", display_name: "YouTube",
    console_name: "Google Cloud Console", console_url: "https://console.cloud.google.com/apis/credentials",
    steps: [{ step: 1, title: "Create a project", detail: "Open the console." }],
    credentials: [{ key: "client_id", label: "Client ID", saved: true }, { key: "client_secret", label: "Client secret", saved: false }],
    redirect_uri: "http://127.0.0.1:8000/auth/callback/youtube",
    scopes: ["https://www.googleapis.com/auth/youtube.upload"],
    review_notice: "Google must verify the app.", fallback: "Manual-post pack.", daily_cap: 6,
  });
  assert.ok(g);
  assert.equal(g?.redirect_uri, "http://127.0.0.1:8000/auth/callback/youtube");
  assert.equal(g?.steps.length, 1);
  assert.equal(g?.credentials[0].saved, true);
  assert.equal(g?.credentials[1].saved, false);
  assert.equal(normalizeConnectGuide({ platform: "youtube" }), null);
  assert.equal(normalizeConnectGuide(null), null);
});

test("normalizeOnboarding fills defaults and rejects bad llm values", () => {
  const d = normalizeOnboarding({});
  assert.equal(d.completed, false);
  assert.equal(d.llm, "");
  const done = normalizeOnboarding({ completed: true, llm: "skipped", ffmpeg_acknowledged: true });
  assert.equal(done.completed, true);
  assert.equal(done.llm, "skipped");
  const bad = normalizeOnboarding({ llm: "bogus" });
  assert.equal(bad.llm, "");
});

test("normalizeFfmpegInstallStatus passes a full status through", () => {
  const s = normalizeFfmpegInstallStatus({
    state: "downloading", progress_pct: 42.5, downloaded_bytes: 42000000, total_bytes: 100000000,
    message: "Downloading FFmpeg…", error: "", version: "", path: "",
    found: false, download_url: "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    platform: "Windows", path_added: null, checksum_verified: null,
    failed_step: "", path_warning: "",
  });
  assert.ok(s);
  assert.equal(s?.state, "downloading");
  assert.equal(s?.progress_pct, 42.5);
  assert.equal(s?.total_bytes, 100000000);
  assert.equal(s?.download_url, "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip");
  assert.equal(s?.failed_step, "");
  assert.equal(s?.path_warning, "");
});

test("normalizeFfmpegInstallStatus passes failed_step and path_warning through", () => {
  const failed = normalizeFfmpegInstallStatus({
    state: "error", progress_pct: 0, downloaded_bytes: 0, total_bytes: null,
    message: "", error: "Download failed: Could not reach the download server.",
    version: "", path: "", found: false, download_url: "", platform: "Windows",
    path_added: null, checksum_verified: null, failed_step: "download", path_warning: "",
  });
  assert.equal(failed?.failed_step, "download");
  assert.equal(failed?.error, "Download failed: Could not reach the download server.");
  const warned = normalizeFfmpegInstallStatus({
    state: "done", progress_pct: 100, downloaded_bytes: 0, total_bytes: null,
    message: "FFmpeg is installed and ready.", error: "",
    version: "ffmpeg version 9.1", path: "C:\\ffmpeg\\bin\\ffmpeg.exe", found: true,
    download_url: "", platform: "Windows", path_added: false, checksum_verified: true,
    failed_step: "", path_warning: "Could not add FFmpeg to your Windows PATH.",
  });
  assert.equal(warned?.path_added, false);
  assert.equal(warned?.path_warning, "Could not add FFmpeg to your Windows PATH.");
});

test("normalizeFfmpegInstallStatus rejects unknown states and non-objects", () => {
  assert.equal(normalizeFfmpegInstallStatus({ state: "flying" }), null);
  assert.equal(normalizeFfmpegInstallStatus(null), null);
  assert.equal(normalizeFfmpegInstallStatus("done"), null);
});

test("ffmpegInstallRunning is true only while a run is active", () => {
  const base = { progress_pct: 0, downloaded_bytes: 0, total_bytes: null, message: "", error: "", version: "", path: "", found: false, download_url: "", platform: "Linux", path_added: null, checksum_verified: null, failed_step: "", path_warning: "" };
  for (const state of ["downloading", "verifying", "extracting", "installing"] as const) {
    assert.equal(ffmpegInstallRunning({ ...base, state }), true);
  }
  for (const state of ["idle", "done", "error", "unsupported"] as const) {
    assert.equal(ffmpegInstallRunning({ ...base, state }), false);
  }
  assert.equal(ffmpegInstallRunning(null), false);
});

test("normalizeYtDlpInstallStatus passes a full status through", () => {
  const s = normalizeYtDlpInstallStatus({
    state: "downloading", progress_pct: 42.5, downloaded_bytes: 17000000, total_bytes: 40000000,
    message: "Downloading yt-dlp…", error: "", version: "", path: "",
    found: false, download_url: "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux",
    platform: "Linux", path_added: null, checksum_verified: null,
    failed_step: "", path_warning: "",
  });
  assert.ok(s);
  assert.equal(s?.state, "downloading");
  assert.equal(s?.progress_pct, 42.5);
  assert.equal(s?.total_bytes, 40000000);
  assert.equal(s?.download_url, "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux");
  assert.equal(s?.failed_step, "");
  assert.equal(s?.path_warning, "");
});

test("normalizeYtDlpInstallStatus passes failed_step and path_warning through", () => {
  const failed = normalizeYtDlpInstallStatus({
    state: "error", progress_pct: 0, downloaded_bytes: 0, total_bytes: null,
    message: "", error: "Verification failed: The downloaded file did not match its published checksum.",
    version: "", path: "", found: false, download_url: "", platform: "Linux",
    path_added: null, checksum_verified: null, failed_step: "verify", path_warning: "",
  });
  assert.equal(failed?.failed_step, "verify");
  assert.equal(failed?.error, "Verification failed: The downloaded file did not match its published checksum.");
  const warned = normalizeYtDlpInstallStatus({
    state: "done", progress_pct: 100, downloaded_bytes: 0, total_bytes: null,
    message: "yt-dlp is installed and ready.", error: "",
    version: "2026.10.26", path: "C:\\clipforge\\yt-dlp\\yt-dlp.exe", found: true,
    download_url: "", platform: "Windows", path_added: false, checksum_verified: true,
    failed_step: "", path_warning: "Could not add yt-dlp to your Windows PATH.",
  });
  assert.equal(warned?.path_added, false);
  assert.equal(warned?.path_warning, "Could not add yt-dlp to your Windows PATH.");
});

test("normalizeYtDlpInstallStatus rejects unknown states and non-objects", () => {
  assert.equal(normalizeYtDlpInstallStatus({ state: "flying" }), null);
  assert.equal(normalizeYtDlpInstallStatus(null), null);
  assert.equal(normalizeYtDlpInstallStatus("done"), null);
});

test("normalizeYtDlpInstallStatus passes source, broken and path_refreshed through (v0.9.1)", () => {
  const s = normalizeYtDlpInstallStatus({
    state: "idle", progress_pct: 0, downloaded_bytes: 0, total_bytes: null,
    message: "", error: "", version: "2026.10.26",
    path: "C:\\Users\\hamxa\\AppData\\Roaming\\Python\\Python313\\Scripts\\yt-dlp.exe",
    found: true, download_url: "", platform: "Windows",
    path_added: null, checksum_verified: null, failed_step: "", path_warning: "",
    broken: false, broken_reason: "", source: "Python user Scripts folder (pip install)",
    path_refreshed: ["C:\\Users\\hamxa\\AppData\\Roaming\\Python\\Python313\\Scripts"],
  });
  assert.equal(s?.found, true);
  assert.equal(s?.version, "2026.10.26");
  assert.equal(s?.source, "Python user Scripts folder (pip install)");
  assert.equal(s?.broken, false);
  assert.equal(s?.path_refreshed.length, 1);
  const broken = normalizeYtDlpInstallStatus({
    state: "idle", progress_pct: 0, downloaded_bytes: 0, total_bytes: null,
    message: "", error: "", version: "",
    path: "C:\\Tools\\yt-dlp.exe", found: false, download_url: "", platform: "Windows",
    path_added: null, checksum_verified: null, failed_step: "", path_warning: "",
    broken: true,
    broken_reason: "yt-dlp was found at C:\\Tools\\yt-dlp.exe (PATH) but does not run (yt-dlp --version failed).",
    source: "PATH", path_refreshed: [],
  });
  assert.equal(broken?.found, false);
  assert.equal(broken?.broken, true);
  assert.ok(broken?.broken_reason.includes("does not run"));
  // Older backends omit the v0.9.1 fields — defaults, not a crash.
  const legacy = normalizeYtDlpInstallStatus({
    state: "done", progress_pct: 100, downloaded_bytes: 0, total_bytes: null,
    message: "", error: "", version: "2026.10.26", path: "/tmp/yt-dlp/yt-dlp",
    found: true, download_url: "", platform: "Linux",
    path_added: null, checksum_verified: null, failed_step: "", path_warning: "",
  });
  assert.equal(legacy?.broken, false);
  assert.equal(legacy?.source, "");
  assert.deepEqual(legacy?.path_refreshed, []);
});

test("ytdlpInstallRunning is true only while a run is active (no extracting step)", () => {
  const base = { progress_pct: 0, downloaded_bytes: 0, total_bytes: null, message: "", error: "", version: "", path: "", found: false, download_url: "", platform: "Linux", path_added: null, checksum_verified: null, failed_step: "", path_warning: "", broken: false, broken_reason: "", source: "", path_refreshed: [] as string[] };
  for (const state of ["downloading", "verifying", "installing"] as const) {
    assert.equal(ytdlpInstallRunning({ ...base, state }), true);
  }
  for (const state of ["idle", "done", "error", "unsupported"] as const) {
    assert.equal(ytdlpInstallRunning({ ...base, state }), false);
  }
  assert.equal(ytdlpInstallRunning(null), false);
});

test("normalizeSettings keeps the yt-dlp version when present", () => {
  const s = normalizeSettings({
    dry_run: true, dry_run_source: "env", default_privacy: {}, default_platforms: [],
    llm: { provider: "openai-compatible", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini", api_key: { configured: false, last4: "", masked: "", source: "none" } },
    credentials: {}, data_dir: "/tmp", secret_backend: "file",
    ffmpeg: { found: true, path: "/tmp/ffmpeg/bin/ffmpeg", version: "ffmpeg version 9.1", install_url: "https://ffmpeg.org/download.html" },
    yt_dlp: { found: true, path: "/tmp/yt-dlp/yt-dlp", version: "2026.10.26", install_url: "https://github.com/yt-dlp/yt-dlp/releases/latest" },
    monetization_thresholds: [],
  });
  assert.equal(s?.yt_dlp.found, true);
  assert.equal(s?.yt_dlp.version, "2026.10.26");
  assert.equal(s?.yt_dlp.broken, false);
  assert.equal(s?.yt_dlp.source, "");
  assert.equal(s?.ffmpeg.found, true);
});

test("normalizeSettings defaults yt-dlp to not found when the backend predates it", () => {
  const s = normalizeSettings({
    dry_run: true, dry_run_source: "env", default_privacy: {}, default_platforms: [],
    llm: { provider: "openai-compatible", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini", api_key: { configured: false, last4: "", masked: "", source: "none" } },
    credentials: {}, data_dir: "/tmp", secret_backend: "file",
    ffmpeg: { found: false, path: "", version: "", install_url: "https://ffmpeg.org/download.html" },
    monetization_thresholds: [],
  });
  assert.equal(s?.yt_dlp.found, false);
  assert.equal(s?.yt_dlp.path, "");
});

test("normalizeSettings keeps the ffmpeg version when present", () => {
  const s = normalizeSettings({
    dry_run: true, dry_run_source: "env", default_privacy: {}, default_platforms: [],
    llm: { provider: "openai-compatible", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini", api_key: { configured: false, last4: "", masked: "", source: "none" } },
    credentials: {}, data_dir: "/tmp", secret_backend: "file",
    ffmpeg: { found: true, path: "/tmp/ffmpeg/bin/ffmpeg", version: "ffmpeg version 9.1", install_url: "https://ffmpeg.org/download.html" },
    monetization_thresholds: [],
  });
  assert.equal(s?.ffmpeg.found, true);
  assert.equal(s?.ffmpeg.version, "ffmpeg version 9.1");
});

test("normalizeAgentStatus requires enabled/running booleans", () => {
  assert.equal(normalizeAgentStatus(null), null);
  assert.equal(normalizeAgentStatus({ enabled: true }), null);
  const s = normalizeAgentStatus({
    enabled: true, running: true, current_job: "job-1",
    accounts: [{ platform: "youtube", account: "My Channel", status: "running", progress_pct: 40, queued: 2, approved: 1, published: 3 }],
  });
  assert.equal(s?.enabled, true);
  assert.equal(s?.current_job, "job-1");
  assert.equal(s?.accounts[0]?.published, 3);
  assert.equal(normalizeAgentStatus({ enabled: false, running: false })?.accounts.length, 0);
});

test("normalizeAgentStatus keeps the run outcome (v0.8.1 toggle fix)", () => {
  // Before the fix the normaliser dropped state/last_error/progress, so
  // the Agent page could only show Enabled/Idle while a run had silently
  // errored — the toggle looked broken. These fields must survive.
  const s = normalizeAgentStatus({
    enabled: true, running: false, state: "error", progress_pct: 5,
    queued: 0, approved: 0, published: 0, engine: "rules", demo: false,
    last_error: "yt-dlp is not installed (no 'yt-dlp' on PATH).",
    last_run_at: "2026-11-21T21:19:06+00:00", niche: "gaming",
    auto_post: false, rights_confirmed: true, current_job: null, accounts: [],
  });
  assert.equal(s?.state, "error");
  assert.equal(s?.last_error, "yt-dlp is not installed (no 'yt-dlp' on PATH).");
  assert.equal(s?.progress_pct, 5);
  assert.equal(s?.queued, 0);
  // Older servers omit the outcome fields: safe defaults, never a crash.
  const bare = normalizeAgentStatus({ enabled: false, running: false });
  assert.equal(bare?.state, "idle");
  assert.equal(bare?.last_error, null);
  assert.equal(bare?.progress_pct, 0);
});

test("normalizeAgentConfig defaults source_type to video and auto_post to false", () => {
  const c = normalizeAgentConfig({ enabled: false });
  assert.equal(c?.source_type, "video");
  assert.equal(c?.auto_post, false);
  assert.equal(c?.rights_confirmed, false);
  assert.equal(normalizeAgentConfig({ enabled: true, source_type: "channel", niche: "tech", auto_post: true, rights_confirmed: true })?.source_type, "channel");
  assert.equal(normalizeAgentConfig(null), null);
});

test("normalizeLlmProviders drops entries without an id", () => {
  const ps = normalizeLlmProviders([
    { id: "gemini", label: "Gemini", configured: true, model: "gemini-2.0-flash", active: true },
    { label: "no id" },
    { id: "openai", label: "OpenAI", configured: false, model: "", active: false },
  ]);
  assert.equal(ps.length, 2);
  assert.equal(ps[0].model, "gemini-2.0-flash");
  assert.equal(normalizeLlmProviders("nope").length, 0);
});

test("activeEngineName names Gemini only when it is configured", () => {
  assert.equal(activeEngineName(null), "rule-based engine");
  assert.equal(activeEngineName([]), "rule-based engine");
  assert.equal(activeEngineName([{ id: "gemini", label: "Gemini", configured: false, model: "", active: false }]), "rule-based engine");
  assert.equal(activeEngineName([{ id: "gemini", label: "Gemini", configured: true, model: "gemini-2.0-flash", active: true }]), "Gemini");
  assert.equal(activeEngineName([{ id: "openai", label: "OpenAI", configured: true, model: "", active: false }]), "OpenAI");
  // The rules provider is always configured=true — it must never read as an AI engine.
  assert.equal(activeEngineName([
    { id: "gemini", label: "Gemini", configured: false, model: "", active: false },
    { id: "openai", label: "OpenAI", configured: false, model: "", active: false },
    { id: "rules", label: "Rule-based", configured: true, model: "", active: true },
  ]), "rule-based engine");
});

test("normalizeSetupState clamps the step and defaults every step to false", () => {
  const s = normalizeSetupState({ current_step: 99, steps: { ffmpeg: true }, niche: "tech", source_type: "channel" });
  assert.equal(s.current_step, 4);
  assert.equal(s.steps.ffmpeg, true);
  assert.equal(s.steps.agent, false);
  assert.equal(s.source_type, "channel");
  const empty = normalizeSetupState(null);
  assert.equal(empty.current_step, 0);
  assert.equal(empty.completed, false);
  assert.equal(empty.source_type, "video");
});

test("normalizeWorkerQueue maps GET /worker/queue (v0.5.0 dashboard feed)", () => {
  const q = normalizeWorkerQueue({
    running: [{ id: "run-1", job_id: "job-001", status: "running", outcome: "", error: null, duration_s: null, started_at: "2026-11-21T10:00:00Z", finished_at: null }],
    queued: [{ id: "job-002", title: "Launch walkthrough", source_type: "local", status: "pending", created_at: "2026-11-21T11:30:00Z" }],
    next: { id: "job-002", title: "Launch walkthrough" },
    recent_runs: [{ id: "run-0", job_id: "job-001", status: "finished", outcome: "success", error: null, duration_s: 42, started_at: "2026-11-21T09:00:00Z", finished_at: "2026-11-21T09:00:42Z" }],
  });
  assert.equal(q?.running.length, 1);
  assert.equal(q?.running[0].job_id, "job-001");
  assert.equal(q?.queued[0].title, "Launch walkthrough");
  assert.equal(q?.next?.id, "job-002");
  assert.equal(q?.recent_runs[0].duration_s, 42);
  assert.equal(normalizeWorkerQueue(null), null);
  assert.equal(normalizeWorkerQueue({}), null);
  assert.equal(normalizeWorkerQueue({ queued: [] })?.running.length, 0);
});

test("normalizeTrendingVideo keeps live fields and derives the watch URL", () => {
  const v = normalizeTrendingVideo({
    video_id: "dQw4w9WgXcQ", title: "Gaming clutch", channel_title: "Chan A",
    channel_id: "UCAAAA", published_at: "2026-11-21T10:00:00Z",
    view_count: 120000, like_count: 9000, comment_count: 800, trend_score: 87.4,
    url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  });
  assert.equal(v?.title, "Gaming clutch");
  assert.equal(v?.view_count, 120000);
  assert.equal(v?.trend_score, 87.4);
  assert.equal(v?.url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ");
  assert.equal(normalizeTrendingVideo({ title: "no id" }), null);
  assert.equal(normalizeTrendingVideo(null), null);
  assert.equal(normalizeTrendingVideos([{ video_id: "a" }, { title: "no id" }]).length, 1);
});

test("normalizeTrendingCredentials requires the usable flag", () => {
  const c = normalizeTrendingCredentials({
    oauth: { connected: true, expired: false, account_name: "My Channel" },
    api_key: { configured: false, last4: "", source: "none" },
    usable: true, message: "ok",
  });
  assert.equal(c?.oauth_connected, true);
  assert.equal(c?.oauth_account, "My Channel");
  assert.equal(c?.api_key_configured, false);
  assert.equal(normalizeTrendingCredentials({ oauth: {}, api_key: {} }), null);
});

test("normalizeTrendingJob keeps the trending topic and per-post video ids", () => {
  const job = normalizeTrendingJob({
    id: "job-1", title: "Agent run", source: "https://www.youtube.com/watch?v=AAAAAAAAAAA",
    status: "review", trending_video_id: "dQw4w9WgXcQ", trending_title: "Best clutch",
    trending_url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ", demo: true,
    posts: [{ id: "p1", platform: "youtube", status: "published", title: "Clip", scheduled_for: null, provider_post_id: "ZZZZZZZZZZZ", video_id: "ZZZZZZZZZZZ" }],
  });
  assert.equal(job?.trending_video_id, "dQw4w9WgXcQ");
  assert.equal(job?.posts[0].platform, "youtube");
  assert.equal(job?.posts[0].provider_post_id, "ZZZZZZZZZZZ");
  assert.equal(normalizeTrendingJob({}), null);
});

// --- v0.7.0: licences, CC clipping guardrail, thumbnails, attribution ---

test("normalizeVideoLicense maps YouTube and API licence spellings", () => {
  // YouTube's raw status.license values (trending / live / licence check).
  assert.equal(normalizeVideoLicense("creativeCommon"), "cc");
  assert.equal(normalizeVideoLicense("creativeCommons"), "cc");
  assert.equal(normalizeVideoLicense("youtube"), "standard");
  // Canonical keys from clip/job records (clipforge.licensing).
  assert.equal(normalizeVideoLicense("cc-by-4.0"), "cc");
  assert.equal(normalizeVideoLicense("cc-by-nc-4.0"), "cc");
  assert.equal(normalizeVideoLicense("cc-by-sa-3.0"), "cc");
  assert.equal(normalizeVideoLicense("cc0"), "cc");
  assert.equal(normalizeVideoLicense("CC-BY"), "cc");
  assert.equal(normalizeVideoLicense("cc"), "cc");
  assert.equal(normalizeVideoLicense("public-domain"), "public-domain");
  assert.equal(normalizeVideoLicense("standard"), "standard");
  assert.equal(normalizeVideoLicense("own"), "own");
  assert.equal(normalizeVideoLicense("bogus"), "unknown");
  assert.equal(normalizeVideoLicense(""), "unknown");
  assert.equal(normalizeVideoLicense(null), "unknown");
  assert.equal(normalizeVideoLicense(42), "unknown");
});

test("licenseBadgeLabel names each licence in plain language", () => {
  assert.equal(licenseBadgeLabel("cc"), "Creative Commons");
  assert.equal(licenseBadgeLabel("own"), "Your video");
  assert.equal(licenseBadgeLabel("standard"), "Standard licence");
  assert.equal(licenseBadgeLabel("unknown"), "Licence unknown");
});

test("deriveCanClip allows own + CC and fails closed otherwise", () => {
  assert.equal(deriveCanClip("cc", null).canClip, true);
  assert.equal(deriveCanClip("own", null).canClip, true);
  assert.equal(deriveCanClip("standard", null).canClip, false);
  assert.equal(deriveCanClip("unknown", null).canClip, false);
  // The API verdict wins over the derived one.
  assert.equal(deriveCanClip("cc", false).canClip, false);
  assert.equal(deriveCanClip("standard", true).canClip, true);
  assert.match(deriveCanClip("standard", null).reason, /Standard YouTube licence/);
  assert.match(deriveCanClip("unknown", null).reason, /No licence information/);
});

test("normalizeTrendingVideo carries licence, verdict and live fields", () => {
  const cc = normalizeTrendingVideo({
    video_id: "cc1", title: "CC talk", channel_title: "Open Chan",
    license: "creativeCommons", license_url: "https://creativecommons.org/licenses/by/4.0/",
    can_clip: true, reason: "CC BY",
  });
  assert.equal(cc?.license, "cc");
  assert.equal(cc?.can_clip, true);
  assert.equal(cc?.license_url, "https://creativecommons.org/licenses/by/4.0/");

  const std = normalizeTrendingVideo({ video_id: "std1", title: "Hit song", license: "youtube" });
  assert.equal(std?.license, "standard");
  assert.equal(std?.can_clip, false);
  assert.match(std?.reason ?? "", /Standard YouTube licence/);

  // No licence at all: fail closed.
  const unknown = normalizeTrendingVideo({ video_id: "u1", title: "Mystery" });
  assert.equal(unknown?.license, "unknown");
  assert.equal(unknown?.can_clip, false);

  // Trending search without licensed_only does NOT verify licences:
  // license "" means "not checked", never a guessed verdict.
  const unchecked = normalizeTrendingVideo({ video_id: "u2", title: "Unverified", license: "" });
  assert.equal(unchecked?.license_checked, false);
  assert.equal(unchecked?.can_clip, false);
  const checked = normalizeTrendingVideo({ video_id: "u3", title: "Verified CC", license: "creativeCommon" });
  assert.equal(checked?.license_checked, true);
  assert.equal(checked?.license_key, "creativeCommon");

  // Live results use the live.py field names: viewers, licence,
  // clip_reason, is_own (no event_type / concurrent_viewers keys).
  const live = normalizeTrendingVideo({
    video_id: "live1", title: "Live now",
    viewers: 1234, started_at: "2026-11-22T00:00:00Z",
    licence: "creativeCommon", can_clip: true, clip_reason: "Creative Commons licensed live stream — you can clip it.",
  });
  assert.equal(live?.event_type, "live");
  assert.equal(live?.concurrent_viewers, 1234);
  assert.equal(live?.started_at, "2026-11-22T00:00:00Z");
  assert.equal(live?.license, "cc");
  assert.equal(live?.can_clip, true);
  const ownLive = normalizeTrendingVideo({
    video_id: "live2", title: "My stream", viewers: 12, started_at: "2026-11-22T00:00:00Z",
    licence: "youtube", is_own: true, can_clip: true, clip_reason: "Your own live stream — you can clip it.",
  });
  assert.equal(ownLive?.license, "own");
  assert.equal(ownLive?.can_clip, true);
});

test("normalizeLicenseCheck normalises the /videos/license contract", () => {
  const check = normalizeLicenseCheck({
    video_id: "abc12345678", title: "Open tutorial", channel: "Open Chan",
    license: "creativeCommons", license_url: "https://creativecommons.org/licenses/by/4.0/",
    can_clip: true, reason: "CC BY 4.0",
  });
  assert.equal(check?.video_id, "abc12345678");
  assert.equal(check?.license, "cc");
  assert.equal(check?.can_clip, true);
  assert.equal(check?.channel, "Open Chan");
  const std = normalizeLicenseCheck({ video_id: "xyz", license: "youtube", can_clip: false });
  assert.equal(std?.license, "standard");
  assert.equal(std?.can_clip, false);
  assert.equal(std?.license_key, "youtube");
  // The licence check returns the raw YouTube value even for CC videos.
  assert.equal(check?.license_key, "creativeCommons");
  // A standard-licence video that is the user's own still returns
  // can_clip=true with an "own video" reason — the UI treats it as own.
  const own = normalizeLicenseCheck({
    video_id: "own1", license: "youtube", can_clip: true,
    reason: "This is your own video — you can clip it.",
  });
  assert.equal(own?.license, "own");
  assert.equal(own?.can_clip, true);
  assert.equal(normalizeLicenseCheck({ title: "no id" }), null);
  assert.equal(normalizeLicenseCheck(null), null);
});

test("normalizeCandidate carries thumbnail, variants, source licence and attribution", () => {
  const c = normalizeCandidate({
    id: "cand-1", job_id: "job-1", n: 1, score: 80, hook_rating: 8, hook_line: "Hook",
    reason: "r", transcript_excerpt: "t", start_s: 10, end_s: 40, status: "pending",
    file_path: null, seo: { youtube: { title: "T", description: "D", hashtags: ["#x"] } },
    thumbnail_url: "https://example.test/thumb.jpg",
    thumbnail_variants: ["https://example.test/t1.jpg", "https://example.test/t2.jpg"],
    thumbnails: [
      { variant: 1, filename: "thumbnail.jpg", url: "https://example.test/thumb.jpg" },
      { variant: 2, filename: "thumbnail_2.jpg", url: "https://example.test/t2.jpg" },
    ],
    source_license: "creativeCommons", source_url: "https://www.youtube.com/watch?v=abc12345678",
    source_title: "Open tutorial", source_channel: "Open Chan",
    attribution_text: "Source: \"Open tutorial\" by Open Chan — CC BY.",
  });
  assert.equal(c.thumbnail_url, "https://example.test/thumb.jpg");
  assert.deepEqual(c.thumbnail_variants, ["https://example.test/thumb.jpg", "https://example.test/t2.jpg"]);
  assert.deepEqual(c.thumbnails.map((t) => t.variant), [1, 2]);
  assert.equal(c.thumbnails[0].filename, "thumbnail.jpg");
  assert.equal(c.source_license, "cc");
  assert.equal(c.attribution_text, "Source: \"Open tutorial\" by Open Chan — CC BY.");
  // Missing v0.7.0 fields degrade to empty, never crash.
  const bare = normalizeCandidate({ id: "cand-2", job_id: "job-1", n: 2, seo: {} });
  assert.equal(bare.thumbnail_url, null);
  assert.deepEqual(bare.thumbnail_variants, []);
  assert.deepEqual(bare.thumbnails, []);
  assert.equal(bare.source_license, "unknown");
  assert.equal(bare.attribution_text, null);
  assert.equal(normalizeCandidates([{ id: "a" }, null]).length, 2);
  assert.deepEqual(normalizeCandidates(null), []);
});

test("normalizeThumbnailVariants accepts API objects and plain URL strings", () => {
  assert.deepEqual(
    normalizeThumbnailVariants([{ variant: 1, filename: "thumbnail.jpg", url: "https://example.test/t1.jpg" }, { variant: 2, url: "https://example.test/t2.jpg" }]).map((t) => t.url),
    ["https://example.test/t1.jpg", "https://example.test/t2.jpg"],
  );
  assert.deepEqual(normalizeThumbnailVariants(["https://example.test/a.jpg"]).map((t) => t.url), ["https://example.test/a.jpg"]);
  assert.deepEqual(normalizeThumbnailVariants(null), []);
  assert.deepEqual(normalizeThumbnailVariants([{ variant: 1 }]), []);
});

test("normalizeJob carries the v0.7.0 licence record", () => {
  const job = normalizeJob({
    id: "job-9", title: "CC job", status: "review",
    source_license: "cc-by-3.0", source_title: "Open tutorial", source_channel: "Open Chan",
    attribution_text: "Source: \"Open tutorial\" by Open Chan — Licensed under Creative Commons Attribution 3.0",
  });
  assert.equal(job.source_license, "cc");
  assert.equal(job.source_title, "Open tutorial");
  assert.equal(job.source_channel, "Open Chan");
  assert.match(job.attribution_text ?? "", /Open tutorial/);
  const bare = normalizeJob({ id: "job-10", status: "pending" });
  assert.equal(bare.source_license, "unknown");
  assert.equal(bare.attribution_text, null);
});

test("jobSourceLicense sends the server a canonical key for a clip job", () => {
  // YouTube's raw CC licence is CC BY: the server's own tests use cc-by-3.0.
  assert.equal(jobSourceLicense({ license: "cc", license_key: "creativeCommon" }), "cc-by-3.0");
  assert.equal(jobSourceLicense({ license: "cc", license_key: "" }), "cc-by-3.0");
  // A canonical key passes through unchanged.
  assert.equal(jobSourceLicense({ license: "cc", license_key: "cc-by-sa-4.0" }), "cc-by-sa-4.0");
  assert.equal(jobSourceLicense({ license: "own", license_key: "own" }), "own");
  assert.equal(jobSourceLicense({ license: "public-domain", license_key: "public-domain" }), "public-domain");
  assert.equal(jobSourceLicense({ license: "standard", license_key: "youtube" }), "standard");
});

test("attributionForClip returns the credit for CC sources only", () => {
  const cc = {
    source_license: "cc" as const, source_title: "Open tutorial", source_channel: "Open Chan",
    source_url: "https://www.youtube.com/watch?v=abc12345678", attribution_text: null,
  };
  const text = attributionForClip(cc);
  assert.match(text ?? "", /Open tutorial/);
  assert.match(text ?? "", /Open Chan/);
  assert.match(text ?? "", /Creative Commons/);
  assert.equal(attributionForClip({ ...cc, attribution_text: "Custom credit." }), "Custom credit.");
  assert.equal(attributionForClip({ ...cc, source_license: "own" }), null);
  assert.equal(attributionForClip({ ...cc, source_license: "standard" }), null);
  const pd = attributionForClip({ ...cc, source_license: "public-domain" });
  assert.match(pd ?? "", /public domain/);
  assert.match(pd ?? "", /Open tutorial/);
});

test("normalizePerfProfile reads the nested hardware object from GET /perf/profile", () => {
  const p = normalizePerfProfile({
    profile: "low", mode: "auto",
    hardware: { cpu_cores: 4, ram_gb: 7.7, gpu: null, platform: "Linux" },
    settings: { whisper_model: "tiny" },
  });
  assert.equal(p?.profile, "low");
  assert.equal(p?.cpu_cores, 4);
  assert.equal(p?.ram_gb, 7.7);
  assert.equal(p?.gpu, null);
  assert.equal(p?.override, false);
  const over = normalizePerfProfile({
    profile: "high", mode: "high",
    hardware: { cpu_cores: 16, ram_gb: 32, gpu: "NVIDIA RTX", platform: "Linux" },
    settings: {},
  });
  assert.equal(over?.override, true);
  assert.equal(over?.gpu, "NVIDIA RTX");
  assert.equal(normalizePerfProfile({}), null);
});

test("normalizeBatchStatus reads clip counts from item.result (real processor)", () => {
  const b = normalizeBatchStatus({
    id: "abc123", status: "done",
    items: [
      { source: "/tmp/a.mp4", state: "done", progress: 100, result: { job_id: "j1", clips: 3, status: "ready" } },
      { source: "/tmp/b.mp4", state: "failed", progress: 0, error: "Source not found" },
    ],
  });
  assert.equal(b?.batch_id, "abc123");
  assert.equal(b?.items[0].clips, 3);
  assert.equal(b?.items[1].clips, 0);
  assert.equal(normalizeBatchStatus({}), null);
});

test("normalizeDetectionCandidate keeps all six signal scores", () => {
  const c = normalizeDetectionCandidate({
    start: 12, end: 40, score: 82, hook_score: 90, peak_score: 70,
    payoff_score: 80, emotion_score: 60, visual_score: 50,
    completeness_score: 95, title: "The hook", reason: "Strong hook",
  });
  assert.equal(c.score, 82);
  assert.equal(c.hook_score, 90);
  assert.equal(c.completeness_score, 95);
  assert.equal(c.title, "The hook");
});

test("normalizeNotifications keeps real UI-alias items, drops broken ones, defaults tone to info", () => {
  const items = normalizeNotifications([
    { id: "n1", title: "Clip ready", body: "3 clips wait for approval.", tone: "success", read: false, created_at: "2026-11-23T00:00:00Z", link: "/queue" },
    { id: "n2", title: "Token expiring", tone: "danger", read: true },
    { id: "n3", title: "Unknown tone falls back to info", tone: "bogus" },
    { title: "No id — dropped" },
    { id: "n4" },
  ]);
  assert.equal(items.length, 3);
  assert.equal(items[0].tone, "success");
  assert.equal(items[0].link, "/queue");
  assert.equal(items[1].tone, "danger");
  assert.equal(items[1].read, true);
  assert.equal(items[2].tone, "info");
  assert.equal(normalizeNotifications(null).length, 0);
  assert.equal(normalizeNotification({}), null);
});
test("normalizeWorkerQueue keeps the v0.9.0 paused list, active job and queue positions", () => {
  const q = normalizeWorkerQueue({
    running: [],
    queued: [{ id: "j1", title: "First", source_type: "local", status: "queued", queue_position: 1, created_at: null }],
    paused: [{ id: "j2", title: "Paused one", source_type: "local", status: "paused", created_at: null }],
    active_job_id: "j9",
    next: { id: "j1", title: "First" },
    recent_runs: [],
  });
  assert.equal(q?.queued[0].queue_position, 1);
  assert.equal(q?.paused.length, 1);
  assert.equal(q?.paused[0].id, "j2");
  assert.equal(q?.active_job_id, "j9");
  const legacy = normalizeWorkerQueue({ running: [], queued: [], recent_runs: [] });
  assert.deepEqual(legacy?.paused, []);
  assert.equal(legacy?.active_job_id, null);
});

test("startAgentOnSource sends enabled:true with the source, then starts the agent", async () => {
  // v0.9.1 regression: the dashboard used to PUT the source/niche
  // WITHOUT enabled, so POST /api/agent/start 422d "agent is off" and
  // pasting ANY video link started nothing. The save-then-start
  // sequence must carry enabled:true.
  const calls: Array<{ url: string; method: string; body: Record<string, unknown> }> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (url: unknown, init?: { method?: string; body?: string }) => {
    calls.push({
      url: String(url),
      method: init?.method ?? "GET",
      body: init?.body ? (JSON.parse(init.body) as Record<string, unknown>) : {},
    });
    return { ok: true, status: 200, json: async () => ({ ok: true }) } as unknown as Response;
  }) as typeof fetch;
  try {
    const res = await startAgentOnSource({
      source_type: "video",
      source_url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      niche: "gaming",
      rights_confirmed: true,
    });
    assert.equal(res.ok, true);
    assert.equal(calls.length, 2);
    assert.equal(calls[0].method, "PUT");
    assert.equal(calls[0].url, `${API_URL}/api/agent/config`);
    assert.equal(calls[0].body.enabled, true);
    assert.equal(calls[0].body.source_url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ");
    assert.equal(calls[0].body.niche, "gaming");
    assert.equal(calls[0].body.rights_confirmed, true);
    assert.equal(calls[1].method, "POST");
    assert.equal(calls[1].url, `${API_URL}/api/agent/start`);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("startAgentOnSource rolls the toggle back off when the start fails", async () => {
  // A failed start must not leave enabled:true saved: the Agent page
  // toggle would show a stuck ON for an agent that never started.
  const calls: Array<{ url: string; method: string; body: Record<string, unknown> }> = [];
  const originalFetch = globalThis.fetch;
  let n = 0;
  globalThis.fetch = (async (url: unknown, init?: { method?: string; body?: string }) => {
    n += 1;
    calls.push({
      url: String(url),
      method: init?.method ?? "GET",
      body: init?.body ? (JSON.parse(init.body) as Record<string, unknown>) : {},
    });
    if (n === 2) {
      return {
        ok: false, status: 422,
        json: async () => ({ detail: "yt-dlp is not installed — install it first." }),
      } as unknown as Response;
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) } as unknown as Response;
  }) as typeof fetch;
  try {
    const res = await startAgentOnSource({
      source_type: "video",
      source_url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      niche: "gaming",
      rights_confirmed: true,
    });
    assert.equal(res.ok, false);
    assert.match(res.error ?? "", /yt-dlp/);
    assert.equal(calls.length, 3);
    assert.equal(calls[2].method, "PUT");
    assert.equal(calls[2].url, `${API_URL}/api/agent/config`);
    assert.equal(calls[2].body.enabled, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("startAgentOnSource returns the save error without starting the agent", async () => {
  const calls: string[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (url: unknown, init?: { method?: string; body?: string }) => {
    calls.push(`${init?.method ?? "GET"} ${String(url)}`);
    return {
      ok: false, status: 422,
      json: async () => ({ detail: "Give the agent a source URL starting with http:// or https://." }),
    } as unknown as Response;
  }) as typeof fetch;
  try {
    const res = await startAgentOnSource({
      source_type: "video",
      source_url: "not-a-url",
      niche: "gaming",
      rights_confirmed: true,
    });
    assert.equal(res.ok, false);
    assert.equal(calls.length, 1);
    assert.match(calls[0], /PUT .*\/api\/agent\/config/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
