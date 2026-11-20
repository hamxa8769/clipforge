// Unit tests for the API status contract (lib/api.ts).
// Run with `npm run test:unit` — compiles this file + api.ts with tsc and
// runs it under Node's built-in test runner. No extra test dependency.
import test from "node:test";
import assert from "node:assert/strict";
import {
  clipVideoUrl,
  normalizeJob,
  normalizeJobs,
  normalizeJobStatus,
  normalizePost,
  normalizePostStatus,
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
