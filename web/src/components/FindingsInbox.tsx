import { Button } from "./controls/Button";
import { Select } from "./controls/Select";
import { Textarea } from "./controls/Textarea";
import { Tooltip } from "./controls/Tooltip";
import { Input } from "./controls/Input";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useRequestGate } from "../use-request-gate";
import {
  ArrowLeft,
  ArrowUpRight,
  Check,
  ChevronDown,
  ChevronRight,
  Circle,
  CircleSlash,
  Search,
} from "./nucleo-icons";
import type {
  FindingRecord,
  Status as FindingStatus,
} from "../../../src/shared/contracts";
import { replaceRoute } from "../App";
import { errorMessage, request } from "../api";
import { EmptyState, ErrorBanner, Loading, Severity, dateTime } from "./ui";
import { CopyForAgent } from "./CopyForAgent";
import { findingMarkdown } from "../agent-markdown";
import "../pages/findings.css";

const SEVERITY_RANK: Record<string, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
};
const rank = (severity: string) => SEVERITY_RANK[severity] ?? 4;
export type OpenClosed = "open" | "closed" | "all";
interface Group {
  key: string;
  label: string;
  root: string;
  rows: FindingRecord[];
}

const NARROW_QUERY = "(max-width: 760px)";
function useNarrow() {
  const [narrow, setNarrow] = useState(
    () => window.matchMedia?.(NARROW_QUERY).matches ?? false,
  );
  useEffect(() => {
    const media = window.matchMedia?.(NARROW_QUERY);
    if (!media) return;
    const change = () => setNarrow(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  return narrow;
}

/** Selects a finding without adding a history entry (keyboard + auto-select). */
function foundAt(finding: FindingRecord): string | undefined {
  const stamp = Number(finding.scanId.split("-")[0]);
  return Number.isFinite(stamp) && stamp > 0
    ? dateTime(new Date(stamp).toISOString())
    : undefined;
}

export type GroupBy = "project" | "severity";

/**
 * Two-pane findings inbox (company/app vulnerabilities inbox): searchable,
 * grouped list on the left, the selected finding on the right. Used by the
 * Findings page (grouped by project) and scan detail (grouped by severity).
 */
export function FindingsInbox({
  findings,
  selectedId,
  hrefFor,
  listHref,
  groupBy,
  searchPlaceholder = "Find a finding…",
  initialOpenClosed = "open",
  onSaved,
  loadMissing = false,
  showScanLink = true,
  backLabel = "Findings",
  emptyDetail,
  emptyListText,
}: {
  findings: FindingRecord[];
  selectedId?: string;
  hrefFor: (key: string) => string;
  listHref: string;
  groupBy: GroupBy;
  searchPlaceholder?: string;
  initialOpenClosed?: OpenClosed;
  onSaved: (finding: FindingRecord) => Promise<void>;
  /** Fetch a selected finding that is not in `findings` (older scans). */
  loadMissing?: boolean;
  showScanLink?: boolean;
  backLabel?: string;
  /** Detail pane when there are no findings at all (e.g. a clean scan). */
  emptyDetail?: ReactNode;
  emptyListText?: string;
}) {
  const narrow = useNarrow();
  const [query, setQuery] = useState("");
  const [severity, setSeverity] = useState("all");
  const [openClosed, setOpenClosed] = useState<OpenClosed>(initialOpenClosed);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [historical, setHistorical] = useState<FindingRecord>();
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const fromList = findings.find((finding) => finding.key === selectedId);
  const hasListedFinding = !!fromList;
  useEffect(() => {
    setHistorical(undefined);
    setDetailError(null);
    setDetailLoading(false);
    if (!selectedId || hasListedFinding || !loadMissing) return;
    const controller = new AbortController();
    setDetailLoading(true);
    void request<{ finding: FindingRecord }>(
      `/findings/${encodeURIComponent(selectedId)}`,
      { signal: controller.signal },
    )
      .then((result) => {
        if (!controller.signal.aborted) setHistorical(result.finding);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setDetailError(errorMessage(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
    return () => controller.abort();
  }, [selectedId, hasListedFinding, loadMissing]);
  const selected =
    fromList || (historical?.key === selectedId ? historical : undefined);

  const groups = useMemo<Group[]>(() => {
    const needle = query.trim().toLowerCase();
    const byKey = new Map<string, Group>();
    for (const finding of findings) {
      if (severity !== "all" && finding.severity !== severity) continue;
      const closed = finding.status !== "open";
      if (openClosed === "open" && closed) continue;
      if (openClosed === "closed" && !closed) continue;
      if (
        needle &&
        !`${finding.title} ${finding.file} ${finding.project} ${finding.cwe}`
          .toLowerCase()
          .includes(needle)
      )
        continue;
      const key = groupBy === "project" ? finding.root : finding.severity;
      const group = byKey.get(key) ?? {
        key,
        label:
          groupBy === "project"
            ? finding.project
            : finding.severity.charAt(0).toUpperCase() +
              finding.severity.slice(1),
        root: finding.root,
        rows: [],
      };
      group.rows.push(finding);
      byKey.set(key, group);
    }
    const list = [...byKey.values()];
    for (const group of list)
      group.rows.sort((a, b) => rank(a.severity) - rank(b.severity));
    return list.sort(
      (a, b) =>
        rank(a.rows[0]!.severity) - rank(b.rows[0]!.severity) ||
        b.rows.length - a.rows.length ||
        a.label.localeCompare(b.label),
    );
  }, [findings, severity, openClosed, query, groupBy]);

  const isCollapsed = (group: Group) =>
    !!collapsed[group.key] && !group.rows.some((row) => row.key === selectedId);
  const visibleRows = groups.flatMap((group) =>
    isCollapsed(group) ? [] : group.rows,
  );
  const visibleCount = groups.reduce((sum, g) => sum + g.rows.length, 0);
  const firstKey = visibleRows[0]?.key;

  // Auto-select the first visible finding on wide screens.
  useEffect(() => {
    if (!selectedId && !narrow && firstKey) replaceRoute(hrefFor(firstKey));
    // hrefFor is recreated by callers each render; the route is what matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, narrow, firstKey]);

  // Keep the selected row in view.
  useEffect(() => {
    if (!selectedId) return;
    listRef.current
      ?.querySelector(`[data-key="${CSS.escape(selectedId)}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [selectedId]);

  // ↑/↓ moves the selection, `/` focuses search.
  const keyState = useRef({ visibleRows, selectedId, hrefFor });
  keyState.current = { visibleRows, selectedId, hrefFor };
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      const typing =
        !!target?.closest(
          "input, textarea, select, [contenteditable], [role='listbox'], [role='option'], [role='combobox'], [role='dialog']",
        ) || !!target?.isContentEditable;
      if (event.key === "/" && !typing) {
        event.preventDefault();
        searchRef.current?.focus();
        return;
      }
      if (typing || (event.key !== "ArrowDown" && event.key !== "ArrowUp"))
        return;
      const {
        visibleRows: rows,
        selectedId: current,
        hrefFor: href,
      } = keyState.current;
      if (!rows.length) return;
      event.preventDefault();
      const index = rows.findIndex((row) => row.key === current);
      const next =
        index === -1
          ? 0
          : Math.min(
              rows.length - 1,
              Math.max(0, index + (event.key === "ArrowDown" ? 1 : -1)),
            );
      replaceRoute(href(rows[next]!.key));
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  let detail: ReactNode;
  if (detailLoading) detail = <Loading />;
  else if (selectedId && selected)
    detail = (
      <FindingDetail
        key={selected.key}
        finding={selected}
        showScanLink={showScanLink}
        onSaved={async (finding) => {
          setHistorical(finding);
          await onSaved(finding);
        }}
      />
    );
  else if (selectedId)
    detail = (
      <div className="inbox-detail-empty">
        <ErrorBanner message={detailError} />
        <EmptyState title="Finding not found">
          <a href={listHref}>Back to the list.</a>
        </EmptyState>
      </div>
    );
  else if (!findings.length && emptyDetail) detail = emptyDetail;
  else
    detail = (
      <div className="inbox-detail-empty">
        <EmptyState title="No finding selected">
          Pick a finding from the list to see its details.
        </EmptyState>
      </div>
    );

  return (
    <div
      className="findings-inbox"
      data-selected={selectedId ? "true" : "false"}
    >
      <aside className="inbox-list" aria-label="Findings">
        <label className="inbox-search">
          <Search size={14} />
          <Input
            ref={searchRef}
            type="search"
            aria-label={searchPlaceholder}
            placeholder={searchPlaceholder}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") event.currentTarget.blur();
            }}
          />
          {!query && <kbd aria-hidden="true">/</kbd>}
        </label>
        <div className="inbox-filters">
          <div className="inbox-tabs" role="group" aria-label="Status">
            {(["open", "closed", "all"] as const).map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={openClosed === value}
                onClick={() => setOpenClosed(value)}
              >
                {value === "open"
                  ? "Open"
                  : value === "closed"
                    ? "Closed"
                    : "All"}
              </button>
            ))}
          </div>
          <Select
            className="inbox-severity"
            aria-label="Filter severity"
            value={severity}
            onValueChange={setSeverity}
            options={[
              { value: "all", label: "Severity" },
              { value: "critical", label: "Critical" },
              { value: "high", label: "High" },
              { value: "medium", label: "Medium" },
              { value: "low", label: "Low" },
            ]}
          />
        </div>
        <div className="inbox-groups" ref={listRef}>
          {findings.length === 0 && emptyListText ? (
            <p className="inbox-empty-text">{emptyListText}</p>
          ) : groups.length === 0 ? (
            <EmptyState title="No matching findings">
              {openClosed === "open" && !query && severity === "all"
                ? "Nothing is open. Resolved and dismissed findings are under Closed."
                : "Try another search or filter."}
            </EmptyState>
          ) : (
            groups.map((group) => {
              const closed = isCollapsed(group);
              return (
                <div
                  key={group.key}
                  className="inbox-group"
                  role="group"
                  aria-label={group.label}
                >
                  <button
                    type="button"
                    className="inbox-group-header"
                    aria-expanded={!closed}
                    onClick={() =>
                      setCollapsed((prev) => ({
                        ...prev,
                        [group.key]: !closed,
                      }))
                    }
                  >
                    <span className="inbox-group-label">
                      {closed ? (
                        <ChevronRight size={12} />
                      ) : (
                        <ChevronDown size={12} />
                      )}
                      {groupBy === "severity" ? (
                        <Severity value={group.key} />
                      ) : (
                        <Tooltip content={group.root || group.label}>
                          <span className="inbox-project-chip">
                            <span className="truncate">{group.label}</span>
                          </span>
                        </Tooltip>
                      )}
                    </span>
                    <span className="inbox-count">{group.rows.length}</span>
                  </button>
                  {!closed &&
                    group.rows.map((finding) => (
                      <FindingRow
                        key={finding.key}
                        finding={finding}
                        href={hrefFor(finding.key)}
                        showSeverity={groupBy !== "severity"}
                        selected={finding.key === selectedId}
                      />
                    ))}
                </div>
              );
            })
          )}
        </div>
        <footer className="inbox-footer">
          <span className="inbox-keys">
            <kbd>↑</kbd>
            <kbd>↓</kbd>
            to navigate
          </span>
          <span>
            {visibleCount} · Grouped by {groupBy}
          </span>
        </footer>
      </aside>
      <div className="inbox-detail">
        {narrow && selectedId && (
          <div className="inbox-back">
            <a
              className="icon-button"
              aria-label={`Back to ${backLabel}`}
              href={listHref}
            >
              <ArrowLeft size={15} />
            </a>
            <span>{backLabel}</span>
          </div>
        )}
        {detail}
      </div>
    </div>
  );
}

function StatusIcon({ status }: { status: string }) {
  if (status === "resolved")
    return <Check size={14} className="row-status row-resolved" />;
  if (status === "dismissed")
    return <CircleSlash size={14} className="row-status" />;
  return <Circle size={14} className="row-status" />;
}

function FindingRow({
  finding,
  selected,
  href,
  showSeverity,
}: {
  finding: FindingRecord;
  selected: boolean;
  href: string;
  showSeverity: boolean;
}) {
  return (
    <a
      href={href}
      className="inbox-row"
      data-key={finding.key}
      data-closed={finding.status !== "open" ? "true" : undefined}
      aria-current={selected ? "true" : undefined}
    >
      <span className="inbox-row-icon">
        <StatusIcon status={finding.status} />
      </span>
      <span className="inbox-row-body">
        <span className="inbox-row-title">{finding.title}</span>
        <span className="inbox-row-meta">
          <span className="truncate">
            {finding.file}:{finding.line} · {finding.cwe}
          </span>
          {showSeverity && <Severity value={finding.severity} />}
          {finding.validation === "rejected" && (
            <span className="validation">Rejected</span>
          )}
        </span>
      </span>
    </a>
  );
}

export function FindingDetail({
  finding,
  onSaved,
  showScanLink = true,
}: {
  finding: FindingRecord;
  onSaved: (finding: FindingRecord) => Promise<void>;
  showScanLink?: boolean;
}) {
  const action = useRequestGate();
  const revision = useRef(0);
  const dirty = useRef(false);
  const [status, setStatus] = useState<FindingStatus>(finding.status);
  const [note, setNote] = useState(finding.note);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    // Polling follows external triage changes only while this form is untouched.
    if (!dirty.current) {
      setStatus(finding.status);
      setNote(finding.note);
    }
  }, [finding.status, finding.note]);
  const changed = status !== finding.status || note !== finding.note;
  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (note.length > 10000) {
      setError("Notes must be 10,000 characters or fewer.");
      return;
    }
    const signal = action.begin();
    if (!signal) return;
    const submittedRevision = revision.current;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const result = await request<{ finding: FindingRecord }>(
        `/findings/${encodeURIComponent(finding.key)}`,
        {
          method: "PATCH",
          signal,
          body: JSON.stringify({ status, note }),
        },
      );
      if (!action.owns(signal)) return;
      await onSaved(result.finding);
      if (action.owns(signal) && revision.current === submittedRevision) {
        dirty.current = false;
        setStatus(result.finding.status);
        setNote(result.finding.note);
        setSaved(true);
      }
    } catch (failure) {
      if (action.owns(signal)) setError(errorMessage(failure));
    } finally {
      if (action.owns(signal)) setBusy(false);
      action.finish(signal);
    }
  }
  const found = foundAt(finding);
  const location = `${finding.file}:${finding.line}${finding.endLine !== finding.line ? `–${finding.endLine}` : ""}`;
  return (
    <form noValidate className="inbox-detail-scroll" onSubmit={save}>
      <div className="finding-detail">
        <header className="fd-header">
          <h2 className="fd-title">{finding.title}</h2>
          <div className="fd-actions">
            {changed && (
              <Button className="button primary" type="submit" disabled={busy}>
                {busy ? "Saving…" : "Save changes"}
              </Button>
            )}
            <CopyForAgent
              markdown={() => findingMarkdown(finding, finding.root)}
            />
            {showScanLink && (
              <a
                href={`#/scans/${encodeURIComponent(finding.scanId)}`}
                className="button"
              >
                View scan
                <ArrowUpRight size={13} />
              </a>
            )}
          </div>
        </header>
        <div className="fd-chips">
          <Severity value={finding.severity} />
          <span className="fd-chip">{finding.cwe}</span>
          <span
            className="fd-chip fd-mono"
            title={`${finding.root}/${location}`}
          >
            <span className="truncate">{location}</span>
          </span>
        </div>
        <div className="fd-meta">
          <div className="fd-meta-item">
            <span className="fd-meta-label">Status</span>
            <Select
              className="fd-status"
              value={status}
              aria-label="Finding status"
              onValueChange={(value) => {
                revision.current++;
                dirty.current = true;
                setStatus(value as FindingStatus);
                setSaved(false);
              }}
              options={[
                { value: "open", label: "Open" },
                { value: "resolved", label: "Resolved" },
                { value: "dismissed", label: "Dismissed" },
              ]}
            />
          </div>
          <div className="fd-meta-item">
            <span className="fd-meta-label">Severity</span>
            <Severity value={finding.severity} />
          </div>
          <div className="fd-meta-item">
            <span className="fd-meta-label">Project</span>
            <Tooltip content={finding.root || finding.project}>
              <span className="fd-meta-value truncate">{finding.project}</span>
            </Tooltip>
          </div>
          <div className="fd-meta-item">
            <span className="fd-meta-label">Validation</span>
            <span className={`validation validation-${finding.validation}`}>
              {finding.validation}
            </span>
          </div>
          {found && <span className="fd-found">Found {found}</span>}
        </div>
        <section className="fd-section">
          <h3>Description</h3>
          <p className="fd-prose">{finding.summary}</p>
        </section>
        <section className="fd-section">
          <h3>Impact</h3>
          <p className="fd-prose">{finding.impact}</p>
        </section>
        <section className="fd-section">
          <h3>Evidence</h3>
          <pre className="code-block fd-code">
            <code>{finding.evidence}</code>
          </pre>
        </section>
        <section className="fd-section">
          <h3>Data flow</h3>
          <p className="fd-prose">{finding.dataFlow}</p>
        </section>
        <section className="fd-section">
          <h3>Recommended fix</h3>
          <p className="fd-prose">{finding.remediation}</p>
        </section>
        <section className="fd-section">
          <h3>Validation reasoning</h3>
          <p className="fd-prose">
            {finding.validationReason ||
              "This finding has not yet been reviewed."}
          </p>
        </section>
        <section className="fd-section fd-triage">
          <h3>Notes</h3>
          <Textarea
            value={note}
            maxLength={10000}
            rows={5}
            aria-label="Notes"
            onChange={(event) => {
              revision.current++;
              dirty.current = true;
              setNote(event.target.value);
              setSaved(false);
            }}
            placeholder="Add context or a follow-up…"
          />
          <ErrorBanner message={error} />
          <div className="fd-triage-actions">
            <Button className="button primary" type="submit" disabled={busy}>
              {busy ? "Saving…" : "Save changes"}
            </Button>
            {saved && (
              <span className="success-text" role="status">
                Changes saved.
              </span>
            )}
          </div>
          <p className="helper">
            Status and notes apply to matching findings in future scans.
            Resolved is a manual assessment.
          </p>
        </section>
      </div>
    </form>
  );
}
