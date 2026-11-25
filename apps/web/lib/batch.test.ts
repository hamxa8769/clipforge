// Unit tests for the batch helpers (lib/batch.ts).
// Run with `npm run test:unit` — same Node built-in runner as api.test.ts.
import test from "node:test";
import assert from "node:assert/strict";
import {
  batchStartError,
  buildBatchPayload,
  extractBatchItemErrors,
  isBatchTerminal,
} from "./batch.js";

test("buildBatchPayload sends plain strings + the declaration flag", () => {
  assert.deepEqual(buildBatchPayload([" /tmp/a.mp4 ", "https://example.com/v"], true), {
    sources: ["/tmp/a.mp4", "https://example.com/v"],
    own_content_declaration: true,
  });
  assert.deepEqual(buildBatchPayload(["  ", ""], false), {
    sources: [],
    own_content_declaration: false,
  });
});

test("isBatchTerminal only for done / partial", () => {
  for (const s of ["done", "partial", "DONE", "Partial"]) assert.equal(isBatchTerminal(s), true);
  for (const s of ["queued", "processing", "paused", "running", ""]) assert.equal(isBatchTerminal(s), false);
});

test("extractBatchItemErrors keeps only non-empty item errors", () => {
  const raw = {
    items: [
      { source: "/tmp/a.mp4", state: "failed", error: "faster-whisper is not installed" },
      { source: "/tmp/b.mp4", state: "done", error: "" },
      { source: "/tmp/c.mp4", state: "queued" },
      { source: "/tmp/d.mp4", state: "failed", error: "   " },
    ],
  };
  const errors = extractBatchItemErrors(raw);
  assert.equal(errors.size, 1);
  assert.equal(errors.get("/tmp/a.mp4"), "faster-whisper is not installed");
  assert.equal(extractBatchItemErrors(null).size, 0);
  assert.equal(extractBatchItemErrors({}).size, 0);
});

test("batchStartError turns a FastAPI validation list into readable text", () => {
  const res = {
    ok: false,
    status: 422,
    data: {
      detail: [
        { type: "string_type", loc: ["body", "sources", 0], msg: "Input should be a valid string", input: { path: "/tmp/a.mp4" } },
      ],
    },
    error: "Request failed (422)",
  };
  assert.equal(batchStartError(res), "sources.0: Input should be a valid string");
  assert.equal(
    batchStartError({ ok: false, status: 422, data: { detail: "YouTube URLs need own_content_declaration=true" } }),
    "YouTube URLs need own_content_declaration=true",
  );
  assert.equal(
    batchStartError({ ok: false, status: 0, data: null, error: "API unreachable" }),
    "API unreachable",
  );
  assert.equal(batchStartError({ ok: true, status: 200, data: {} }), "");
});
