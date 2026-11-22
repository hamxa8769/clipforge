// Approve-queue summary (Worker A audit fix, v0.9.0).
//
// Approving a clip candidate runs the automation rules server-side
// (`automation.on_candidate_approved`): one queue entry per platform —
// `queued` (a post was created), `exists` (a post already existed),
// `skipped_no_account` (no channel connected), `skipped_cap` (daily cap
// reached). The review page used to ignore this result and always say
// "Approved — sent to the approval queue", even when zero posts were
// created. This pure helper turns the raw `automation.queue` array into
// the honest per-platform outcome the UI shows instead.

export interface ApproveQueueSummary {
  /** Platforms a post was actually created for. */
  queued: string[];
  /** Platforms that already had a post for this clip. */
  alreadyExisted: string[];
  /** Platforms skipped because no channel is connected. */
  skippedNoAccount: string[];
  /** Platforms skipped because the daily cap was reached. */
  skippedCap: string[];
  /** Total queue entries the server returned. */
  total: number;
}

export function summarizeApproveQueue(queue: unknown): ApproveQueueSummary {
  const out: ApproveQueueSummary = {
    queued: [],
    alreadyExisted: [],
    skippedNoAccount: [],
    skippedCap: [],
    total: 0,
  };
  if (!Array.isArray(queue)) return out;
  for (const entry of queue) {
    const e = (entry ?? {}) as Record<string, unknown>;
    const platform = typeof e.platform === "string" ? e.platform : "?";
    const status = typeof e.status === "string" ? e.status : "";
    out.total += 1;
    if (status === "queued") out.queued.push(platform);
    else if (status === "exists") out.alreadyExisted.push(platform);
    else if (status === "skipped_no_account") out.skippedNoAccount.push(platform);
    else if (status === "skipped_cap") out.skippedCap.push(platform);
  }
  return out;
}
