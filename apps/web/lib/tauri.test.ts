// Unit tests for the updater bridge helpers (lib/tauri.ts, v0.10.2).
// Run with `npm run test:unit` — compiles this file + tauri.ts with
// tsc and runs it under Node's built-in test runner. No extra test
// dependency. Pure logic only: `isTauri()` is false under Node, so no
// Tauri invoke is ever attempted here.
//
// These pin the v0.10.2 fix for "updates do nothing": a busy check
// ("already in progress" — the launch check still running) must be
// retried, not shown as a failure, and the launch check's settled
// result must be adoptable from a get_update_status snapshot without
// a second GitHub fetch.
import test from "node:test";
import assert from "node:assert/strict";
import { isUpdateCheckBusy, updateInfoFromStatus, type UpdateStatus } from "./tauri.js";

function status(partial: Partial<UpdateStatus> & { state: string }): UpdateStatus {
  return partial as UpdateStatus;
}

test("isUpdateCheckBusy only matches the busy-check error", () => {
  assert.equal(isUpdateCheckBusy("An update check is already in progress — try again in a few seconds."), true);
  assert.equal(isUpdateCheckBusy("An update check is already in progress."), true);
  assert.equal(isUpdateCheckBusy("Could not reach GitHub: dns error"), false);
  assert.equal(isUpdateCheckBusy("GitHub rate limit hit — try again later."), false);
  assert.equal(isUpdateCheckBusy(undefined), false);
  assert.equal(isUpdateCheckBusy(null), false);
  assert.equal(isUpdateCheckBusy(""), false);
});

test("updateInfoFromStatus adopts an available launch-check result", () => {
  const info = updateInfoFromStatus(
    status({ state: "available", version: "0.10.1", size_bytes: 45985124 }),
    "0.9.3",
  );
  assert.deepEqual(info, {
    available: true,
    version: "0.10.1",
    current_version: "0.9.3",
    size_bytes: 45985124,
  });
});

test("updateInfoFromStatus adopts downloading / ready snapshots too", () => {
  const downloading = updateInfoFromStatus(
    status({ state: "downloading", version: "0.10.1", progress_pct: 42, size_bytes: 45985124 }),
    "0.9.3",
  );
  assert.equal(downloading?.available, true);
  assert.equal(downloading?.version, "0.10.1");
  const ready = updateInfoFromStatus(status({ state: "ready", version: "0.10.1" }), "0.9.3");
  assert.equal(ready?.available, true);
});

test("updateInfoFromStatus returns null when there is no offer to adopt", () => {
  for (const state of ["idle", "checking", "failed", "error"]) {
    assert.equal(updateInfoFromStatus(status({ state, version: "0.10.1" }), "0.9.3"), null, state);
  }
  // Available but no version string: nothing to show.
  assert.equal(updateInfoFromStatus(status({ state: "available" }), "0.9.3"), null);
  // A failed snapshot keeps its error for the caller to display.
  const failed = status({ state: "failed", error: "Could not reach GitHub: dns error" });
  assert.equal(updateInfoFromStatus(failed, "0.9.3"), null);
  assert.equal(failed.error, "Could not reach GitHub: dns error");
});
