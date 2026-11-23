import { useEffect, useRef, useState, type FormEvent } from "react";
import { Folder, Play, X } from "./nucleo-icons";
import type {
  DirectoryPickResponse,
  ScanResponse,
  SettingsResponse,
} from "../../../src/shared/contracts";
import { providerDefinitions } from "../../../src/shared/providers";
import { ApiError, errorMessage, request } from "../api";
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
  const [choosing, setChoosing] = useState(false);
  const chooser = useRef<AbortController | null>(null);
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
  // Closing this dialog aborts the request, which also closes the native folder chooser.
  useEffect(() => () => chooser.current?.abort(), []);
  async function browse() {
    if (chooser.current || busy) return;
    const controller = new AbortController();
    chooser.current = controller;
    setChoosing(true);
    setError(null);
    try {
      const result = await request<DirectoryPickResponse>("/directories/pick", {
        method: "POST",
        signal: controller.signal,
        body: "{}",
        timeoutMs: 11 * 60_000,
      });
      if (controller.signal.aborted) return;
      if (result.path) {
        setPath(result.path);
        input.current?.focus();
      }
    } catch (failure) {
      if (controller.signal.aborted) return;
      // No native picker here (or an older dashboard): use the in-app browser.
      if (
        failure instanceof ApiError &&
        (failure.status === 501 || failure.status === 404)
      )
        setPicking(true);
      else setError(errorMessage(failure));
    } finally {
      if (chooser.current === controller) {
        chooser.current = null;
        setChoosing(false);
      }
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    event.stopPropagation();
    if (!settings || picking || choosing || busy) return;
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
      dismissible={!busy && !picking && !choosing}
      initialFocus={input}
    >
      <form onSubmit={submit} noValidate>
        <div className="dialog-header">
          <DialogTitle>Run a scan</DialogTitle>
          <Button
            type="button"
            className="icon-button"
            onClick={onClose}
            disabled={busy}
            aria-label="Close new scan"
          >
            <X size={15} />
          </Button>
        </div>
        <div className="dialog-body">
          <DialogDescription>
            Pick a project folder. It's scanned on this machine.
          </DialogDescription>
          <div className="field">
            <label className="field-label sr-only" htmlFor="new-scan-path">
              Project folder
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
                onClick={() => void browse()}
                disabled={busy || choosing}
                aria-busy={choosing}
                aria-label={
                  choosing
                    ? "Choosing a project folder"
                    : "Browse project folders"
                }
              >
                <Folder size={14} />
                {choosing ? "Choosing…" : "Browse"}
              </Button>
            </div>
          </div>
          {providerStatus && !providerStatus.ready && (
            <p className="helper new-scan-warning" role="status">
              {selectedProvider?.label} isn't ready: {providerStatus.detail}{" "}
              <a href="#/settings" onClick={onClose}>
                Open Settings
              </a>
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
            disabled={busy || choosing || !settings || !path.trim()}
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
