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
  normalizeChannelStatus,
  normalizeChannels,
  normalizeFfmpegInstallStatus,
  normalizeJob,
  normalizeJobs,
  normalizeJobStatus,
  normalizePost,
  normalizePostStatus,
  normalizeSettings,
  pipelineProgress,
  pipelineStageStates,
  resolveApiUrl,
  RUNNING_STATUSES,
  sourceVideoUrl,
  API_URL,
} from "./api.js";

test("normalizeJobStatus passes backend statuses through unchanged", () => {
  for (const s of ["pending", "transcribing", "scoring", "rendering", "review", "ready", "failed"]) {
    assert.equal(normalizeJobStatus(s), s);
  }
});

test("normalizeJobStatus maps the legacy 'queued' alias to 'pending'", () => {
  assert.equal(normalizeJobStatus("queued"), "pending");
});

test("normalizeJobStatus falls back to 'pending' for unknown or non-string values", () => {
  for (const bad of ["bogus", "", null, undefined, 42, {}, ["pending"]]) {
    assert.equal(normalizeJobStatus(bad), "pending");
  }
});

test("normalizeJob normalises the status of a raw job payload", () => {
  const job = normalizeJob({ id: "j1", title: "t", source: "s.mp4", status: "queued", created_at: "2026-11-20", candidates: 2 });
  assert.equal(job.status, "pending");
  assert.equal(job.candidates, 2);
  const empty = normalizeJob({ status: "ready" });
  assert.equal(empty.id, "");
  assert.equal(empty.candidates, 0);
});

test("normalizeJobs maps an array and drops non-arrays", () => {
  assert.deepEqual(normalizeJobs([{ status: "queued" }, { status: "ready" }]).map((j) => j.status), ["pending", "ready"]);
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
  // "pending" has not started yet (it is queued) and "review" has
  // finished rendering and waits for a human — neither is "running".
  assert.deepEqual([...RUNNING_STATUSES], ["transcribing", "scoring", "rendering"]);
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
  });
  assert.equal(card.status, "expired");
  assert.equal(card.daily_cap, 6);
  assert.equal(card.source, "oauth");
  assert.equal(card.can_refresh, true);
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
