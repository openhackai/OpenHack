import { Button } from "../components/controls/Button";
import { NumberInput } from "../components/controls/NumberInput";
import { Input } from "../components/controls/Input";
import { Textarea } from "../components/controls/Textarea";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useRequestGate } from "../use-request-gate";
import { RefreshCw } from "../components/nucleo-icons";
import { ProviderIcon } from "../components/provider-icons";
import "./settings.css";
import { ProviderCredentials } from "../components/ProviderCredentials";
import type {
  Settings,
  SettingsResponse,
  SettingsView,
} from "../../../src/shared/contracts";
import {
  providerDefinitions,
  providerIds,
  type ProviderStatus,
} from "../../../src/shared/providers";
import { errorMessage, request } from "../api";
import { ErrorBanner, Loading } from "../components/ui";

const SECTIONS = [
  ["settings-provider", "Provider"],
  ["settings-context", "Project context"],
  ["settings-workspace", "Workspace"],
] as const;
type SectionId = (typeof SECTIONS)[number][0];

export function SettingsPage() {
  const [active, setActive] = useState<SectionId>("settings-provider");
  const scrollRef = useRef<HTMLDivElement>(null);
  const lockSpy = useRef(0);
  const action = useRequestGate();
  const revision = useRef(0);
  const [settings, setSettings] = useState<SettingsView>();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void request<SettingsResponse>("/settings", { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) setSettings(result.settings);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      });
    return () => controller.abort();
  }, []);
  function update<K extends keyof Settings>(key: K, value: Settings[K]) {
    revision.current++;
    setSettings((current) =>
      current ? { ...current, [key]: value } : current,
    );
    setSaved(false);
    setDirty(true);
  }
  function changeProvider(value: string) {
    const provider = providerIds.find((id) => id === value);
    if (!provider) return;
    revision.current++;
    setSettings((current) =>
      current
        ? {
            ...current,
            provider,
            model: providerDefinitions[provider].defaultModel,
            reasoning: undefined,
          }
        : current,
    );
    setSaved(false);
    setDirty(true);
    setError(null);
  }
  const selectedProvider = settings && providerDefinitions[settings.provider];
  const providerStatus = settings?.providers.find(
    (provider) => provider.id === settings.provider,
  );
  function mergeProviderMetadata(providers: ProviderStatus[]) {
    setSettings((current) =>
      current
        ? {
            ...current,
            providers,
            apiKeyConfigured:
              ["openai", "anthropic"].includes(current.provider) &&
              !!providers.find((item) => item.id === current.provider)?.ready,
          }
        : current,
    );
  }
  async function refreshAvailability() {
    const signal = action.begin();
    if (!signal) return;
    setRefreshing(true);
    setError(null);
    try {
      const result = await request<{ providers: ProviderStatus[] }>(
        "/providers/refresh",
        { method: "POST", body: "{}", signal },
      );
      if (action.owns(signal)) mergeProviderMetadata(result.providers);
    } catch (failure) {
      if (action.owns(signal)) setError(errorMessage(failure));
    } finally {
      if (action.owns(signal)) setRefreshing(false);
      action.finish(signal);
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!settings || busy || refreshing) return;
    setError(null);
    setSaved(false);
    const { provider, model, workers, steps, reasoning, context, port } =
      settings;
    if (!model.trim() || model.trim().length > 200) {
      setError("Enter a model name between 1 and 200 characters.");
      return;
    }
    if (!Number.isInteger(workers) || workers < 1 || workers > 32) {
      setError("Parallel workers must be a whole number between 1 and 32.");
      return;
    }
    if (!Number.isInteger(steps) || steps < 1 || steps > 30) {
      setError("Tool rounds must be a whole number between 1 and 30.");
      return;
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      setError("Dashboard port must be a whole number between 1 and 65535.");
      return;
    }
    if (context.length > 12000) {
      setError("Project context must be 12,000 characters or fewer.");
      return;
    }
    const signal = action.begin();
    if (!signal) return;
    const submittedRevision = revision.current;
    setBusy(true);
    try {
      const result = await request<SettingsResponse>("/settings", {
        method: "PATCH",
        signal,
        body: JSON.stringify({
          provider,
          model: model.trim(),
          workers,
          steps,
          reasoning: reasoning ?? null,
          context,
          port,
        }),
      });
      if (!action.owns(signal)) return;
      // Edits made while saving remain drafts; only untouched forms adopt the response.
      if (revision.current === submittedRevision) {
        setSettings(result.settings);
        setSaved(true);
        setDirty(false);
      } else {
        mergeProviderMetadata(result.settings.providers);
      }
    } catch (failure) {
      if (action.owns(signal)) setError(errorMessage(failure));
    } finally {
      if (action.owns(signal)) setBusy(false);
      action.finish(signal);
    }
  }

  return (
    <section className="page">
      <div className="pagebar">
        <div className="page-title">
          <h1>Settings</h1>
          <span className="muted">Defaults for new scans</span>
        </div>
      </div>
      <ErrorBanner message={error} />
      {!settings ? (
        !error && <Loading />
      ) : (
        <form noValidate className="settings-layout" onSubmit={submit}>
          <div
            className="settings-scroll"
            ref={scrollRef}
            onScroll={(event) => {
              // Highlight the section nearest the top; a click wins while it scrolls.
              if (Date.now() < lockSpy.current) return;
              const root = event.currentTarget;
              const top = root.getBoundingClientRect().top + 48;
              let current: SectionId = SECTIONS[0][0];
              for (const [id] of SECTIONS) {
                const el = document.getElementById(id);
                if (el && el.getBoundingClientRect().top <= top) current = id;
              }
              if (root.scrollTop + root.clientHeight >= root.scrollHeight - 4)
                current = "settings-workspace";
              setActive(current);
            }}
          >
            <div className="settings-columns">
              <nav className="settings-nav" aria-label="Settings sections">
                {SECTIONS.map(([id, label]) => (
                  <a
                    key={id}
                    href="#/settings"
                    aria-current={active === id ? "true" : undefined}
                    onClick={(event) => {
                      event.preventDefault();
                      setActive(id);
                      lockSpy.current = Date.now() + 800;
                      document.getElementById(id)?.scrollIntoView({
                        behavior: "smooth",
                        block: "start",
                      });
                    }}
                  >
                    {label}
                  </a>
                ))}
              </nav>
              <div className="settings-cards">
                <section className="settings-card" id="settings-provider">
                  <header className="settings-card-header">
                    <div>
                      <h2>Provider</h2>
                      <p>
                        The model provider agents use for new scans. Running and
                        resumed scans keep their saved configuration.
                      </p>
                    </div>
                    <Button
                      className="button"
                      onClick={() => void refreshAvailability()}
                      disabled={refreshing || busy}
                    >
                      <RefreshCw
                        size={12}
                        className={refreshing ? "spin" : undefined}
                      />
                      {refreshing ? "Checking…" : "Check availability"}
                    </Button>
                  </header>
                  <div
                    className="provider-options"
                    role="radiogroup"
                    aria-label="Scan provider"
                  >
                    {providerIds.map((id) => {
                      const ready = settings.providers.find(
                        (item) => item.id === id,
                      )?.ready;
                      const checked = settings.provider === id;
                      return (
                        <label
                          key={id}
                          className={`provider-option${checked ? " checked" : ""}`}
                        >
                          <input
                            type="radio"
                            name="provider"
                            value={id}
                            checked={checked}
                            onChange={() => changeProvider(id)}
                          />
                          <span className="provider-tile">
                            <ProviderIcon
                              provider={id}
                              size={15}
                              aria-hidden="true"
                            />
                          </span>
                          <span className="provider-option-text">
                            <span className="settings-row-title">
                              {providerDefinitions[id].label}
                            </span>
                            <span className="settings-row-desc">
                              {providerDefinitions[id].description}
                            </span>
                          </span>
                          <span
                            className={`dot-status ${ready ? "tone-success" : "tone-muted"}`}
                          >
                            <i aria-hidden="true" />
                            {ready ? "Available" : "Unavailable"}
                          </span>
                          <span className="radio-mark" aria-hidden="true" />
                        </label>
                      );
                    })}
                  </div>
                  <ProviderCredentials
                    key={settings.provider}
                    provider={settings.provider}
                    status={providerStatus}
                    disabled={busy || refreshing}
                    onMetadata={(result) =>
                      mergeProviderMetadata(result.providers)
                    }
                  />
                  <div className="settings-row">
                    <div className="settings-row-text">
                      <label
                        className="settings-row-title"
                        htmlFor="settings-model"
                      >
                        Model
                      </label>
                      <span className="settings-row-desc">
                        {settings.provider === "codex"
                          ? 'Use "default" for your Codex CLI model, or enter a model ID.'
                          : settings.provider === "claude-code"
                            ? "A Claude Code model alias or model ID."
                            : settings.provider === "anthropic"
                              ? "An Anthropic model ID with tool use support."
                              : "An OpenAI model with Responses API, tools and structured output."}
                      </span>
                    </div>
                    <div className="settings-row-control">
                      <Input
                        id="settings-model"
                        className="mono-field"
                        value={settings.model}
                        onChange={(event) =>
                          update("model", event.target.value)
                        }
                        required
                        maxLength={200}
                        autoComplete="off"
                        spellCheck={false}
                      />
                    </div>
                  </div>
                </section>

                <section className="settings-card" id="settings-context">
                  <header className="settings-card-header">
                    <div>
                      <h2>Project context</h2>
                      <p>
                        Optional notes included with every new scan: trust
                        boundaries, deployment assumptions, areas to focus on.
                      </p>
                    </div>
                  </header>
                  <div className="settings-block">
                    <Textarea
                      rows={6}
                      aria-label="Project context"
                      value={settings.context}
                      onChange={(event) =>
                        update("context", event.target.value)
                      }
                      maxLength={12000}
                      placeholder="e.g. Admin routes sit behind the VPN; focus on the public API and webhook handlers."
                    />
                    <span className="settings-counter">
                      {settings.context.length.toLocaleString()} / 12,000
                    </span>
                  </div>
                </section>

                <section className="settings-card" id="settings-workspace">
                  <header className="settings-card-header">
                    <div>
                      <h2>Local workspace</h2>
                      <p>Where this dashboard runs and keeps its data.</p>
                    </div>
                  </header>
                  <div className="settings-row">
                    <div className="settings-row-text">
                      <span className="settings-row-title">Dashboard port</span>
                      <span className="settings-row-desc">
                        Applies the next time you start the dashboard.
                      </span>
                    </div>
                    <div className="settings-row-control">
                      <NumberInput
                        id="settings-port"
                        aria-label="Dashboard port"
                        min={1}
                        max={65535}
                        value={settings.port}
                        onValueChange={(value) => update("port", value)}
                        required
                      />
                    </div>
                  </div>
                  <div className="settings-row">
                    <div className="settings-row-text">
                      <span className="settings-row-title">
                        Storage directory
                      </span>
                      <span className="settings-row-desc">
                        Scans, findings and saved keys live here.
                      </span>
                    </div>
                    <code className="settings-path">{settings.home}</code>
                  </div>
                </section>
              </div>
            </div>
          </div>
          <footer className="settings-footer">
            <span className={saved ? "success-text" : "muted"} role="status">
              {saved
                ? "Settings saved"
                : dirty
                  ? "Unsaved changes"
                  : "Changes apply to new scans"}
            </span>
            <Button
              type="submit"
              className="button primary"
              disabled={busy || refreshing || !dirty}
            >
              {busy ? "Saving…" : "Save changes"}
            </Button>
          </footer>
        </form>
      )}
    </section>
  );
}
