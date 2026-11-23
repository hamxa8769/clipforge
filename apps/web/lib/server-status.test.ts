// Unit tests for the single server-status source of truth
// (lib/server-status.ts). Run with `npm run test:unit` — compiles this
// file + server-status.ts with tsc and runs it under Node's built-in
// test runner. No extra test dependency.
import test from "node:test";
import assert from "node:assert/strict";
import {
  formatLastSeenAge,
  INITIAL_SERVER_STATUS,
  LIVE_POLL_MS,
  reconnectDelayMs,
  reduceServerStatus,
  serverStatusLabel,
} from "./server-status.js";

test("reconnectDelayMs backs off exponentially and caps at 15 s", () => {
  assert.equal(reconnectDelayMs(0), 1000);
  assert.equal(reconnectDelayMs(1), 2000);
  assert.equal(reconnectDelayMs(2), 4000);
  assert.equal(reconnectDelayMs(3), 8000);
  assert.equal(reconnectDelayMs(4), 15000);
  assert.equal(reconnectDelayMs(10), 15000);
  assert.equal(reconnectDelayMs(-1), 1000);
});

test("LIVE_POLL_MS is 15 s — the dashboard's poll interval", () => {
  assert.equal(LIVE_POLL_MS, 15000);
});

test("serverStatusLabel names each state, with the attempt count while reconnecting", () => {
  assert.equal(serverStatusLabel("checking", 0), "Checking server…");
  assert.equal(serverStatusLabel("live", 0), "Live — server connected");
  assert.equal(serverStatusLabel("reconnecting", 1), "Reconnecting… (attempt 1)");
  assert.equal(serverStatusLabel("reconnecting", 3), "Reconnecting… (attempt 3)");
});

test("reduceServerStatus: first successful probe goes live and resets attempts", () => {
  const health = { status: "ok", dry_run: true, version: "0.9.2" };
  const next = reduceServerStatus(INITIAL_SERVER_STATUS, { ok: true, health, now: 1000 });
  assert.equal(next.status, "live");
  assert.equal(next.attempts, 0);
  assert.equal(next.health, health);
  assert.equal(next.lastLiveAt, 1000);
});

test("reduceServerStatus: first failed probe leaves checking and starts reconnecting at attempt 1", () => {
  const next = reduceServerStatus(INITIAL_SERVER_STATUS, { ok: false, health: null, now: 1000 });
  assert.equal(next.status, "reconnecting");
  assert.equal(next.attempts, 1);
  assert.equal(next.health, null);
  assert.equal(next.lastLiveAt, null);
});

test("reduceServerStatus: a failure while live keeps lastLiveAt and counts attempts", () => {
  const live = reduceServerStatus(INITIAL_SERVER_STATUS, {
    ok: true, health: { status: "ok", dry_run: false }, now: 5000,
  });
  assert.equal(live.status, "live");
  const down = reduceServerStatus(live, { ok: false, health: null, now: 20000 });
  assert.equal(down.status, "reconnecting");
  assert.equal(down.attempts, 1);
  assert.equal(down.lastLiveAt, 5000);
  assert.equal(down.health, null);
  const down2 = reduceServerStatus(down, { ok: false, health: null, now: 21000 });
  assert.equal(down2.attempts, 2);
  assert.equal(down2.lastLiveAt, 5000);
});

test("reduceServerStatus: recovery after failures resets attempts and refreshes lastLiveAt", () => {
  const live = reduceServerStatus(INITIAL_SERVER_STATUS, {
    ok: true, health: { status: "ok", dry_run: true }, now: 1000,
  });
  const down = reduceServerStatus(live, { ok: false, health: null, now: 2000 });
  const back = reduceServerStatus(down, {
    ok: true, health: { status: "ok", dry_run: true, version: "0.9.3" }, now: 9000,
  });
  assert.equal(back.status, "live");
  assert.equal(back.attempts, 0);
  assert.equal(back.lastLiveAt, 9000);
  assert.equal(back.health?.version, "0.9.3");
});

test("reduceServerStatus: a probe that claims ok but carries no payload counts as down", () => {
  // getHealth() returns null on any failure, so ok=true with a null
  // payload must never flip the UI to live.
  const next = reduceServerStatus(INITIAL_SERVER_STATUS, { ok: true, health: null, now: 1000 });
  assert.equal(next.status, "reconnecting");
  assert.equal(next.attempts, 1);
});

test("formatLastSeenAge says how long ago the server was last seen", () => {
  assert.equal(formatLastSeenAge(null, 10000), null);
  assert.equal(formatLastSeenAge(10000, 10000), "just now");
  assert.equal(formatLastSeenAge(10000, 15000), "just now");
  assert.equal(formatLastSeenAge(10000, 55000), "45 s ago");
  assert.equal(formatLastSeenAge(10000, 70000), "1 min ago");
  assert.equal(formatLastSeenAge(10000, 190000), "3 min ago");
  assert.equal(formatLastSeenAge(10000, 3700000), "1 h ago");
  // A clock going backwards never shows a negative age.
  assert.equal(formatLastSeenAge(20000, 10000), "just now");
});

test("reduceServerStatus never returns to checking once a probe has settled", () => {
  const down = reduceServerStatus(INITIAL_SERVER_STATUS, { ok: false, health: null, now: 1000 });
  const back = reduceServerStatus(down, {
    ok: true, health: { status: "ok", dry_run: true }, now: 2000,
  });
  const downAgain = reduceServerStatus(back, { ok: false, health: null, now: 3000 });
  assert.equal(downAgain.status, "reconnecting");
});
