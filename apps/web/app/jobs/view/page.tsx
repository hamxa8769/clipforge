// Static client page for reviewing ANY job id, including live ones.
//
// The static export can only generate /jobs/<id> pages for ids known at
// build time (see ../[id]/page.tsx generateStaticParams), so a live job id
// has no exported file and /jobs/<live-id> 404s from the static file
// server. This route fixes that: /jobs/view/?id=<id> is a single static
// page (jobs/view/index.html) that reads the id from the query string in
// the browser and renders the same JobReviewClient. In-app links
// (dashboard, queue) point here, so every job — demo or live — is
// reviewable in the desktop app. The /jobs/[id] pages are kept as-is for
// direct links to demo jobs.
//
// useSearchParams() needs a <Suspense> boundary for static export
// (Next.js errors at build time without one: the page is prerendered
// without a query string, then hydrated in the browser with ?id=...).
"use client";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import JobReviewClient from "../[id]/JobReviewClient";

function JobViewInner() {
  const params = useSearchParams();
  const id = params.get("id");
  if (!id) {
    return (
      <div className="space-y-2">
        <h1 className="text-2xl font-bold">No job selected</h1>
        <p className="text-sm text-neutral-400">
          Open a job from the dashboard, or add <code>?id=&lt;job-id&gt;</code> to this URL.
        </p>
        <Link href="/" className="text-sm underline hover:text-white">
          Back to dashboard
        </Link>
      </div>
    );
  }
  return <JobReviewClient id={id} />;
}

export default function JobView() {
  return (
    <Suspense fallback={<p className="text-sm text-neutral-400">Loading job…</p>}>
      <JobViewInner />
    </Suspense>
  );
}
