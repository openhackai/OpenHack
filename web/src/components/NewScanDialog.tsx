import { useEffect, useRef, useState, type FormEvent } from "react";
import { Folder, Play, X } from "./nucleo-icons";
import type {
  ScanResponse,
  SettingsResponse,
} from "../../../src/shared/contracts";
import { providerDefinitions } from "../../../src/shared/providers";
import { errorMessage, request } from "../api";
import { ErrorBanner } from "./ui";
import { FolderPicker } from "./FolderPicker";
import { useRequestGate } from "../use-request-gate";
import {
  Button,
  Dialog,
  DialogTitle,
  DialogDescription,
  Input,
} from "./controls";

export function NewScanDialog({
  onClose,
  onStarted,
}: {
  onClose: () => void;
  onStarted: (scan: ScanResponse["scan"]) => void;
}) {
  const action = useRequestGate();
  const input = useRef<HTMLInputElement>(null);
  const [path, setPath] = useState("");
  const [settings, setSettings] = useState<SettingsResponse["settings"]>();
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selectedProvider = settings && providerDefinitions[settings.provider];
  const providerStatus = settings?.providers.find(
    (provider) => provider.id === settings.provider,
  );
  useEffect(() => {
    const controller = new AbortController();
    void request<SettingsResponse>("/settings", { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setSettings(value.settings);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      });
    return () => controller.abort();
  }, []);
  async function submit(event: FormEvent) {
    event.preventDefault();
    event.stopPropagation();
    if (!settings || picking || busy) return;
    if (!path.trim() || path.trim().length > 4096) {
      setError("Enter a project directory of up to 4,096 characters.");
      input.current?.focus();
      return;
    }
    const signal = action.begin();
    if (!signal) return;
    setBusy(true);
    setError(null);
    try {
      const result = await request<ScanResponse>("/scans", {
        method: "POST",
        signal,
        body: JSON.stringify({ path: path.trim() }),
      });
      if (action.owns(signal)) onStarted(result.scan);
    } catch (failure) {
      if (action.owns(signal)) setError(errorMessage(failure));
    } finally {
      if (action.owns(signal)) setBusy(false);
      action.finish(signal);
    }
  }
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      dismissible={!busy && !picking}
      initialFocus={input}
    >
      <form onSubmit={submit} noValidate>
        <div className="dialog-header">
          <DialogTitle>Run a new scan</DialogTitle>
          <Button
            type="button"
            className="icon-button"
            onClick={onClose}
            disabled={busy}
            aria-label="Close new scan"
          >
            <X size={17} />
          </Button>
        </div>
        <div className="dialog-body">
          <DialogDescription>
            Scan a local project with the OpenHack source harness.
          </DialogDescription>
          <div className="field">
            <label className="field-label" htmlFor="new-scan-path">
              Project directory
            </label>
            <div className="directory-input">
              <Input
                id="new-scan-path"
                ref={input}
                required
                disabled={busy}
                maxLength={4096}
                aria-describedby={error ? "new-scan-error" : undefined}
                value={path}
                onChange={(event) => setPath(event.target.value)}
                placeholder="/path/to/your/project"
                autoComplete="off"
                spellCheck={false}
              />
              <Button
                className="button"
                onClick={() => setPicking(true)}
                disabled={busy}
                aria-label="Browse project folders"
              >
                <Folder size={15} />
                Browse
              </Button>
            </div>
          </div>
          {settings && (
            <div className="scan-config">
              <span>
                {selectedProvider?.label} ·{" "}
                <strong>
                  {settings.model === "default" && settings.provider === "codex"
                    ? "CLI default model"
                    : settings.model}
                </strong>
              </span>
              <span>
                {settings.workers} workers · {settings.steps} tool rounds
              </span>
            </div>
          )}
          <p className="helper">
            Uses your saved harness settings.{" "}
            {selectedProvider?.description ?? "Loading scan provider…"}
          </p>
          {providerStatus && !providerStatus.ready && (
            <p className="helper" role="status">
              {providerStatus.detail}
            </p>
          )}
          <ErrorBanner id="new-scan-error" message={error} />
        </div>
        <div className="dialog-footer">
          <Button
            type="button"
            className="button"
            onClick={onClose}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            className="button primary"
            disabled={busy || !settings || !path.trim()}
          >
            <Play size={12} />
            {busy ? "Starting…" : "Run scan"}
          </Button>
        </div>
      </form>
      {picking && (
        <FolderPicker
          initialPath={path}
          onClose={() => setPicking(false)}
          onSelect={(selected) => {
            setPath(selected);
            setPicking(false);
            setError(null);
          }}
        />
      )}
    </Dialog>
  );
}
