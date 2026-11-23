import { useState } from "react";
import { Button } from "../components/controls/Button";
import { Tooltip } from "../components/controls/Tooltip";
import { ChevronRight, Play, RefreshCw } from "../components/nucleo-icons";
import { ProviderIcon } from "../components/provider-icons";
import { Onboarding } from "../components/Onboarding";
import { NewScanDialog } from "../components/NewScanDialog";
import type { FindingRecord, Report } from "../../../src/shared/contracts";
import { navigate, routeHash } from "../App";
import {
  Loading,
  Severity,
  Status,
  duration,
  relative,
} from "../components/ui";
import "./dashboard.css";

const SEVERITIES = ["critical", "high", "medium", "low"] as const;
type Sev = (typeof SEVERITIES)[number];
const rank = (s: string) => {
  const i = SEVERITIES.indexOf(s as Sev);
  return i === -1 ? 4 : i;
};
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

function Column({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="posture-column" role="group" aria-label={label}>
      <span className="posture-label">{label}</span>
      {children}
    </div>
  );
}

function Card({
  title,
  href,
  linkLabel = "View all",
  className = "",
  children,
}: {
  title: string;
  href?: string;
  linkLabel?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <section className={`dash-card ${className}`} aria-label={title}>
      <header>
        <h2>{title}</h2>
        {href && (
          <a href={href} className="dash-link">
            {linkLabel}
            <ChevronRight size={10} />
          </a>
        )}
      </header>
      {children}
    </section>
  );
}

