// Guided Gemini setup (v0.5.0) — used on the Agent page and in Settings →
// LLM. One masked key field, Save + Test, plain-language result.
//
// Contract (worker 2 owns the backend):
//   POST /api/llm/gemini/test {api_key} -> {ok, model, message}
//   POST /api/llm/gemini/save {api_key}
//   GET  /api/llm/providers           -> [{id, label, configured}] (masked)
//
// Until those endpoints land, Save/Test fall back to the existing
// generic LLM key (PUT /settings + POST /settings/test-connection/llm)
// so the card still works in this build — the result line says which
// path answered. When Gemini is not connected the card says plainly
// that the agent uses the built-in rule-based engine. The key is never
// stored in the browser, never logged, and never shown after saving.
"use client";
import { useCallback, useEffect, useState } from "react";
import {
  activeEngineName, explainConnectionError, getLlmProviders, getSettings,
  saveGeminiKey, saveSettings, testConnection, testGeminiKey,
  type LlmProvider,
} from "@/lib/api";
import { ExternalLink, PrimaryButton, SecondaryButton } from "./ui";

export const GEMINI_KEY_URL = "https://aistudio.google.com/app/apikey";

export default function GeminiSetup() {
  const [providers, setProviders] = useState<LlmProvider[] | null>(null);
  const [genericConfigured, setGenericConfigured] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [result, setResult] = useState<{ ok: boolean; summary: string; nextStep: string } | null>(null);

  const load = useCallback(async () => {
    const [fetchedProviders, settings] = await Promise.all([getLlmProviders(), getSettings()]);
    setProviders(fetchedProviders);
    if (settings) setGenericConfigured(settings.llm.api_key.configured);
    setLoaded(true);
  }, []);

  useEffect(() => { load().catch(() => setLoaded(true)); }, [load]);

  const gemini = providers?.find((p) => p.id === "gemini");
  const geminiConfigured = gemini ? gemini.configured : genericConfigured;
  const engine = activeEngineName(providers);

  async function save() {
    if (!key.trim()) return;
    setBusy("save");
    setResult(null);
    // Worker-2 endpoint first; 404 means this build predates it — fall
    // back to the generic LLM key the Settings page has always used.
    const res = await saveGeminiKey(key.trim());
    let ok = res.ok;
    let usedFallback = false;
    if (!res.ok && res.status === 404) {
      usedFallback = true;
      ok = (await saveSettings({ llm_api_key: key.trim() })).ok;
    }
    setBusy(null);
    if (ok) {
      setKey("");
      setResult({
        ok: true,
        summary: usedFallback
          ? "Key saved (generic AI key — the Gemini endpoint is not in this build yet)."
          : "Gemini key saved.",
        nextStep: "Press Test connection to check it works.",
      });
      await load();
    } else {
      setResult({
        ok: false,
        summary: res.error ?? "Could not save the key.",
        nextStep: "Check the server is running, then try again.",
      });
    }
  }

  async function test() {
    setBusy("test");
    setResult(null);
    // The test needs a key: use the typed one, or the saved one when the
    // field is empty (the endpoint reads the stored key in that case —
    // the generic fallback does exactly that).
    const res = key.trim()
      ? await testGeminiKey(key.trim())
      : await testGeminiKey("");
    let ok = res.ok;
    let message = (res.data as { message?: string; model?: string } | null)?.message ?? res.error ?? "";
    let usedFallback = false;
    if (!res.ok && res.status === 404) {
      usedFallback = true;
      const fb = await testConnection("llm");
      ok = fb.ok;
      message = (fb.data as { message?: string } | null)?.message ?? fb.error ?? "";
    }
    setBusy(null);
    if (ok) {
      const model = (res.data as { model?: string } | null)?.model;
      setResult({
        ok: true,
        summary: usedFallback
          ? "Connected — the saved key works (generic AI check)."
          : `Connected to Gemini${model ? ` — model ${model}` : ""}.`,
        nextStep: "",
      });
      await load();
    } else {
      // The Gemini endpoint already answers in plain language (e.g.
      // "Could not reach the Gemini API: ... Check your internet
      // connection and test again."), so show its message as-is —
      // explainConnectionError's generic fallback ("try the Connect
      // wizard again") would be wrong here; it is only for cryptic
      // OAuth/provider codes or an empty message.
      const explained = message
        ? {
            summary: message,
            nextStep: message.includes("Check your internet")
              ? ""
              : "Check the key is copied correctly, then press Test connection again.",
          }
        : explainConnectionError(message);
      setResult({ ok: false, summary: explained.summary, nextStep: explained.nextStep });
    }
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-neutral-400">
        Gemini is the AI engine the agent uses to pick the best moments and write titles.
        Without a key, ClipForge still works — the built-in rule-based engine does the same jobs with templates.
      </p>

      {!loaded ? (
        <p className="text-sm text-neutral-500">Checking the AI engine…</p>
      ) : geminiConfigured ? (
        <p className="rounded-lg border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm text-emerald-200">
          Gemini is connected — the agent uses Gemini{gemini?.model ? ` (${gemini.model})` : ""}.
          Paste a new key below to replace it.
        </p>
      ) : (
        <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm text-amber-200">
          Gemini is not connected. The agent uses the {engine} right now.
        </p>
      )}

      <ol className="list-decimal space-y-1 pl-5 text-sm text-neutral-300">
        <li>
          Open{" "}
          <ExternalLink href={GEMINI_KEY_URL} className="text-sky-400 underline">
            Google AI Studio — Get API key
          </ExternalLink>{" "}
          and sign in with your Google account.
        </li>
        <li>Press “Create API key”, copy it, and paste it below.</li>
        <li>Press Save key, then Test connection.</li>
      </ol>

      <label className="block text-sm">
        <span className="text-neutral-400">Gemini API key</span>
        <input
          type="password"
          autoComplete="off"
          className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="AIza…"
        />
      </label>
      <p className="text-xs text-neutral-500">
        The key is sent to the ClipForge server only, stored encrypted there, and never shown again — not even the last 4 characters leave the server except as “••••”.
      </p>

      <div className="flex flex-wrap gap-2">
        <PrimaryButton onClick={save} disabled={busy !== null || !key.trim()}>
          {busy === "save" ? "Saving…" : "Save key"}
        </PrimaryButton>
        <SecondaryButton onClick={test} disabled={busy !== null}>
          {busy === "test" ? "Testing…" : "Test connection"}
        </SecondaryButton>
      </div>

      {result && (
        <div className={`rounded-lg border p-3 text-sm ${result.ok ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200" : "border-red-500/40 bg-red-500/10 text-red-200"}`} role="status">
          <p className="font-medium">{result.summary}</p>
          {result.nextStep && <p className="mt-1 text-neutral-300">{result.nextStep}</p>}
        </div>
      )}
    </div>
  );
}
