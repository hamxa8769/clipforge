// AI providers (v0.10.0) — guided key setup per provider, a Test button
// for each, and the active-provider choice. Used on the Agent page and in
// Settings → LLM. Keys are stored in the OS keyring server-side, never in
// the browser, never logged, and never shown after saving.
//
// Contract:
//   GET  /api/llm/providers            -> [{id, label, configured, active, models, key_url, ...}]
//   POST /api/llm/{provider}/test {api_key, model?} -> {ok, model, message}
//   POST /api/llm/{provider}/save {api_key, model?} -> {ok, configured, last4, masked, message}
//   POST /api/llm/active {provider}    -> {ok, active, selected, message}
// With no key anywhere the agent uses the built-in rule-based engine,
// which always works.
"use client";
import { useCallback, useEffect, useState } from "react";
import {
  activeEngineName, getLlmProviders, saveLlmProvider, setActiveLlmProvider,
  testLlmProvider, type LlmProvider,
} from "@/lib/api";
import { ExternalLink, PrimaryButton, SecondaryButton } from "./ui";

interface ProviderResult {
  ok: boolean;
  summary: string;
  nextStep: string;
}

function ProviderCard({
  provider,
  onChanged,
}: {
  provider: LlmProvider;
  onChanged: () => void;
}) {
  const [key, setKey] = useState("");
  const [model, setModel] = useState(provider.model);
  const [busy, setBusy] = useState<"save" | "test" | "use" | null>(null);
  const [result, setResult] = useState<ProviderResult | null>(null);
  const models = provider.models ?? [];
  const needsKey = provider.needsKey !== false;

  async function save() {
    if (needsKey && !key.trim()) return;
    setBusy("save");
    setResult(null);
    const res = await saveLlmProvider(provider.id, key.trim(), model || undefined);
    setBusy(null);
    if (res.ok) {
      setKey("");
      const message = (res.data as { message?: string } | null)?.message ?? `${provider.label} key saved.`;
      setResult({ ok: true, summary: message, nextStep: "Press Test connection to check it works." });
      onChanged();
    } else {
      setResult({ ok: false, summary: res.error ?? "Could not save the key.", nextStep: "Check the server is running, then try again." });
    }
  }

  async function test() {
    setBusy("test");
    setResult(null);
    const res = await testLlmProvider(provider.id, key.trim(), model || undefined);
    setBusy(null);
    const message = (res.data as { message?: string; model?: string } | null)?.message ?? res.error ?? "";
    if (res.ok) {
      setResult({ ok: true, summary: message || `${provider.label} is working.`, nextStep: "" });
      onChanged();
    } else {
      setResult({
        ok: false,
        summary: message || "The test failed.",
        nextStep: message.includes("Check your internet") ? "" : "Check the key is copied correctly, then press Test connection again.",
      });
    }
  }

  async function useProvider() {
    setBusy("use");
    setResult(null);
    const res = await setActiveLlmProvider(provider.id);
    setBusy(null);
    if (res.ok) {
      const message = (res.data as { message?: string } | null)?.message ?? `${provider.label} selected.`;
      setResult({ ok: true, summary: message, nextStep: "" });
      onChanged();
    } else {
      setResult({ ok: false, summary: res.error ?? "Could not select this provider.", nextStep: "" });
    }
  }

  return (
    <div className="rounded-lg border border-neutral-800 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <p className="font-medium">{provider.label}</p>
        {provider.active && <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-300">In use</span>}
        {provider.configured && !provider.active && (
          <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-xs font-medium text-sky-300">
            Connected{provider.last4 ? ` ····${provider.last4}` : ""}
          </span>
        )}
        {!provider.configured && provider.id !== "rules" && (
          <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs font-medium text-amber-300">Not connected</span>
        )}
      </div>
      {provider.blurb && <p className="mt-1 text-sm text-neutral-400">{provider.blurb}</p>}

      {provider.id === "rules" ? (
        <p className="mt-3 text-sm text-neutral-300">
          The built-in rule engine — no key, no internet. It writes titles and picks moments with templates.
        </p>
      ) : provider.id === "ollama" ? (
        <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm text-neutral-300">
          <li>
            Install Ollama from{" "}
            <ExternalLink href="https://ollama.com" className="text-sky-400 underline">ollama.com</ExternalLink>{" "}
            and pull a model, for example <code className="rounded bg-neutral-800 px-1">ollama pull llama3.1</code>.
          </li>
          <li>Start Ollama — it answers on {provider.baseUrl ?? "http://127.0.0.1:11434"}.</li>
          <li>Press Test connection, then “Use Ollama”.</li>
        </ol>
      ) : (
        <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm text-neutral-300">
          <li>
            Open{" "}
            {provider.keyUrl ? (
              <ExternalLink href={provider.keyUrl} className="text-sky-400 underline">{provider.label} — get an API key</ExternalLink>
            ) : (
              <>your {provider.label} dashboard</>
            )}{" "}
            and create a key.
          </li>
          <li>Paste the key below, press Save key, then Test connection.</li>
        </ol>
      )}

      {needsKey && (
        <label className="mt-3 block text-sm">
          <span className="text-neutral-400">{provider.label} API key</span>
          <input
            type="password"
            autoComplete="off"
            className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={provider.id === "grok" ? "xai-…" : provider.id === "groq" ? "gsk_…" : provider.id === "openrouter" ? "sk-or-…" : provider.id === "anthropic" ? "sk-ant-…" : "sk-…"}
            aria-label={`${provider.label} API key`}
          />
        </label>
      )}

      {models.length > 0 && (
        <label className="mt-3 block text-sm">
          <span className="text-neutral-400">Model</span>
          <select
            className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            aria-label={`${provider.label} model`}
          >
            {models.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
            {!models.includes(model) && model && <option value={model}>{model}</option>}
          </select>
        </label>
      )}

      {needsKey && (
        <p className="mt-2 text-xs text-neutral-500">
          The key is sent to the ClipForge server only, stored encrypted there, and never shown again.
        </p>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        {needsKey && (
          <>
            <PrimaryButton onClick={save} disabled={busy !== null || !key.trim()}>
              {busy === "save" ? "Saving…" : "Save key"}
            </PrimaryButton>
            <SecondaryButton onClick={test} disabled={busy !== null}>
              {busy === "test" ? "Testing…" : "Test connection"}
            </SecondaryButton>
          </>
        )}
        {provider.id === "ollama" && (
          <SecondaryButton onClick={test} disabled={busy !== null}>
            {busy === "test" ? "Testing…" : "Test connection"}
          </SecondaryButton>
        )}
        <SecondaryButton onClick={useProvider} disabled={busy !== null || provider.active || (needsKey && !provider.configured)}>
          {busy === "use" ? "Selecting…" : provider.active ? "In use" : `Use ${provider.label}`}
        </SecondaryButton>
      </div>

      {result && (
        <div className={`mt-3 rounded-lg border p-3 text-sm ${result.ok ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200" : "border-red-500/40 bg-red-500/10 text-red-200"}`} role="status">
          <p className="font-medium">{result.summary}</p>
          {result.nextStep && <p className="mt-1 text-neutral-300">{result.nextStep}</p>}
        </div>
      )}
    </div>
  );
}

export default function LlmProviders() {
  const [providers, setProviders] = useState<LlmProvider[] | null>(null);

  const load = useCallback(async () => {
    try {
      setProviders(await getLlmProviders());
    } catch {
      setProviders([]);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  if (providers === null) {
    return <p className="text-sm text-neutral-500">Checking the AI engine…</p>;
  }

  const engine = activeEngineName(providers);
  return (
    <div className="space-y-3">
      <p className="text-sm text-neutral-400">
        The agent uses <strong className="text-neutral-200">{engine}</strong> right now.
        Pick any provider below — or none at all, and the built-in rule-based engine does the same jobs with templates.
      </p>
      <div className="space-y-3">
        {providers.map((p) => (
          <ProviderCard key={p.id} provider={p} onChanged={load} />
        ))}
      </div>
    </div>
  );
}
