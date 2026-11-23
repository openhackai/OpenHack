import { useRef, useState, type FormEvent } from "react";
import type {
  SettingsResponse,
  SettingsView,
} from "../../../src/shared/contracts";
import {
  providerDefinitions,
  type ProviderId,
  type ProviderStatus,
} from "../../../src/shared/providers";
import { errorMessage, request } from "../api";
import {
  Button,
  Dialog,
  DialogDescription,
  DialogTitle,
  Input,
} from "./controls";
import { Save, X } from "./nucleo-icons";
import { ErrorBanner } from "./ui";
import { useRequestGate } from "../use-request-gate";

interface ProviderCredentialsProps {
  provider: ProviderId;
  status?: ProviderStatus;
  disabled?: boolean;
  onMetadata: (settings: SettingsView) => void;
}

/** Never receives an existing secret. Mount with the provider as its key. */
export function ProviderCredentials({
  provider,
  status,
  disabled,
  onMetadata,
}: ProviderCredentialsProps) {
  const action = useRequestGate();
  const input = useRef<HTMLInputElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const [mode, setMode] = useState<"key" | "remove" | null>(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const isApi = provider === "openai" || provider === "anthropic";
  const savedKey =
    status?.savedKeyConfigured ?? status?.credentialSource === "saved";
  const environmentKey = status?.credentialSource === "environment";
  const label = providerDefinitions[provider].label;

  function close() {
    if (busy) return;
    setKey("");
    setError(null);
    setMode(null);
  }
  function open(next: "key" | "remove") {
    setKey("");
    setError(null);
    setNotice(null);
    setMode(next);
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    // Dialogs are portalled but React form events still bubble to Settings.
    event.stopPropagation();
    if (!isApi || !mode || disabled) return;
    const value = key.trim();
    if (mode === "key" && (!value || value.length > 8192 || /\s/.test(value))) {
      setError("Enter an API key without whitespace, up to 8,192 characters.");
      input.current?.focus();
      return;
    }
    const signal = action.begin();
    if (!signal) return;
    setBusy(true);
    setError(null);
    try {
      const result = await request<SettingsResponse>(
        `/providers/${provider}/credentials`,
        {
          method: mode === "remove" ? "DELETE" : "POST",
          signal,
          body: JSON.stringify(mode === "remove" ? {} : { key: value }),
        },
      );
      if (!action.owns(signal)) return;
      onMetadata(result.settings);
      setKey("");
      setMode(null);
      setNotice(
        mode === "remove" ? "Saved API key removed." : "API key saved.",
      );
    } catch (failure) {
      if (action.owns(signal)) setError(errorMessage(failure));
    } finally {
      if (action.owns(signal)) setBusy(false);
      action.finish(signal);
    }
  }

  return (
    <div className="settings-row">
      <div className="settings-row-text">
        <span className="settings-row-title">Credentials</span>
        <span className="settings-row-desc">
          {status?.detail ?? "Refresh availability to check this provider."}
          {environmentKey && savedKey
            ? " A saved key is also configured; the environment key takes precedence."
            : ""}
        </span>
        {notice && (
          <span className="success-text" role="status">
            {notice}
          </span>
        )}
      </div>
      <div className="settings-row-control credentials-control">
        {isApi ? (
          <>
            {savedKey && (
              <Button
                className="button"
                disabled={disabled || busy}
                onClick={() => open("remove")}
              >
                Remove
              </Button>
            )}
            <Button
              className="button"
              disabled={disabled || busy}
              onClick={() => open("key")}
            >
              {savedKey ? "Replace key" : "Add API key"}
            </Button>
          </>
        ) : (
          <span
            className={`dot-status ${status?.ready ? "tone-success" : "tone-muted"}`}
          >
            <i aria-hidden="true" />
            {status?.ready ? "Signed in" : "Not detected"}
          </span>
        )}
      </div>
      {mode && (
        <Dialog
          open
          onOpenChange={(next) => {
            if (!next) close();
          }}
          dismissible={!busy}
          initialFocus={mode === "key" ? input : cancelButton}
        >
          <form noValidate onSubmit={save}>
            <div className="dialog-header">
              <DialogTitle>
                {mode === "remove"
                  ? "Remove saved API key"
                  : savedKey
                    ? "Replace API key"
                    : "Add API key"}
              </DialogTitle>
              <Button
                className="icon-button"
                aria-label="Close API key settings"
                disabled={busy}
                onClick={close}
              >
                <X size={17} />
              </Button>
            </div>
            <div className="dialog-body">
              <DialogDescription>
                {mode === "remove"
                  ? `Remove the ${label} key saved in this OpenHack workspace?`
                  : `Save your ${label} key for scans in this local workspace.`}
              </DialogDescription>
              {mode === "key" && (
                <label className="field" htmlFor="provider-api-key">
                  <span>API key</span>
                  <Input
                    id="provider-api-key"
                    ref={input}
                    type="password"
                    value={key}
                    onChange={(event) => setKey(event.target.value)}
                    autoComplete="new-password"
                    autoCorrect="off"
                    autoCapitalize="off"
                    spellCheck={false}
                    maxLength={8192}
                    disabled={busy}
                    aria-invalid={!!error}
                    aria-describedby={error ? "provider-key-error" : undefined}
                    placeholder="Enter your API key"
                  />
                  <small>
                    Saved on this machine in the workspace's credentials.json
                    file with owner-only permissions.
                  </small>
                </label>
              )}
              {environmentKey && (
                <p className="helper credential-dialog-note">
                  {mode === "remove"
                    ? "The environment key is unaffected and will remain active."
                    : "The environment key remains active and takes precedence over this saved key."}
                </p>
              )}
              <ErrorBanner id="provider-key-error" message={error} />
            </div>
            <div className="dialog-footer">
              <Button
                ref={cancelButton}
                className="button"
                disabled={busy}
                onClick={close}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                className="button primary"
                disabled={busy || (mode === "key" && !key.trim())}
              >
                {mode === "key" && <Save size={13} />}{" "}
                {busy
                  ? mode === "remove"
                    ? "Removing…"
                    : "Saving…"
                  : mode === "remove"
                    ? "Remove saved key"
                    : "Save API key"}
              </Button>
            </div>
          </form>
        </Dialog>
      )}
    </div>
  );
}
