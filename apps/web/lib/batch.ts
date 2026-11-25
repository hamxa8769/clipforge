// Batch helpers (Worker A audit fix, v0.9.0).
//
// The batch page used to send the wrong payload shape to POST /batch
// (objects `{url}` / `{path}` — the server requires plain strings) and
// never sent `own_content_declaration`, so every batch start from the UI
// failed with a 422. These pure helpers build the correct payload, read
// per-item errors out of the raw batch response (the normaliser in
// lib/api.ts drops `error`), and turn a failed start into a readable
// message instead of the generic "Request failed (422)".

export interface BatchPayload {
  sources: string[];
  own_content_declaration: boolean;
}

/** The exact body POST /batch expects: plain string sources + the
 * own-content declaration the server requires for YouTube URLs
 * (sent with every batch since v0.11.0 — no checkbox in the UI). */
export function buildBatchPayload(sources: string[], ownContent: boolean): BatchPayload {
  return {
    sources: sources.map((s) => s.trim()).filter(Boolean),
    own_content_declaration: ownContent,
  };
}

/**
 * Batch-level statuses that mean the batch will not progress any further
 * (see `Batch.status` in src/clipforge/batch_queue.py). A new batch may
 * be started once the previous one is terminal.
 */
export function isBatchTerminal(status: string): boolean {
  return ["done", "partial"].includes(status.toLowerCase());
}

/**
 * Per-item error text from a raw GET /batch response, keyed by source.
 * Items that failed carry `error` (e.g. "faster-whisper … missing");
 * items without an error are left out.
 */
export function extractBatchItemErrors(raw: unknown): Map<string, string> {
  const out = new Map<string, string>();
  const r = (raw ?? {}) as Record<string, unknown>;
  const items = Array.isArray(r.items) ? r.items : [];
  for (const it of items) {
    const q = (it ?? {}) as Record<string, unknown>;
    if (typeof q.source === "string" && typeof q.error === "string" && q.error.trim()) {
      out.set(q.source, q.error);
    }
  }
  return out;
}

export interface BatchStartResult {
  ok: boolean;
  status: number;
  data: unknown;
  error?: string;
}

/**
 * Readable message for a failed POST /batch. FastAPI validation errors
 * arrive as `detail: [{loc, msg, ...}]` — join the first messages
 * (e.g. "sources.0: Input should be a valid string") instead of the
 * generic "Request failed (422)" the generic POST helper returns.
 */
export function batchStartError(res: BatchStartResult): string {
  if (res.ok) return "";
  const detail = (res.data as { detail?: unknown } | null)?.detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => {
        const e = (d ?? {}) as Record<string, unknown>;
        const loc = Array.isArray(e.loc) ? e.loc.slice(1).join(".") : "";
        const msg = typeof e.msg === "string" ? e.msg : "";
        return loc ? `${loc}: ${msg}` : msg;
      })
      .filter(Boolean);
    if (msgs.length > 0) return msgs.join("; ");
  }
  if (typeof detail === "string" && detail) return detail;
  return res.error ?? `Could not start the batch (HTTP ${res.status}).`;
}
