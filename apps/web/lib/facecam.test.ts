// Unit tests for the Facecam Studio helpers (lib/facecam.ts).
// Run with `npm run test:unit` — compiles this file + facecam.ts with
// tsc and runs it under Node's built-in test runner.
import test from "node:test";
import assert from "node:assert/strict";
import {
  boxToStyle,
  captionYFrac,
  clampBox,
  defaultFacecamLayout,
  pipRectFrac,
  splitBandFrac,
  transcriptLinesToWords,
} from "./facecam";

test("clampBox keeps boxes inside the frame with a minimum size", () => {
  const a = clampBox({ x: -0.2, y: 1.2, w: 2, h: 0.01 });
  assert.equal(a.x, 0);
  assert.equal(a.y, 0.95);
  assert.ok(Math.abs(a.w - 1) < 1e-9);
  assert.ok(Math.abs(a.h - 0.05) < 1e-9);
  const b = clampBox({ x: 0.9, y: 0.9, w: 0.5, h: 0.5 });
  assert.ok(Math.abs(b.w - 0.1) < 1e-9);
  assert.ok(Math.abs(b.h - 0.1) < 1e-9);
});

test("boxToStyle converts a normalised box to CSS percentages", () => {
  assert.deepEqual(boxToStyle({ x: 0.25, y: 0, w: 0.5, h: 0.35 }), {
    left: "25%",
    top: "0%",
    width: "50%",
    height: "35%",
  });
});

test("split band follows split_ratio within 0.20..0.80", () => {
  const layout = defaultFacecamLayout("split");
  assert.equal(splitBandFrac(layout), 0.35);
  assert.equal(splitBandFrac({ ...layout, split_ratio: 0.05 }), 0.2);
  assert.equal(splitBandFrac({ ...layout, split_ratio: 0.95 }), 0.8);
});

test("caption line sits on the gameplay half for split layouts", () => {
  const layout = defaultFacecamLayout("split");
  const y = captionYFrac(layout);
  assert.ok(y > 0.35 && y < 0.9, `caption y frac ${y}`);
  // An explicit override wins.
  assert.equal(captionYFrac({ ...layout, caption_y: 960 }), 0.5);
});

test("pip rect stays inside the canvas for every corner", () => {
  for (const pos of ["top-right", "top-left", "bottom-left", "bottom-right"] as const) {
    const layout = { ...defaultFacecamLayout("pip"), pip_pos: pos };
    const r = pipRectFrac(layout);
    assert.ok(r.x >= 0 && r.x + r.w <= 1, `${pos} x`);
    assert.ok(r.y >= 0 && r.y + r.h <= 1, `${pos} y`);
    assert.ok(r.w > 0 && r.h > 0, `${pos} size`);
  }
});

test("transcript lines become evenly-spaced word timings", () => {
  const words = transcriptLinesToWords([
    { start: 0, end: 3, text: "hello brave new world" },
    { start: 3, end: 4 },
  ]);
  assert.equal(words.length, 4);
  assert.deepEqual(words[0], { text: "hello", start: 0, end: 0.75 });
  assert.deepEqual(words[3], { text: "world", start: 2.25, end: 3 });
});
