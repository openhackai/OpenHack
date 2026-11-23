import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Button } from "../components/controls/Button";
import { Tooltip } from "../components/controls/Tooltip";
import { providerDefinitions } from "../../../src/shared/providers";
import type { FindingRecord, Report } from "../../../src/shared/contracts";
import {
  ArrowLeft,
  ChevronRight,
  Play,
  RefreshCw,
  ShieldCheck,
  Square,
} from "../components/nucleo-icons";
import { ProviderIcon } from "../components/provider-icons";
import { CopyForAgent } from "../components/CopyForAgent";
import { findingsMarkdown } from "../agent-markdown";
import { FindingsInbox } from "../components/FindingsInbox";
import { routeHash } from "../App";
import { request, ApiError } from "../api";
import { dateTime, duration, ErrorBanner, Status } from "../components/ui";

type Task = NonNullable<Report["execution"]>["tasks"][number];
type Tone = "running" | "success" | "danger" | "warning" | "muted";

const STAGES: { key: string; label: string; roles: Task["role"][] }[] = [
  { key: "recon", label: "Recon", roles: ["planner"] },
  { key: "review", label: "Source review", roles: ["research"] },
  { key: "validation", label: "Validation", roles: ["advocate", "validator"] },
  { key: "judging", label: "Judging", roles: ["judge"] },
];

const SCAN_STATUS: Record<Report["state"], { label: string; tone: Tone }> = {
  running: { label: "Scanning", tone: "running" },
  complete: { label: "Completed", tone: "success" },
  partial: { label: "Partial", tone: "warning" },
  failed: { label: "Failed", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "muted" },
};

const TASK_STATUS: Record<Task["state"], { label: string; tone: Tone }> = {
  queued: { label: "Queued", tone: "muted" },
  blocked: { label: "Blocked", tone: "muted" },
  running: { label: "Running", tone: "running" },
  complete: { label: "Done", tone: "success" },
  limited: { label: "Limited", tone: "warning" },
  failed: { label: "Failed", tone: "danger" },
};

const DONE: Task["state"][] = ["complete", "limited", "failed"];

function seconds(value: number) {
  if (!value) return "";
  return value < 60 ? `${value}s` : `${Math.floor(value / 60)}m ${value % 60}s`;
}

function compact(value: number) {
  return value >= 1_000_000
    ? `${(value / 1_000_000).toFixed(1)}M`
    : value >= 10_000
      ? `${Math.round(value / 1000)}k`
      : value.toLocaleString();
}

function StatusDot({ tone, label }: { tone: Tone; label: string }) {
  return (
    <span className={`dot-status tone-${tone}`}>
      <i aria-hidden="true" />
      {label}
    </span>
  );
}

function stageStatus(tasks: Task[]): { label: string; tone: Tone } {
  if (tasks.some((task) => task.state === "running"))
    return { label: "Running", tone: "running" };
  if (tasks.some((task) => task.state === "failed")) {
    if (tasks.some((task) => task.state === "queued"))
      return { label: "In progress", tone: "running" };
    return tasks.some((task) => task.state === "complete" || task.state === "limited")
      ? { label: "Partial", tone: "warning" }
      : { label: "Failed", tone: "danger" };
  }
  if (tasks.every((task) => DONE.includes(task.state)))
    return tasks.some((task) => task.state === "limited")
      ? { label: "Limited", tone: "warning" }
      : { label: "Done", tone: "success" };
  if (tasks.some((task) => DONE.includes(task.state)))
    return { label: "In progress", tone: "running" };
  return { label: "Queued", tone: "muted" };
}

function Disclosure({
  open,
  onToggle,
  children,
  className = "",
}: {
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
  className?: string;
}) {
  return (
    <button
      type="button"
      className={`disclosure ${className}`}
      aria-expanded={open}
      onClick={onToggle}
    >
      <ChevronRight size={12} className="disclosure-chevron" />
      {children}
    </button>
  );
}

