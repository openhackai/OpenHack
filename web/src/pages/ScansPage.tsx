import { projectOptions } from "../project-options";
import { Button } from "../components/controls/Button";
import { Select } from "../components/controls/Select";
import { Tooltip } from "../components/controls/Tooltip";
import { useEffect, useState } from "react";
import { useRequestGate } from "../use-request-gate";
import { ArrowLeft, Play, RefreshCw } from "../components/nucleo-icons";
import type { Report } from "../../../src/shared/contracts";
import { navigate } from "../App";
import { request, errorMessage } from "../api";
import {
  dateTime,
  duration,
  relative,
  EmptyState,
  ErrorBanner,
  Loading,
  SearchField,
  Status,
} from "../components/ui";
import { NewScanDialog } from "../components/NewScanDialog";
import { ScanDetail } from "./ScanDetail";
import { ProviderIcon } from "../components/provider-icons";
import { providerDefinitions } from "../../../src/shared/providers";

export function ScansPage({
  scans,
  loading,
  selectedId,
  findingId,
  refresh,
}: {
  scans: Report[];
  loading: boolean;
  selectedId?: string;
  findingId?: string;
  refresh: () => Promise<void>;
}) {
  const action = useRequestGate();
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("all");
  const [project, setProject] = useState("all");
  const [newScan, setNewScan] = useState(false);
  const [createdScan, setCreatedScan] = useState<Report>();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setBusy(false);
    setError(null);
    return () => action.cancel();
  }, [selectedId, action]);
  const selected =
    scans.find((scan) => scan.id === selectedId) ??
    (createdScan?.id === selectedId ? createdScan : undefined);
  useEffect(() => {
    if (createdScan && scans.some((scan) => scan.id === createdScan.id)) {
      setCreatedScan(undefined);
    }
  }, [scans, createdScan]);
  const projects = projectOptions(scans);
  // Chronological scan numbers, oldest first (#1), like company/app.
  const numbers = new Map(
    [...scans]
      .sort(
        (a, b) =>
          a.startedAt.localeCompare(b.startedAt) || a.id.localeCompare(b.id),
      )
      .map((scan, index) => [scan.id, index + 1]),
  );
  const filtered = scans.filter(
    (scan) =>
      (status === "all" || scan.state === status) &&
      (project === "all" || scan.root === project) &&
      `${scan.project} ${scan.root} ${scan.id}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  const showDetail = !loading && Boolean(selectedId && selected);
  async function act(scanAction: "resume" | "cancel") {
    if (!selected) return;
    const signal = action.begin();
    if (!signal) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/scans/${encodeURIComponent(selected.id)}/${scanAction}`, {
        method: "POST",
        signal,
        body: "{}",
      });
      if (action.owns(signal)) await refresh();
    } catch (failure) {
      if (action.owns(signal)) setError(errorMessage(failure));
    } finally {
      if (action.owns(signal)) setBusy(false);
      action.finish(signal);
    }
  }
  return (
    <section className="page">
      {!showDetail && (
        <div className="pagebar">
          <div className="page-title">
            {selectedId && (
              <Button
                className="icon-button"
                aria-label="Back to scans"
                onClick={() => navigate("scans")}
              >
                <ArrowLeft size={15} />
              </Button>
            )}
            <h1>{selected ? selected.project : "Scans"}</h1>
            <span className="muted">
              {selected
                ? selected.id.slice(0, 8)
                : `${scans.length} total${scans.some((scan) => scan.state === "running") ? ` · ${scans.filter((scan) => scan.state === "running").length} running` : ""}`}
            </span>
          </div>
          <div className="actions">
            <Button
              className="button icon-button"
              aria-label="Refresh scans"
              onClick={() => void refresh()}
            >
              <RefreshCw size={14} />
            </Button>
            <Button className="button primary" onClick={() => setNewScan(true)}>
              <Play size={12} />
              Run scan
            </Button>
          </div>
        </div>
      )}
      {!showDetail && <ErrorBanner message={error} />}
      {loading ? (
        <Loading />
      ) : selectedId ? (
        selected ? (
          <ScanDetail
            key={selected.id}
            scan={selected}
            busy={busy}
            error={error}
            onAction={(scanAction) => void act(scanAction)}
            number={numbers.get(selected.id)}
            findingId={findingId}
            onRefresh={refresh}
          />
        ) : (
          <EmptyState title="Scan not found">
            This scan is no longer available.{" "}
            <a href="#/scans">Return to scans.</a>
          </EmptyState>
        )
      ) : (
        <>
          <div className="toolbar">
            <SearchField
              value={query}
              onChange={setQuery}
              placeholder="Filter by project or directory…"
            />
            <Select
              className="bar-filter"
              prefix="Project"
              aria-label="Filter by project"
              value={project}
              onValueChange={setProject}
              options={[
                { value: "all", label: "All" },
                ...projects,
              ]}
            />
            <Select
              className="bar-filter"
              prefix="Status"
              aria-label="Filter by status"
              value={status}
              onValueChange={setStatus}
              options={[
                { value: "all", label: "All" },
                { value: "running", label: "Scanning" },
                { value: "complete", label: "Completed" },
                { value: "partial", label: "Partial" },
                { value: "failed", label: "Failed" },
                { value: "cancelled", label: "Cancelled" },
              ]}
            />
          </div>
          <div className="table-scroll">
            <table className="scans-table">
              <thead>
                <tr>
                  <th>Project</th>
                  <th>Provider</th>
                  <th>Status</th>
                  <th>Findings</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((scan) => {
                  const href = `#/scans/${encodeURIComponent(scan.id)}`;
                  const provider = scan.provider ?? "openai";
                  const counts = (
                    ["critical", "high", "medium", "low"] as const
                  )
                    .map((severity) => ({
                      severity,
                      count: scan.findings.filter(
                        (f) =>
                          f.severity === severity &&
                          f.validation !== "rejected",
                      ).length,
                    }))
                    .filter((item) => item.count);
                  return (
                    <tr
                      key={scan.id}
                      className="row-link"
                      onClick={(event) => {
                        if (!(event.target as HTMLElement).closest("a"))
                          navigate("scans", scan.id);
                      }}
                    >
                      <td>
                        <a href={href} className="project-name">
                          <span className="truncate">{scan.project}</span>
                          <small className="truncate" title={scan.root}>
                            {scan.root}
                          </small>
                        </a>
                      </td>
                      <td>
                        <span className="provider-cell">
                          <ProviderIcon provider={provider} size={14} />
                          <span className="truncate">
                            {providerDefinitions[provider].label}
                            <small>{scan.model}</small>
                          </span>
                        </span>
                      </td>
                      <td>
                        <span className="scan-status">
                          <Status value={scan.state} />
                          <span className="muted">
                            {duration(scan.startedAt, scan.finishedAt)}
                          </span>
                        </span>
                      </td>
                      <td>
                        {scan.state === "running" ? (
                          <span className="muted">Pending</span>
                        ) : counts.length ? (
                          <span className="severity-summary">
                            {counts.slice(0, 2).map(({ severity, count }) => (
                              <span key={severity}>
                                <i className={`severity-square ${severity}`} />
                                {count}{" "}
                                {severity.charAt(0).toUpperCase() +
                                  severity.slice(1)}
                              </span>
                            ))}
                            {counts.length > 2 && (
                              <span className="muted">
                                +
                                {counts
                                  .slice(2)
                                  .reduce((sum, item) => sum + item.count, 0)}
                              </span>
                            )}
                          </span>
                        ) : (
                          <span className="muted">No findings</span>
                        )}
                      </td>
                      <td className="date-cell">
                        <Tooltip content={dateTime(scan.startedAt)}>
                          <span tabIndex={0}>{relative(scan.startedAt)}</span>
                        </Tooltip>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {filtered.length === 0 && (
              <EmptyState
                title={scans.length ? "No matching scans" : "No scans yet"}
                action={
                  !scans.length && (
                    <Button
                      className="button primary"
                      onClick={() => setNewScan(true)}
                    >
                      <Play size={12} />
                      Run your first scan
                    </Button>
                  )
                }
              >
                {scans.length ? (
                  "Try another search or status filter."
                ) : (
                  <>
                    <p>
                      Run a scan of any local project to review its findings
                      here.
                    </p>
                    <code>openhack scan /path/to/project</code>
                  </>
                )}
              </EmptyState>
            )}
          </div>
          <footer className="table-footer">
            {filtered.length} scan{filtered.length === 1 ? "" : "s"}
            <span>Across all local projects</span>
          </footer>
        </>
      )}
      {newScan && (
        <NewScanDialog
          onClose={() => setNewScan(false)}
          onStarted={(scan) => {
            setCreatedScan(scan);
            setNewScan(false);
            navigate("scans", scan.id);
            void refresh();
          }}
        />
      )}
    </section>
  );
}
