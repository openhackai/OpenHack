import { Button } from "./components/controls/Button";
import { useCallback, useEffect, useState } from "react";
import {
  ChevronRight,
  Monitor,
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
  IconNavScans,
  IconNavSettings,
  IconNavShield,
} from "./components/nav-icons";
import { ScansPage } from "./pages/ScansPage";
import { FindingsPage } from "./pages/FindingsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { useRequestGate } from "./use-request-gate";

type Page = "scans" | "findings" | "settings";
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
    page: (["scans", "findings", "settings"].includes(page ?? "")
      ? page
      : "scans") as Page,
    id: decode(id),
    sub: decode(sub),
  };
}
export function routeHash(page: Page, id?: string, sub?: string) {
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
  const pageTitle = route.page.charAt(0).toUpperCase() + route.page.slice(1);
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
          <a href="#/scans" className="brand" aria-label="OpenHack scans">
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
              { page: "scans", label: "Scans", Icon: IconNavScans },
              { page: "findings", label: "Findings", Icon: IconNavShield },
              { page: "settings", label: "Settings", Icon: IconNavSettings },
            ] as const
          ).map(({ page, label, Icon }) => (
            <a
              key={page}
              href={`#/${page}`}
              aria-label={label}
              aria-current={route.page === page ? "page" : undefined}
            >
              <Icon />
              <span>{label}</span>
            </a>
          ))}
        </nav>
        <div className="sidebar-footer">
          <span className="local-label">
            <Monitor size={14} />
            <span>Local workspace</span>
          </span>
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
          <span>{pageTitle}</span>
          <span className="local-status">
            <span /> On this machine
          </span>
        </header>
        <ErrorBanner message={error} />
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
            findings={findings}
            loading={loading}
            selectedId={route.id}
            refresh={refresh}
          />
        )}
        {route.page === "settings" && <SettingsPage />}
      </main>
    </div>
  );
}