function Progress({ scan }: { scan: Report }) {
  const tasks = scan.execution?.tasks ?? [];
  const [toggled, setToggled] = useState<Record<string, boolean>>({});
  const stages = STAGES.map((stage) => ({
    ...stage,
    tasks: tasks.filter((task) => stage.roles.includes(task.role)),
  })).filter((stage) => stage.tasks.length);
  const done = tasks.filter((task) => DONE.includes(task.state)).length;
  const percent = tasks.length ? (done / tasks.length) * 100 : 0;
  const tone = SCAN_STATUS[scan.state].tone;

  return (
    <section className="card" aria-label="Scan progress">
      <header className="card-header">
        <h3>Progress</h3>
        <span className="muted truncate">{scan.phase}</span>
        <span className="spacer" />
        {tasks.length > 0 && (
          <span className="muted nowrap">
            {done} of {tasks.length} tasks
          </span>
        )}
      </header>
      <div
        className="progress-track"
        role="progressbar"
        aria-label="Tasks complete"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(percent)}
      >
        <div
          className={`progress-fill tone-${tone}`}
          style={{ width: `${percent}%` }}
        />
      </div>
      {scan.inventory.limited && (
        <p className="card-note">
          Source inventory was limited; coverage is partial.
        </p>
      )}
      {stages.length > 0 && (
        <ul className="stage-list">
          {stages.map((stage) => {
            const status = stageStatus(stage.tasks);
            const stageDone = stage.tasks.filter((task) =>
              DONE.includes(task.state),
            ).length;
            const open =
              toggled[stage.key] ??
              (status.tone === "running" || status.tone === "danger");
            return (
              <li key={stage.key} className="stage">
                <Disclosure
                  open={open}
                  onToggle={() =>
                    setToggled({ ...toggled, [stage.key]: !open })
                  }
                  className="stage-row"
                >
                  <span className="stage-name">{stage.label}</span>
                  <span className="muted nowrap">
                    {stageDone}/{stage.tasks.length}
                  </span>
                  <span className="spacer" />
                  {status.tone !== "success" && (
                    <span className="stage-bar" aria-hidden="true">
                      <span
                        className={`tone-${status.tone}`}
                        style={{
                          width: `${(stageDone / stage.tasks.length) * 100}%`,
                        }}
                      />
                    </span>
                  )}
                  <span className="stage-status">
                    <StatusDot {...status} />
                  </span>
                </Disclosure>
                {open && (
                  <ul className="task-list">
                    {stage.tasks.map((task) => (
                      <li className="task-row" key={task.id}>
                        <span className="truncate">{task.title}</span>
                        <span className="muted nowrap">
                          {seconds(task.elapsed)}
                        </span>
                        <span className="stage-status">
                          <StatusDot {...TASK_STATUS[task.state]} />
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function Activity({ scan }: { scan: Report }) {
  const [open, setOpen] = useState(false);
  const events = scan.events.slice(-60).reverse();
  if (!events.length) return null;
  return (
    <section className="card" aria-label="Activity">
      <Disclosure
        open={open}
        onToggle={() => setOpen(!open)}
        className="card-header"
      >
        <h3>Activity</h3>
        <span className="muted nowrap">{scan.events.length} events</span>
        {!open && (
          <span className="muted truncate activity-latest">
            · {events[0]?.message}
          </span>
        )}
      </Disclosure>
      {open && (
        <ul className="activity-list">
          {events.map((event, index) => (
            <li className="activity-row" key={`${event.at}-${index}`}>
              <time>{new Date(event.at).toLocaleTimeString()}</time>
              <span>{event.message}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="detail-fact">
      <span className="fact-label">{label}</span>
      <span className="fact-value">{children}</span>
    </div>
  );
}

/** Scan findings with their triage state; refetched as the scan changes. */
function useScanFindings(scan: Report) {
  const [records, setRecords] = useState<FindingRecord[]>();
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const signature = `${scan.id}|${scan.findings.length}|${scan.state}|${scan.findings
    .map((f) => f.validation)
    .join(",")}`;
  useEffect(() => {
    const controller = new AbortController();
    const signal = controller.signal;
    setRecords(undefined);
    setError(null);
    void request<{ findings: FindingRecord[] }>(
      `/scans/${encodeURIComponent(scan.id)}/findings`,
      { signal },
    )
      .then((result) => result.findings)
      .catch(async (failure) => {
        // Dashboards started before this route existed: read each finding.
        if (!(failure instanceof ApiError) || failure.status !== 404)
          throw failure;
        return Promise.all(
          scan.findings.map((finding) =>
            request<{ finding: FindingRecord }>(
              `/findings/${encodeURIComponent(`${scan.id}:${finding.id}`)}`,
              { signal },
            ).then((result) => result.finding),
          ),
        );
      })
      .then((list) => {
        if (!signal.aborted) setRecords(list);
      })
      .catch(() => {
        if (!signal.aborted) {
          setRecords(undefined);
          setError("Could not load saved findings and triage. Retry to edit findings safely.");
        }
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, version]);
  const reload = useCallback(() => setVersion((v) => v + 1), []);
  return { records, reload, error };
}

function Overview({ scan }: { scan: Report }) {
  const provider = scan.provider ?? "openai";
  const tokens = scan.usage.input + scan.usage.output;
  return (
    <div className="detail-scroll scan-body">
      {scan.error &&
        (scan.state === "cancelled" ? (
          <p className="scan-note" role="status">
            {scan.error} Resume to finish validating and judging.
          </p>
        ) : (
          <ErrorBanner message={scan.error} />
        ))}
      <section className="card" aria-label="Scan details">
        <div className="detail-facts">
          <Fact label="Project">
            <Tooltip content={scan.root}>
              <span tabIndex={0}>{scan.project}</span>
            </Tooltip>
          </Fact>
          <Fact label="Status">
            <Status value={scan.state} />
          </Fact>
          <Fact label="Started">{dateTime(scan.startedAt)}</Fact>
          <Fact label="Duration">
            {duration(scan.startedAt, scan.finishedAt)}
          </Fact>
          <Fact label="Provider">
            <span className="fact-provider">
              <ProviderIcon provider={provider} size={13} />
              {providerDefinitions[provider].label}
            </span>
          </Fact>
          <Fact label="Model">
            <code>{scan.model}</code>
          </Fact>
          <Fact label="Files">{scan.inventory.files.toLocaleString()}</Fact>
          <Fact label="Tokens">{tokens.toLocaleString()}</Fact>
        </div>
        <div className="detail-path">
          <span className="fact-label">Directory</span>
          <code>{scan.root}</code>
        </div>
      </section>
      <Progress scan={scan} />
      <Activity scan={scan} />
    </div>
  );
}

export function ScanDetail({
  scan,
  number,
  findingId,
  busy,
  error,
  onAction,
  onRefresh,
}: {
  scan: Report;
  number?: number;
  findingId?: string;
  busy: boolean;
  error: string | null;
  onAction: (action: "resume" | "cancel") => void;
  onRefresh: () => Promise<void>;
}) {
  const { records, reload, error: findingsError } = useScanFindings(scan);
  // Findings stay hidden until the scan finishes: the council can still reject them.
  const running = scan.state === "running";
  const listed = running
    ? []
    : (records ?? []).filter((f) => f.validation !== "rejected");
  const open = listed.filter((f) => f.status === "open");
  const [view, setView] = useState<"findings" | "overview">("findings");
  const tasks = scan.execution?.tasks ?? [];
  const done = tasks.filter((task) =>
    ["complete", "limited", "failed"].includes(task.state),
  ).length;
  const provider = scan.provider ?? "openai";
  return (
    <>
      <header className="pagebar scan-bar">
        <div className="page-title">
          <a className="button back-button" href="#/scans">
            <ArrowLeft size={11} />
            Back
          </a>
          <h1>Viewing scan {number ? `#${number}` : scan.id.slice(0, 8)}</h1>
          <span className="scan-bar-meta">
            <Tooltip content={scan.root}>
              <span tabIndex={0}>{scan.project}</span>
            </Tooltip>
            <Status value={scan.state} />
            <span className="fact-provider">
              <ProviderIcon provider={provider} size={12} />
              <code>{scan.model}</code>
            </span>
            <span>{duration(scan.startedAt, scan.finishedAt)}</span>
          </span>
        </div>
        <div className="actions">
          {scan.state === "running" ? (
            <Button
              className="button bar-button tone-danger-text"
              disabled={busy}
              onClick={() => onAction("cancel")}
            >
              <Square size={10} />
              Stop scan
            </Button>
          ) : (
            ["failed", "partial", "cancelled"].includes(scan.state) && (
              <Button
                className="button bar-button"
                disabled={busy}
                onClick={() => onAction("resume")}
              >
                <Play size={11} />
                Resume scan
              </Button>
            )
          )}
          {open.length > 0 && (
            <CopyForAgent
              label={
                open.length > 1
                  ? `Copy ${open.length} for Agent`
                  : "Copy for Agent"
              }
              markdown={() =>
                findingsMarkdown(open, {
                  project: scan.project,
                  root: scan.root,
                })
              }
            />
          )}
          <Button
            className="button icon-button bar-icon"
            aria-label="Refresh scan"
            onClick={() => {
              reload();
              void onRefresh();
            }}
          >
            <RefreshCw size={13} />
          </Button>
        </div>
      </header>
      <nav className="view-tabs" aria-label="Scan views">
        <button
          type="button"
          aria-pressed={view === "findings"}
          onClick={() => setView("findings")}
        >
          Findings <span>{listed.length || ""}</span>
        </button>
        <button
          type="button"
          aria-pressed={view === "overview"}
          onClick={() => setView("overview")}
        >
          Progress <span>{tasks.length ? `${done}/${tasks.length}` : ""}</span>
        </button>
      </nav>
      <ErrorBanner message={error} />
      {view === "overview" ? (
        <Overview scan={scan} />
      ) : findingsError ? (
        <div className="scan-empty">
          <ErrorBanner message={findingsError} />
          <Button className="button" onClick={reload}>Retry</Button>
        </div>
      ) : (
        <FindingsInbox
          key={scan.id}
          findings={listed}
          selectedId={findingId}
          hrefFor={(key) => routeHash("scans", scan.id, key)}
          listHref={routeHash("scans", scan.id)}
          groupBy="severity"
          initialOpenClosed="all"
          searchPlaceholder="Find a finding in this scan…"
          showScanLink={false}
          backLabel={`Scan ${number ? `#${number}` : ""}`.trim()}
          emptyListText={
            records === undefined
              ? "Loading findings…"
              : scan.state === "running"
                ? "Findings appear when the scan finishes."
                : "No findings in this scan."
          }
          emptyDetail={
            <div className="scan-empty">
              <div className="scan-empty-icon">
                <ShieldCheck size={20} strokeWidth={1.4} />
              </div>
              <h2>
                {records === undefined
                  ? "Loading findings…"
                  : scan.state === "running"
                    ? "Scanning"
                    : "No findings"}
              </h2>
              <p>
                {scan.state === "running"
                  ? "Findings show here once the council finishes validating and judging them, so nothing you see is later rejected. You can leave this page."
                  : scan.findings.length
                    ? "Every candidate was rejected during validation."
                    : "This scan didn't report any vulnerabilities."}
              </p>
              <Button className="button" onClick={() => setView("overview")}>
                View progress
                <ChevronRight size={11} />
              </Button>
            </div>
          }
          onSaved={async () => {
            reload();
            await onRefresh();
          }}
        />
      )}
    </>
  );
}
