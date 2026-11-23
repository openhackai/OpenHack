import { Input } from "./controls/Input";
import {
  Check,
  Circle,
  LoaderCircle,
  Minus,
  Search,
  ShieldCheck,
  X,
} from "./nucleo-icons";
import type { ReactNode } from "react";

export function GroundMark() {
  return (
    <svg
      viewBox="4.8 4.2 14.4 14.4"
      fill="currentColor"
      aria-hidden="true"
      className="ground-mark"
    >
      <rect x="11.3" y="4.7" width="1.4" height="6.9" rx=".15" />
      <rect x="5.85" y="11.4" width="12.3" height="1.35" rx=".15" />
      <rect x="7.55" y="14.1" width="8.9" height="1.3" rx=".15" />
      <rect x="9.25" y="16.8" width="5.5" height="1.35" rx=".15" />
    </svg>
  );
}

export function Status({ value }: { value: string }) {
  const Icon =
    value === "running"
      ? LoaderCircle
      : ["complete", "resolved"].includes(value)
        ? Check
        : value === "failed"
          ? X
          : ["cancelled", "dismissed"].includes(value)
            ? Minus
            : Circle;
  const label =
    (
      {
        complete: "Completed",
        running: "Scanning",
        partial: "Partial",
        open: "Open",
        cancelled: "Cancelled",
        failed: "Failed",
        resolved: "Resolved",
        dismissed: "Dismissed",
      } as Record<string, string>
    )[value] || value;
  return (
    <span className={`status status-${value}`}>
      <Icon size={13} className={value === "running" ? "spin" : undefined} />
      {label}
    </span>
  );
}

export function Severity({ value }: { value: string }) {
  return (
    <span className={`severity severity-${value}`}>
      {value.charAt(0).toUpperCase() + value.slice(1)}
    </span>
  );
}

export function SearchField({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}) {
  return (
    <label className="search">
      <Search size={14} />
      <Input
        type="search"
        aria-label={placeholder}
        placeholder={placeholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <ShieldCheck size={25} strokeWidth={1.3} />
      <h2>{title}</h2>
      <div className="empty-copy">{children}</div>
      {action}
    </div>
  );
}

export function ErrorBanner({
  message,
  id,
}: {
  message: string | null;
  id?: string;
}) {
  return message ? (
    <div id={id} className="error-banner" role="alert">
      {message}
    </div>
  ) : null;
}

export function Loading() {
  return (
    <div className="loading" role="status">
      <LoaderCircle size={16} className="spin" /> Loading…
    </div>
  );
}

export function dateTime(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "Unknown";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}
export function duration(start: string, end?: string): string {
  const seconds = Math.max(
    0,
    Math.floor(
      ((end ? new Date(end).getTime() : Date.now()) -
        new Date(start).getTime()) /
        1000,
    ),
  );
  if (!Number.isFinite(seconds)) return "Unknown";
  return seconds < 60
    ? `${seconds}s`
    : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

export function relative(value: string): string {
  const time = new Date(value).getTime();
  if (!Number.isFinite(time)) return "Unknown";
  const seconds = Math.max(0, Math.round((Date.now() - time) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(
    time,
  );
}
