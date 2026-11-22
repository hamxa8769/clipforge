// Unit tests for the approve-queue summary (lib/review.ts).
// Run with `npm run test:unit` — same Node built-in runner as api.test.ts.
import test from "node:test";
import assert from "node:assert/strict";
import { summarizeApproveQueue } from "./review.js";

test("summarizeApproveQueue splits queued / skipped / existing per platform", () => {
  const summary = summarizeApproveQueue([
    { platform: "youtube", status: "queued" },
    { platform: "instagram", status: "skipped_no_account", reason: "no account connected" },
    { platform: "facebook", status: "skipped_no_account" },
    { platform: "tiktok", status: "skipped_cap" },
    { platform: "youtube", status: "exists" },
  ]);
  assert.deepEqual(summary.queued, ["youtube"]);
  assert.deepEqual(summary.alreadyExisted, ["youtube"]);
  assert.deepEqual(summary.skippedNoAccount, ["instagram", "facebook"]);
  assert.deepEqual(summary.skippedCap, ["tiktok"]);
  assert.equal(summary.total, 5);
});

test("summarizeApproveQueue handles a missing or empty queue", () => {
  assert.deepEqual(summarizeApproveQueue(undefined), {
    queued: [],
    alreadyExisted: [],
    skippedNoAccount: [],
    skippedCap: [],
    total: 0,
  });
  assert.deepEqual(summarizeApproveQueue([]), {
    queued: [],
    alreadyExisted: [],
    skippedNoAccount: [],
    skippedCap: [],
    total: 0,
  });
  assert.deepEqual(summarizeApproveQueue("not-an-array"), {
    queued: [],
    alreadyExisted: [],
    skippedNoAccount: [],
    skippedCap: [],
    total: 0,
  });
});

test("summarizeApproveQueue counts a no-channel approval as zero posts", () => {
  const summary = summarizeApproveQueue([
    { platform: "youtube", status: "skipped_no_account" },
    { platform: "instagram", status: "skipped_no_account" },
    { platform: "facebook", status: "skipped_no_account" },
    { platform: "tiktok", status: "skipped_no_account" },
  ]);
  assert.equal(summary.queued.length, 0);
  assert.equal(summary.skippedNoAccount.length, 4);
  assert.equal(summary.total, 4);
});