/** Local workspace overview: posture numbers, severity mix, projects, recent scans. */
export function DashboardPage({
  scans,
  findings,
  loading,
  refresh,
}: {
  scans: Report[];
  findings: FindingRecord[];
  loading: boolean;
  refresh: () => Promise<void>;
}) {
  const [newScan, setNewScan] = useState(false);
  const [demoOnboarding, setDemoOnboarding] = useState(
    () =>
      import.meta.env.DEV &&
      new URLSearchParams(window.location.search).get("demo") === "onboarding",
  );
  function setDemo(active: boolean) {
    if (!import.meta.env.DEV) return;
    const url = new URL(window.location.href);
    if (active) url.searchParams.set("demo", "onboarding");
    else url.searchParams.delete("demo");
    window.history.replaceState(null, "", url);
    setDemoOnboarding(active);
  }
  const showOnboarding =
    scans.length === 0 || (import.meta.env.DEV && demoOnboarding);
  const runningIds = new Set(
    scans.filter((s) => s.state === "running").map((s) => s.id),
  );
  const live = findings.filter(
    (f) => f.validation !== "rejected" && !runningIds.has(f.scanId),
  );
  const open = live.filter((f) => f.status === "open");
  const closed = live.length - open.length;
  const bySeverity = Object.fromEntries(
    SEVERITIES.map((s) => [s, open.filter((f) => f.severity === s).length]),
  ) as Record<Sev, number>;
  const urgent = bySeverity.critical + bySeverity.high;
  const validated = open.filter((f) => f.validation === "validated").length;
  const running = scans.filter((s) => s.state === "running");
  const latest = scans[0];

  const projects = [...new Set(scans.map((s) => s.project))].map((name) => {
    const projectScans = scans.filter((s) => s.project === name);
    const rows = open.filter((f) => f.project === name);
    return {
      name,
      root: projectScans[0]!.root,
      lastScan: projectScans[0]!,
      scans: projectScans.length,
      open: rows.length,
      counts: SEVERITIES.map((s) => ({
        severity: s,
        count: rows.filter((f) => f.severity === s).length,
      })).filter((c) => c.count),
      urgent: rows.filter((f) => rank(f.severity) <= 1).length,
    };
  });
  projects.sort(
    (a, b) =>
      b.urgent - a.urgent || b.open - a.open || a.name.localeCompare(b.name),
  );
  const atRisk = projects.filter((p) => p.urgent > 0).length;
  const top = [...open]
    .sort((a, b) => rank(a.severity) - rank(b.severity))
    .slice(0, 6);
  const maxSeverity = Math.max(1, ...SEVERITIES.map((s) => bySeverity[s]));

  return (
    <section className="page">
      <div className="pagebar">
        <div className="page-title">
          <h1>Dashboard</h1>
          {!showOnboarding && (
            <span className="muted">
              {projects.length} project{projects.length === 1 ? "" : "s"} on this machine
            </span>
          )}
        </div>
        <div className="actions">
          {import.meta.env.DEV && !demoOnboarding && (
            <Button
              className="button"
              onClick={() => setDemo(true)}
            >
              Demo onboarding
            </Button>
          )}
          <Button
            className="button icon-button"
            aria-label="Refresh dashboard"
            onClick={() => void refresh()}
          >
            <RefreshCw size={14} />
          </Button>
          {!showOnboarding && (
            <Button className="button primary" onClick={() => setNewScan(true)}>
              <Play size={12} />
              Run scan
            </Button>
          )}
        </div>
      </div>
      {loading ? (
        <Loading />
      ) : showOnboarding ? (
        <div className="dash-scroll">
          <Onboarding
            key={import.meta.env.DEV && demoOnboarding ? "demo" : "first-scan"}
            onStarted={(scan) => {
              if (import.meta.env.DEV && demoOnboarding) {
                setDemo(false);
                void refresh();
                navigate("scans");
                return;
              }
              navigate("scans", scan.id);
              void refresh();
            }}
          />
        </div>
      ) : (
        <div className="dash-scroll">
          <section className="posture" aria-label="Security posture">
            <Column label="Open findings">
              <span className="posture-value">{open.length}</span>
              <span className="posture-sub">
                {open.length
                  ? `${validated} validated · ${closed} closed`
                  : closed
                    ? `${closed} resolved or dismissed`
                    : "Nothing found yet"}
              </span>
            </Column>
            <Column label="Critical and high">
              <span className="posture-row">
                <span className="posture-value">{urgent}</span>
                <span className="posture-dots">
                  <i className="severity-square critical" />
                  {bySeverity.critical}
                  <i className="severity-square high" />
                  {bySeverity.high}
                </span>
              </span>
              <span className={`posture-sub ${urgent ? "worse" : ""}`}>
                {urgent ? "Fix these first" : "None open"}
              </span>
            </Column>
            <Column label="Projects at risk">
              <span className="posture-row">
                <span className="posture-value">{atRisk}</span>
                <span className="posture-of">of {projects.length}</span>
              </span>
              <span className="posture-sub">
                {atRisk
                  ? "with a critical or high finding open"
                  : "No critical or high finding open"}
              </span>
            </Column>
            <Column label="Scans">
              <span className="posture-row">
                <span className="posture-value">{scans.length}</span>
                {running.length > 0 && <Status value="running" />}
              </span>
              <span className="posture-sub">
                {latest
                  ? `Last ran ${relative(latest.startedAt)} · ${latest.project}`
                  : "No scans yet"}
              </span>
            </Column>
          </section>

          <div className="dash-row">
            <Card
              title="Open by severity"
              href="#/findings"
              className="dash-narrow"
            >
              <ul className="severity-bars">
                {SEVERITIES.map((s) => (
                  <li key={s}>
                    <span className="severity-bar-label">{cap(s)}</span>
                    <span className="severity-bar-track">
                      <span
                        className={`severity-bar-fill ${s}`}
                        style={{
                          width: `${(bySeverity[s] / maxSeverity) * 100}%`,
                        }}
                      />
                    </span>
                    <span className="severity-bar-count">{bySeverity[s]}</span>
                  </li>
                ))}
              </ul>
            </Card>
            <Card title="Projects" href="#/scans">
              {projects.length ? (
                <ul className="dash-list">
                  {projects.slice(0, 6).map((p) => (
                    <li key={p.name}>
                      <a
                        href={routeHash("scans", p.lastScan.id)}
                        className="dash-item"
                      >
                        <span className="dash-item-main">
                          <span className="truncate">{p.name}</span>
                          <small className="truncate" title={p.root}>
                            {p.root}
                          </small>
                        </span>
                        <span className="severity-summary">
                          {p.counts.length ? (
                            p.counts.slice(0, 2).map(({ severity, count }) => (
                              <span key={severity}>
                                <i className={`severity-square ${severity}`} />
                                {count} {cap(severity)}
                              </span>
                            ))
                          ) : (
                            <span className="muted">No open findings</span>
                          )}
                        </span>
                        <span className="dash-item-time">
                          {relative(p.lastScan.startedAt)}
                        </span>
                      </a>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="dash-empty">Run a scan to see projects here.</p>
              )}
            </Card>
          </div>

          <div className="dash-row">
            <Card title="Fix first" href="#/findings" className="dash-half">
              {top.length ? (
                <ul className="dash-list">
                  {top.map((f) => (
                    <li key={f.key}>
                      <a
                        href={routeHash("findings", f.key)}
                        className="dash-item"
                      >
                        <Severity value={f.severity} />
                        <span className="dash-item-main">
                          <span className="truncate">{f.title}</span>
                          <small className="truncate">
                            {f.project} · {f.file}:{f.line}
                          </small>
                        </span>
                      </a>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="dash-empty">No open findings. Nice.</p>
              )}
            </Card>
            <Card title="Recent scans" href="#/scans" className="dash-half">
              {scans.length ? (
                <ul className="dash-list">
                  {scans.slice(0, 6).map((scan) => {
                    const pending = scan.state === "running";
                    const count = scan.findings.filter(
                      (f) => f.validation !== "rejected",
                    ).length;
                    return (
                      <li key={scan.id}>
                        <a
                          href={routeHash("scans", scan.id)}
                          className="dash-item"
                        >
                          <Tooltip content={scan.provider ?? "openai"}>
                            <span className="dash-provider" tabIndex={-1}>
                              <ProviderIcon
                                provider={scan.provider ?? "openai"}
                                size={13}
                                aria-hidden="true"
                              />
                            </span>
                          </Tooltip>
                          <span className="dash-item-main">
                            <span className="truncate">{scan.project}</span>
                            <small className="truncate">
                              {scan.model} ·{" "}
                              {duration(scan.startedAt, scan.finishedAt)}
                            </small>
                          </span>
                          <Status value={scan.state} />
                          <span className="dash-item-count">
                            {pending
                              ? "Pending"
                              : count
                                ? `${count} finding${count === 1 ? "" : "s"}`
                                : "No findings"}
                          </span>
                          <span className="dash-item-time">
                            {relative(scan.startedAt)}
                          </span>
                        </a>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <p className="dash-empty">
                  No scans yet.{" "}
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => setNewScan(true)}
                  >
                    Run your first scan
                  </button>
                </p>
              )}
            </Card>
          </div>
        </div>
      )}
      {newScan && (
        <NewScanDialog
          onClose={() => setNewScan(false)}
          onStarted={(scan) => {
            setNewScan(false);
            navigate("scans", scan.id);
            void refresh();
          }}
        />
      )}
    </section>
  );
}
