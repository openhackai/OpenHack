import { useCallback, useEffect, useRef, useState } from "react";
import type { DirectoryListing } from "../../../src/shared/contracts";
import { request, errorMessage } from "../api";
import {
  ArrowLeft,
  ChevronRight,
  Folder,
  LoaderCircle,
  X,
} from "./nucleo-icons";
import {
  Button,
  Checkbox,
  Dialog,
  DialogDescription,
  DialogTitle,
  Input,
} from "./controls";
import { ErrorBanner } from "./ui";

/** Browses directory names on the local server; selecting returns only an absolute path. */
export function FolderPicker({
  initialPath,
  onSelect,
  onClose,
}: {
  initialPath?: string;
  onSelect: (path: string) => void;
  onClose: () => void;
}) {
  const [listing, setListing] = useState<DirectoryListing>();
  const [path, setPath] = useState(initialPath ?? "");
  const [showHidden, setShowHidden] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const pathInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async (target?: string, hidden = false) => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setLoading(true);
    setError(null);
    try {
      const result = await request<DirectoryListing>("/directories/list", {
        method: "POST",
        signal: next.signal,
        body: JSON.stringify({
          path: target?.trim() || undefined,
          showHidden: hidden,
        }),
      });
      if (next.signal.aborted) return;
      setListing(result);
      setPath(result.path);
    } catch (failure) {
      if (!next.signal.aborted) setError(errorMessage(failure));
    } finally {
      if (!next.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(initialPath);
    return () => controller.current?.abort();
  }, [initialPath, load]);

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      initialFocus={pathInput}
    >
      <div className="dialog-header">
        <DialogTitle>Select project folder</DialogTitle>
        <Button
          className="icon-button"
          onClick={onClose}
          aria-label="Close folder picker"
        >
          <X size={17} />
        </Button>
      </div>
      <div className="folder-picker-body">
        <DialogDescription>
          Choose the folder you want to scan.
        </DialogDescription>
        <form
          className="folder-location"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            event.stopPropagation();
            void load(path, showHidden);
          }}
        >
          <Button
            className="button icon-button"
            aria-label="Parent folder"
            disabled={loading || !listing?.parent}
            onClick={() => void load(listing?.parent ?? undefined, showHidden)}
          >
            <ArrowLeft size={16} />
          </Button>
          <Input
            ref={pathInput}
            aria-label="Folder path"
            value={path}
            onChange={(event) => setPath(event.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
          <Button className="button" type="submit" disabled={loading}>
            Go
          </Button>
        </form>
        <div className="folder-options">
          <Button
            className="button"
            onClick={() => void load(undefined, showHidden)}
            disabled={loading}
          >
            Home folder
          </Button>
          <label className="checkbox-label">
            <Checkbox
              checked={showHidden}
              onCheckedChange={(checked) => {
                setShowHidden(checked);
                void load(listing?.path, checked);
              }}
            />
            Show hidden
          </label>
        </div>
        <ErrorBanner message={error} />
        <div className="folder-list" aria-label="Folders" aria-busy={loading}>
          {loading ? (
            <div className="folder-empty" role="status">
              <LoaderCircle className="spin" size={16} /> Loading folders…
            </div>
          ) : listing?.directories.length ? (
            listing.directories.map((directory) => (
              <Button
                key={directory.path}
                className="folder-row"
                onClick={() => void load(directory.path, showHidden)}
                aria-label={`Open folder ${directory.name}`}
              >
                <Folder size={17} />
                <span>{directory.name}</span>
                <ChevronRight size={12} />
              </Button>
            ))
          ) : (
            <div className="folder-empty">
              {listing
                ? "No subfolders. You can select this folder."
                : "Enter a folder path or open your home folder."}
            </div>
          )}
        </div>
        {listing?.truncated && (
          <p className="helper">
            Some folders are not shown. Enter a path above to open one directly.
          </p>
        )}
        {listing && (
          <p className="folder-selection">
            <span>Selected folder</span>
            <span>{listing.path}</span>
          </p>
        )}
      </div>
      <div className="dialog-footer">
        <Button className="button" onClick={onClose}>
          Cancel
        </Button>
        <Button
          className="button primary"
          disabled={
            loading || !listing || !!error || path.trim() !== listing.path
          }
          onClick={() => {
            if (listing) onSelect(listing.path);
          }}
        >
          <Folder size={14} />
          Select folder
        </Button>
      </div>
    </Dialog>
  );
}
