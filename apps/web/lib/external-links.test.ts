// Unit tests for the external-link allow-list (lib/external-links.ts).
// Run with `npm run test:unit` — compiles this file + external-links.ts
// with tsc and runs it under Node's built-in test runner. No extra test
// dependency. Must stay in sync with the Rust tests for
// `is_allowed_external_url` (apps/desktop/src-tauri/src/main.rs): the
// same URL must be allowed/refused on both sides.
import test from "node:test";
import assert from "node:assert/strict";
import { isExternalUrl } from "./external-links.js";

test("isExternalUrl accepts every link the UI opens", () => {
  for (const url of [
    "https://accounts.google.com/o/oauth2/v2/auth?client_id=x",
    "https://console.cloud.google.com/apis/credentials",
    "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
    "https://developers.facebook.com/apps",
    "https://developers.tiktok.com/",
    "https://www.facebook.com/v19.0/dialog/oauth",
    "https://www.tiktok.com/v2/auth/authorize/",
    "https://ffmpeg.org/download.html",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/hamxa8769/clipforge/releases",
    "http://127.0.0.1:17847/auth/callback/youtube",
    "HTTPS://EXAMPLE.COM/UPPER",
  ]) {
    assert.equal(isExternalUrl(url), true, url);
  }
});

test("isExternalUrl refuses anything but a plain http(s) URL", () => {
  for (const url of [
    "",
    "https://",
    "http://",
    "ftp://example.com/x",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "data:text/html,<h1>x</h1>",
    "mailto:someone@example.com",
    "/channels/connect",
    "https://example.com/has space",
    "https://example.com/tab\there",
    "https://example.com/back\\slash",
    "https://example.com\nnewline",
  ]) {
    assert.equal(isExternalUrl(url), false, JSON.stringify(url));
  }
});
