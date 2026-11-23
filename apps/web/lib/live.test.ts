// Unit tests for the live-monitor helpers (lib/live.ts).
// Run with `npm run test:unit` — same Node built-in runner as api.test.ts.
import test from "node:test";
import assert from "node:assert/strict";
import {
  fmtElapsed,
  fmtPeakWindow,
  isLiveSessionActive,
  liveChatLabel,
  liveSessionState,
  missedPeaks,
  peaksWithJobs,
} from "./live.js";
import type { LivePeak, LiveSession } from "./api.js";

function session(status: string, peaks: LivePeak[] = []): LiveSession {
  return {
    session_id: "abc123", mode: "live", platform: "youtube",
    url: "https://www.youtube.com/watch?v=abc12345678", title: "Stream",
    channel: "Chan", status, error: "", started_at: 0, ended_at: null,
    elapsed_s: 65, peaks, missed_peaks: 0, chat_on: false, chat_total: 0,
    transcript_ok: true, transcript_note: "", words: 10, stalled: 0,
    level: [], base: {}, transcript_tail: "", recent_chat: [], buffer_s: 60,
  };
}

function peak(n: number, trigger: number, jobId: string | null, error = ""): LivePeak {
  return {
    n, trigger_s: trigger, start_s: trigger - 12, end_s: trigger + 28,
    reasons: ["loudness +120%"], score: 80, kind: "spike", transcript: "",
    file: `/tmp/peak_${n}.mp4`, job_id: jobId, error,
  };
}

test("liveSessionState maps starting/live to active, failed to failed", () => {
  assert.equal(liveSessionState(session("starting")), "active");
  assert.equal(liveSessionState(session("live")), "active");
  assert.equal(liveSessionState(session("ended")), "ended");
  assert.equal(liveSessionState(session("failed")), "failed");
  assert.equal(liveSessionState(null), "unknown");
  assert.equal(isLiveSessionActive(session("live")), true);
  assert.equal(isLiveSessionActive(session("ended")), false);
});

test("fmtElapsed formats mm:ss", () => {
  assert.equal(fmtElapsed(0), "0:00");
  assert.equal(fmtElapsed(65), "1:05");
  assert.equal(fmtElapsed(3600), "60:00");
  assert.equal(fmtElapsed(-5), "0:00");
});

test("fmtPeakWindow shows the 12s/28s golden window", () => {
  assert.equal(fmtPeakWindow(peak(1, 65, "job1")), "53.0s – 93.0s");
});

test("liveChatLabel is honest per platform", () => {
  assert.equal(liveChatLabel(true), "Chat: yes, no key needed");
  assert.equal(liveChatLabel("oauth"), "Chat: needs your channel connected (OAuth)");
  assert.equal(liveChatLabel(false), "Chat: not available (audio-only)");
});

test("peaksWithJobs returns only jobbed peaks, newest first", () => {
  const s = session("live", [peak(1, 65, "job1"), peak(2, 130, null, "cut failed"), peak(3, 200, "job3")]);
  assert.deepEqual(peaksWithJobs(s).map((p) => p.n), [3, 1]);
  assert.deepEqual(missedPeaks(s).map((p) => p.n), [2]);
  assert.deepEqual(peaksWithJobs(null), []);
});
