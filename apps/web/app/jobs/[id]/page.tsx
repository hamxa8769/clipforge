// Thin server wrapper: static export needs generateStaticParams() for
// this dynamic route, and that export is only allowed in a server
// component — so the data fetching lives in ./JobReviewClient.tsx.
// No fetching happens here; the page is fully static at build time.
//
// Static-export limit (see apps/web/README.md): only ids known at build
// time get an exported HTML file, so generateStaticParams() lists the
// demo job ids. A live job id not known at build time has no exported
// file: opening /jobs/<live-id> directly returns 404 from the static
// file server. Live jobs are reviewed via /jobs/view/?id=<id> instead
// (see ../view/page.tsx); in-app links point there.
import { demoJobs } from "@/lib/mock";
import JobReviewClient from "./JobReviewClient";

export function generateStaticParams() {
  return demoJobs.map((j) => ({ id: j.id }));
}

export default async function JobReview({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <JobReviewClient id={id} />;
}
