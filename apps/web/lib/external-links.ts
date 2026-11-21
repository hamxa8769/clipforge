// External-link allow-list (v0.4.2) — the single definition of "a URL the
// app may open in the system browser".
//
// The desktop app is a Tauri WebView: `window.open(url, "_blank")` opens
// nothing there (the WebView has no new-window handler) and plain
// `<a target="_blank">` links only reach the browser through the opener
// plugin, which the ACL denies unless a capability grants it. Every
// external link in the UI therefore goes through `openExternal()`
// (lib/tauri.ts), which asks the desktop shell to open the URL — and the
// shell only opens URLs this function accepts. Keep the two in sync: the
// Rust side (`is_allowed_external_url` in
// apps/desktop/src-tauri/src/main.rs) enforces the same rule, so a bad
// URL is refused twice.
//
// Pure (no `window`, no DOM) so it is unit-testable under Node — see
// lib/external-links.test.ts.

/** True for absolute http(s) URLs with a host and no whitespace, control characters or backslashes. */
export function isExternalUrl(url: string): boolean {
  const match = /^https?:\/\//i.exec(url);
  if (!match) return false;
  const afterScheme = url.slice(match[0].length);
  if (afterScheme.length === 0) return false;
  return !/[\s\x00-\x1f\x7f\\]/.test(url);
}
