import { Button } from "./components/controls/Button";
import { useCallback, useEffect, useState } from "react";
import {
  ChevronRight,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Sun,
} from "./components/nucleo-icons";
import type {
  FindingRecord,
  FindingsResponse,
  Report,
  ScansResponse,
} from "../../src/shared/contracts";
import { request, errorMessage } from "./api";
import { GroundMark, ErrorBanner } from "./components/ui";
import {
  IconNavOverview,
  IconNavScans,
  IconNavSettings,
  IconNavShield,
} from "./components/nav-icons";
import { ScansPage } from "./pages/ScansPage";
import { FindingsPage } from "./pages/FindingsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { ProPage } from "./pages/ProPage";
import { ProLogo } from "./components/ProLogo";
import { useRequestGate } from "./use-request-gate";

type Page = "dashboard" | "scans" | "findings" | "pro" | "settings";
function decode(value?: string) {
  if (!value) return undefined;
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}
function readRoute() {
  const [page, id, sub] = window.location.hash.slice(2).split("/");
  return {
    page: (["scans", "findings", "pro", "settings"].includes(page ?? "")
      ? page
      : "dashboard") as Page,
    id: decode(id),
    sub: decode(sub),
  };
}
export function routeHash(page: Page, id?: string, sub?: string) {
  if (page === "dashboard") return "#/";
  return `#/${page}${id ? `/${encodeURIComponent(id)}` : ""}${id && sub ? `/${encodeURIComponent(sub)}` : ""}`;
}
export function navigate(page: Page, id?: string, sub?: string) {
  window.location.hash = routeHash(page, id, sub).slice(1);
}
/** Changes the route without adding a history entry (selection, auto-select). */
export function replaceRoute(hash: string) {
  if (window.location.hash === hash) return;
  window.history.replaceState(null, "", hash);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}

export function App() {
  const refreshGate = useRequestGate();
  const [route, setRoute] = useState(readRoute);
  const [scans, setScans] = useState<Report[]>([]);
  const [findings, setFindings] = useState<FindingRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  // The council can still reject a running scan's candidates; show settled results only.
  const runningIds = new Set(
    scans.filter((scan) => scan.state === "running").map((scan) => scan.id),
  );
  const settledFindings = findings.filter(
    (finding) => !runningIds.has(finding.scanId),
  );
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem("openhack-theme") === "light"
        ? "light"
        : "dark";
    } catch {
      return "dark";
    }
  });
  const refresh = useCallback(async () => {
    const signal = refreshGate.begin(true)!;
    try {
      const [scanResult, findingResult] = await Promise.all([
        request<ScansResponse>("/scans", { signal }),
        request<FindingsResponse>("/findings", { signal }),
      ]);
      if (!refreshGate.owns(signal)) return;
      setScans(scanResult.scans);
      setFindings(findingResult.findings);
      setError(null);
    } catch (failure) {
      if (refreshGate.owns(signal)) setError(errorMessage(failure));
    } finally {
      if (refreshGate.owns(signal)) setLoading(false);
      refreshGate.finish(signal);
    }
  }, [refreshGate]);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh();
      if (active) timer = setTimeout(poll, 3000);
    };
    void poll();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [refresh]);
  useEffect(() => {
    const change = () => setRoute(readRoute());
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("openhack-theme", theme);
    } catch {}
  }, [theme]);
  const pageTitle =
    route.page === "pro"
      ? "OpenHack Pro"
      : route.page.charAt(0).toUpperCase() + route.page.slice(1);
  return (
    <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`}>
      <a
        href="#main-content"
        className="skip-link"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        Skip to content
      </a>
      <aside className="sidebar">
        <div className="sidebar-header">
          <a href="#/" className="brand" aria-label="OpenHack dashboard">
            <GroundMark />
            <span>OpenHack</span>
          </a>
          <Button
            className="icon-button collapse-toggle"
            onClick={() => setCollapsed(!collapsed)}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!collapsed}
          >
            {collapsed ? (
              <PanelLeftOpen size={15} />
            ) : (
              <PanelLeftClose size={15} />
            )}
          </Button>
        </div>
        <div className="nav-section-label">Workspace</div>
        <nav aria-label="Main navigation">
          {(
            [
              { page: "dashboard", label: "Dashboard", Icon: IconNavOverview },
              { page: "scans", label: "Scans", Icon: IconNavScans },
              { page: "findings", label: "Findings", Icon: IconNavShield },
              { page: "pro", label: "OpenHack Pro", Icon: ProLogo },
              { page: "settings", label: "Settings", Icon: IconNavSettings },
            ] as const
          ).map(({ page, label, Icon }) => (
            <a
              key={page}
              href={page === "dashboard" ? "#/" : `#/${page}`}
              aria-label={label}
              aria-current={route.page === page ? "page" : undefined}
            >
              <Icon />
              <span>{label}</span>
            </a>
          ))}
        </nav>
        <div className="sidebar-footer">
          <Button
            className="icon-button"
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
          </Button>
        </div>
      </aside>
      <main id="main-content" className="main" tabIndex={-1}>
        <header className="topbar">
          <span>Local workspace</span>
          <ChevronRight size={12} />
          {route.id ? (
            <>
              <a className="crumb-link" href={`#/${route.page}`}>
                {pageTitle}
              </a>
              <ChevronRight size={12} />
              <span className="crumb-current">
                {route.page === "scans"
                  ? (scans.find((scan) => scan.id === route.id)?.project ??
                    "Scan")
                  : (findings.find((finding) => finding.key === route.id)
                      ?.project ?? "Finding")}
              </span>
            </>
          ) : (
            <span className="crumb-current">{pageTitle}</span>
          )}
        </header>
        <ErrorBanner message={error} />
        {route.page === "dashboard" && (
          <DashboardPage
            scans={scans}
            findings={settledFindings}
            loading={loading}
            refresh={refresh}
          />
        )}
        {route.page === "scans" && (
          <ScansPage
            scans={scans}
            loading={loading}
            selectedId={route.id}
            findingId={route.sub}
            refresh={refresh}
          />
        )}
        {route.page === "findings" && (
          <FindingsPage
            findings={settledFindings}
            loading={loading}
            selectedId={route.id}
            refresh={refresh}
          />
        )}
        {route.page === "settings" && <SettingsPage />}
        {route.page === "pro" && <ProPage />}
      </main>
    </div>
  );
}
