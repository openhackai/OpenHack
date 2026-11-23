import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button, Input } from "./controls";
import { Check, ChevronRight, Folder, Play, RefreshCw } from "./nucleo-icons";
import { ProviderIcon } from "./provider-icons";
import { ErrorBanner, Loading } from "./ui";
import {
  providerDefinitions,
  providerIds,
  type ProviderId,
  type ProviderStatus,
} from "../../../src/shared/providers";
import type {
  DirectoryListing,
  DirectoryPickResponse,
  ScanResponse,
  SettingsResponse,
  SettingsView,
} from "../../../src/shared/contracts";
import { ApiError, errorMessage, request } from "../api";
import { useRequestGate } from "../use-request-gate";
import "./onboarding.css";

const steps = ["Connect a provider", "Choose a project", "Start your scan"];

/** First-scan setup lives in the dashboard, with no modal or browser-local completion flag. */
export function Onboarding({
  onStarted,
}: {
  onStarted: (scan: ScanResponse["scan"]) => void;
}) {
  const action = useRequestGate();
  const heading = useRef<HTMLHeadingElement>(null);
  const [step, setStep] = useState(0);
  const [settings, setSettings] = useState<SettingsView>();
  const [provider, setProvider] = useState<ProviderId>("openai");
  const [key, setKey] = useState("");
  const [path, setPath] = useState("");
  const [listing, setListing] = useState<DirectoryListing>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const status = settings?.providers.find((item) => item.id === provider);
  const isApi = provider === "openai" || provider === "anthropic";

  useEffect(() => {
    const controller = new AbortController();
    void request<SettingsResponse>("/settings", { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted) return;
        setSettings(result.settings);
        setProvider(result.settings.provider);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      });
    return () => controller.abort();
  }, []);

  function go(next: number) {
    setStep(next);
    setError(null);
    requestAnimationFrame(() => heading.current?.focus());
  }
  async function perform(work: (signal: AbortSignal) => Promise<void>) {
    const signal = action.begin();
    if (!signal) return;
    setBusy(true);
    setError(null);
    try {
      await work(signal);
    } catch (failure) {
      if (action.owns(signal)) setError(errorMessage(failure));
    } finally {
      if (action.owns(signal)) setBusy(false);
      action.finish(signal);
    }
  }
  async function browseFallback(nextPath?: string) {
    await perform(async (signal) => {
      const result = await request<DirectoryListing>("/directories/list", {
        method: "POST",
        signal,
        body: JSON.stringify({ ...(nextPath ? { path: nextPath } : {}) }),
      });
      if (action.owns(signal)) {
        setListing(result);
        setPath(result.path);
      }
    });
  }
  async function browse() {
    await perform(async (signal) => {
      try {
        const result = await request<DirectoryPickResponse>("/directories/pick", {
          method: "POST",
          signal,
          body: "{}",
          timeoutMs: 11 * 60_000,
        });
        if (action.owns(signal) && result.path) {
          setPath(result.path);
          setListing(undefined);
        }
      } catch (failure) {
        if (!action.owns(signal)) return;
        if (!(failure instanceof ApiError) || ![404, 501].includes(failure.status))
          throw failure;
        const result = await request<DirectoryListing>("/directories/list", {
          method: "POST",
          signal,
          body: JSON.stringify({ ...(path.trim() ? { path: path.trim() } : {}) }),
        });
        if (action.owns(signal)) {
          setListing(result);
          setPath(result.path);
        }
      }
    });
  }
  async function refreshProviders() {
    await perform(async (signal) => {
      const result = await request<{ providers: ProviderStatus[] }>(
        "/providers/refresh",
        { method: "POST", signal, body: "{}" },
      );
      if (action.owns(signal))
        setSettings(
          (current) => current && { ...current, providers: result.providers },
        );
    });
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!settings) return;
    await perform(async (signal) => {
      if (step === 0) {
        let current = settings;
        if (isApi && key.trim()) {
          if (key.trim().length > 8192 || /\s/.test(key.trim()))
            throw new Error(
              "Enter an API key without whitespace, up to 8,192 characters.",
            );
          const saved = await request<SettingsResponse>(
            `/providers/${provider}/credentials`,
            {
              method: "POST",
              signal,
              body: JSON.stringify({ key: key.trim() }),
            },
          );
          if (!action.owns(signal)) return;
          current = saved.settings;
          setSettings(current);
          setKey("");
        }
        if (!current.providers.find((item) => item.id === provider)?.ready)
          throw new Error(
            "Connect this provider before continuing. For a CLI login, sign in and refresh its status.",
          );
        const saved = await request<SettingsResponse>("/settings", {
          method: "PATCH",
          signal,
          body: JSON.stringify({ provider }),
        });
        if (action.owns(signal)) {
          setSettings(saved.settings);
          go(1);
        }
      } else if (step === 1) {
        if (!path.trim()) throw new Error("Enter or choose a project folder.");
        const result = await request<DirectoryListing>("/directories/list", {
          method: "POST",
          signal,
          body: JSON.stringify({ path: path.trim() }),
        });
        if (action.owns(signal)) {
          setPath(result.path);
          go(2);
        }
      } else {
        const result = await request<ScanResponse>("/scans", {
          method: "POST",
          signal,
          body: JSON.stringify({
            path,
            provider,
            model: settings.model,
            workers: settings.workers,
            steps: settings.steps,
            reasoning: settings.reasoning,
            context: settings.context,
          }),
        });
        if (action.owns(signal)) onStarted(result.scan);
      }
    });
  }

  return (
    <section className="setup" aria-label="Set up your first scan">
      <div className="setup-intro">
        <span className="setup-eyebrow">GET STARTED</span>
        <h2>Your first scan starts here.</h2>
        <p>
          Connect your AI provider, pick a codebase, and let OpenHack review it.
        </p>
      </div>
      <ol className="setup-steps" aria-label="Setup progress">
        {steps.map((label, index) => (
          <li
            key={label}
            data-state={
              index === step
                ? "current"
                : index < step
                  ? "complete"
                  : "upcoming"
            }
            aria-current={index === step ? "step" : undefined}
          >
            <Button disabled={index > step || busy} onClick={() => go(index)}>
              <span className="setup-step-number">
                {index < step ? <Check size={14} /> : index + 1}
              </span>
              <span>{label}</span>
            </Button>
          </li>
        ))}
      </ol>
      <form
        className="setup-card"
        onSubmit={submit}
        noValidate
        aria-busy={busy}
      >
        <div className="setup-card-heading">
          <span className="setup-eyebrow">STEP {step + 1} OF 3</span>
          <h3 ref={heading} tabIndex={-1}>
            {steps[step]}
          </h3>
          <p>
            {step === 0
              ? "Use an API key or a CLI login you already have."
              : step === 1
                ? "Choose the local folder containing the source code you want to review."
                : "Review your setup. The scan begins only when you’re ready."}
          </p>
        </div>
        {!settings && !error ? (
          <Loading />
        ) : (
          settings && (
            <>
              {step === 0 && (
                <>
                  <div
                    className="setup-providers"
                    role="group"
                    aria-label="AI provider"
                  >
                    {providerIds.map((id) => (
                      <Button
                        key={id}
                        className="setup-provider"
                        aria-pressed={provider === id}
                        disabled={busy}
                        onClick={() => {
                          setProvider(id);
                          setKey("");
                          setError(null);
                        }}
                      >
                        <ProviderIcon
                          provider={id}
                          size={22}
                          aria-hidden="true"
                        />
                        <span>
                          <strong>{providerDefinitions[id].label}</strong>
                          <small>
                            {id === "openai" || id === "anthropic"
                              ? "API key"
                              : "Existing CLI login"}
                          </small>
                        </span>
                        {provider === id && <Check size={15} />}
                      </Button>
                    ))}
                  </div>
                  <p className="setup-provider-description">
                    {providerDefinitions[provider].description}
                  </p>
                  {isApi ? (
                    <label className="field" htmlFor="setup-api-key">
                      <span>
                        {status?.ready
                          ? "API key connected · replace it below if needed"
                          : "API key"}
                      </span>
                      <Input
                        id="setup-api-key"
                        type="password"
                        value={key}
                        disabled={busy}
                        onChange={(event) => setKey(event.target.value)}
                        placeholder={
                          status?.ready
                            ? "Leave blank to use your connected key"
                            : "Enter your API key"
                        }
                        autoComplete="new-password"
                        spellCheck={false}
                        maxLength={8192}
                      />
                      <small>
                        Your key is saved locally and is never shown in the
                        dashboard.
                      </small>
                    </label>
                  ) : (
                    <div className="setup-login">
                      <p>
                        Install the official{" "}
                        {provider === "codex" ? "Codex" : "Claude Code"} CLI,
                        then sign in from your terminal:
                      </p>
                      <code>
                        {provider === "codex"
                          ? "codex login"
                          : "claude auth login"}
                      </code>
                      <Button
                        className="button"
                        onClick={() => void refreshProviders()}
                        disabled={busy}
                      >
                        <RefreshCw size={13} />
                        Refresh login status
                      </Button>
                    </div>
                  )}
                  <p
                    className={`setup-connection ${status?.ready ? "is-ready" : ""}`}
                    role="status"
                  >
                    {status?.ready ? <Check size={14} /> : null}
                    {status?.ready ? "Connected" : "Not connected"}
                    {!isApi && <span> · {status?.detail}</span>}
                  </p>
                </>
              )}
              {step === 1 && (
                <>
                  <div className="field">
                    <label htmlFor="setup-project">Project folder</label>
                    <div className="directory-input">
                      <Input
                        id="setup-project"
                        value={path}
                        disabled={busy}
                        maxLength={4096}
                        onChange={(event) => {
                          setPath(event.target.value);
                          setListing(undefined);
                        }}
                        placeholder="/path/to/your/project"
                        autoComplete="off"
                        spellCheck={false}
                      />
                      <Button
                        className="button"
                        disabled={busy}
                        onClick={() => void browse()}
                      >
                        <Folder size={14} />
                        Browse
                      </Button>
                    </div>
                  </div>
                  {listing && (
                    <div className="setup-folders" aria-label="Project folders">
                      <div className="setup-folder-header">
                        <span title={listing.path}>{listing.path}</span>
                        {listing.parent && (
                          <Button
                            className="text-button"
                            disabled={busy}
                            onClick={() => void browseFallback(listing.parent!)}
                          >
                            Up one level
                          </Button>
                        )}
                      </div>
                      <ul>
                        {listing.directories.map((directory) => (
                          <li key={directory.path}>
                            <Button
                              disabled={busy}
                              onClick={() => void browseFallback(directory.path)}
                            >
                              <Folder size={14} />
                              <span>{directory.name}</span>
                              <ChevronRight size={12} />
                            </Button>
                          </li>
                        ))}
                      </ul>
                      {!listing.directories.length && (
                        <p>No subfolders. You can use this folder.</p>
                      )}
                      {listing.truncated && (
                        <p>
                          Some folders are not shown. Enter a full path to
                          navigate directly.
                        </p>
                      )}
                    </div>
                  )}
                  <p className="setup-note">
                    OpenHack reads your source without running it. Selected
                    source excerpts are sent to your chosen AI provider.
                  </p>
                </>
              )}
              {step === 2 && (
                <>
                  <dl className="setup-summary">
                    <div>
                      <dt>Project</dt>
                      <dd>{path}</dd>
                    </div>
                    <div>
                      <dt>Provider</dt>
                      <dd>{providerDefinitions[provider].label}</dd>
                    </div>
                    <div>
                      <dt>Model</dt>
                      <dd>{settings.model}</dd>
                    </div>
                    <div>
                      <dt>Scan settings</dt>
                      <dd>
                        {settings.workers} workers · {settings.steps} tool
                        rounds
                      </dd>
                    </div>
                  </dl>
                  <p className="setup-note">
                    Follow progress in Scans, then review and triage the
                    findings. API charges or your subscription’s usage limits
                    apply.
                  </p>
                </>
              )}
            </>
          )
        )}
        <ErrorBanner message={error} />
        {!settings && error && (
          <Button
            className="button"
            disabled={busy}
            onClick={() =>
              void perform(async (signal) => {
                const result = await request<SettingsResponse>("/settings", {
                  signal,
                });
                if (action.owns(signal)) {
                  setSettings(result.settings);
                  setProvider(result.settings.provider);
                }
              })
            }
          >
            Retry setup
          </Button>
        )}
        <div className="setup-footer">
          <span>
            {step === 0
              ? "You can change providers later in Settings."
              : "Your source files won’t be modified."}
          </span>
          <div>
            {step > 0 && (
              <Button
                className="button"
                disabled={busy}
                onClick={() => go(step - 1)}
              >
                Back
              </Button>
            )}
            <Button
              className="button primary"
              type="submit"
              disabled={
                busy ||
                !settings ||
                (step === 0 && !status?.ready && !(isApi && key.trim())) ||
                (step === 1 && !path.trim())
              }
            >
              {step === 2 && <Play size={12} />}
              {busy ? "Working…" : step === 2 ? "Start first scan" : "Continue"}
              {step < 2 && <ChevronRight size={12} />}
            </Button>
          </div>
        </div>
      </form>
    </section>
  );
}
