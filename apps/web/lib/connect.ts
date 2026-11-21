// Guided Connect wizard content (v0.4.0) — one developer-app recipe per
// platform. Links are the official consoles; the steps are the clicks in
// order. Keep the copy plain: a user who has never made a developer app
// should be able to follow it without searching anything up.
//
// The redirect URI is NOT in here — it comes from the API at runtime
// (lib/api.ts getRedirectUri), because it depends on the port the server
// actually runs on.
import { platformLabel } from "@/components/ui";

export interface GuideStep { title: string; detail: string; link?: { label: string; url: string } }
export interface PlatformGuide {
  platform: string;
  consoleName: string;
  consoleUrl: string;
  whatYouNeed: string;
  steps: GuideStep[];
  afterConnectNote: string;
}

export const PLATFORM_GUIDES: Record<string, PlatformGuide> = {
  youtube: {
    platform: "youtube",
    consoleName: "Google Cloud Console",
    consoleUrl: "https://console.cloud.google.com/apis/credentials",
    whatYouNeed: "A Google account. No payment needed — the YouTube Data API is free for this use.",
    steps: [
      { title: "Open Google Cloud Console", detail: "Sign in with your Google account and create a project (or pick an existing one) at the top of the page.", link: { label: "Open Google Cloud Console", url: "https://console.cloud.google.com/apis/credentials" } },
      { title: "Turn on the YouTube Data API", detail: "Go to APIs & Services → Library, search for “YouTube Data API v3”, open it and click Enable.", link: { label: "Open the API Library — YouTube Data API v3", url: "https://console.cloud.google.com/apis/library/youtube.googleapis.com" } },
      { title: "Create OAuth credentials", detail: "Go to APIs & Services → Credentials → Create Credentials → OAuth client ID. If asked, configure the consent screen first: choose “External”, fill in the app name, and add yourself as a test user.", link: { label: "Open Credentials", url: "https://console.cloud.google.com/apis/credentials" } },
      { title: "Choose “Web application”", detail: "For “Application type” pick Web application. Give it a name like “ClipForge”." },
      { title: "Add the redirect URI", detail: "Under “Authorized redirect URIs” click Add URI and paste the redirect URI shown below — exactly as shown, no extra spaces. Then click Create." },
      { title: "Copy the Client ID and Client Secret", detail: "Google shows them on the next screen. Copy both into the next step of this wizard. You can find them again later under Credentials." },
    ],
    afterConnectNote: "Google must verify the app before anyone other than you can connect. Until verification passes, only test users you add in Google Cloud Console can connect — that is enough for your own channel.",
  },
  instagram: {
    platform: "instagram",
    consoleName: "Meta for Developers",
    consoleUrl: "https://developers.facebook.com/apps",
    whatYouNeed: "A Facebook account and an Instagram account switched to a Professional (Creator or Business) account. The same Meta app serves Instagram and Facebook.",
    steps: [
      { title: "Open Meta for Developers", detail: "Sign in with Facebook and click Create App.", link: { label: "Open Meta for Developers — My Apps", url: "https://developers.facebook.com/apps" } },
      { title: "Create the app", detail: "Choose “Other” as the use case, then “Business” as the app type. Give it a name like “ClipForge”." },
      { title: "Add the Instagram product", detail: "On the app dashboard click Add Product (or Set up) next to “Instagram Graph API”. Connect the Instagram Professional account when asked." },
      { title: "Add Facebook Login", detail: "Also add the “Facebook Login” product — this is where the redirect URI is registered." },
      { title: "Add the redirect URI", detail: "Go to Facebook Login → Settings → Valid OAuth Redirect URIs, paste the redirect URI shown below exactly, and save." },
      { title: "Copy the App ID and App Secret", detail: "Go to Settings → Basic. The App ID is the Client ID; click Show next to App Secret for the Client Secret. Copy both into the next step." },
    ],
    afterConnectNote: "Meta reviews apps before public posting works. Until review passes, ClipForge saves clips as drafts and manual-post packs — posting itself stays off.",
  },
  facebook: {
    platform: "facebook",
    consoleName: "Meta for Developers",
    consoleUrl: "https://developers.facebook.com/apps",
    whatYouNeed: "A Facebook account and a Facebook Page you manage. The same Meta app serves Instagram and Facebook.",
    steps: [
      { title: "Open Meta for Developers", detail: "Sign in with Facebook and click Create App.", link: { label: "Open Meta for Developers — My Apps", url: "https://developers.facebook.com/apps" } },
      { title: "Create the app", detail: "Choose “Other” as the use case, then “Business” as the app type. Give it a name like “ClipForge”." },
      { title: "Add Facebook Login", detail: "On the app dashboard click Add Product (or Set up) next to “Facebook Login”." },
      { title: "Add the redirect URI", detail: "Go to Facebook Login → Settings → Valid OAuth Redirect URIs, paste the redirect URI shown below exactly, and save." },
      { title: "Copy the App ID and App Secret", detail: "Go to Settings → Basic. The App ID is the Client ID; click Show next to App Secret for the Client Secret. Copy both into the next step." },
      { title: "Connect your Page", detail: "When you press Connect in the last step, Facebook asks which Page to use — pick the Page the clips should post to." },
    ],
    afterConnectNote: "Meta reviews apps before public posting works. Until review passes, ClipForge saves clips as drafts and manual-post packs — posting itself stays off.",
  },
  tiktok: {
    platform: "tiktok",
    consoleName: "TikTok for Developers",
    consoleUrl: "https://developers.tiktok.com/",
    whatYouNeed: "A TikTok account. Posting needs TikTok's Content Posting API audit — that takes time, so start the app now and use drafts meanwhile.",
    steps: [
      { title: "Open TikTok for Developers", detail: "Sign in with TikTok and go to Manage apps → Create an app.", link: { label: "Open TikTok for Developers", url: "https://developers.tiktok.com/" } },
      { title: "Create the app", detail: "Fill in the app name (e.g. “ClipForge”) and description. Choose the Login Kit product." },
      { title: "Add the redirect URI", detail: "In the app's Login Kit settings, add the redirect URI shown below exactly as shown, and save." },
      { title: "Copy the Client Key and Client Secret", detail: "On the app's Basic Information page, the Client Key is the Client ID. Copy the Client Secret from the same page into the next step." },
      { title: "Request the Content Posting API", detail: "In Products, request access to the Content Posting API. TikTok audits the app before public posting is allowed — this can take days." },
    ],
    afterConnectNote: "Until TikTok's audit passes, unaudited apps can only create private / inbox drafts. ClipForge uses that draft mode automatically — nothing posts publicly by accident.",
  },
};

export function guideFor(platform: string): PlatformGuide {
  return PLATFORM_GUIDES[platform.toLowerCase()] ?? {
    platform,
    consoleName: "the platform's developer console",
    consoleUrl: "",
    whatYouNeed: "",
    steps: [],
    afterConnectNote: "",
  };
}

export function guideLabel(platform: string): string {
  return platformLabel(platform);
}
