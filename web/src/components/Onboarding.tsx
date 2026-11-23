import { useRef, useState } from "react";
import { Button, Dialog, DialogDescription, DialogTitle } from "./controls";
import {
  IconNavOverview,
  IconNavScans,
  IconNavSettings,
  IconNavShield,
} from "./nav-icons";
import { Copy, X } from "./nucleo-icons";
import { GroundMark } from "./ui";

const KEY = "openhack-onboarded";

export function shouldOnboard() {
  try {
    return localStorage.getItem(KEY) !== "1";
  } catch {
    return false;
  }
}

const STEPS = [
  {
    Icon: IconNavOverview,
    title: "Dashboard",
    text: "Open findings, critical issues and recent scans at a glance.",
  },
  {
    Icon: IconNavScans,
    title: "Scans",
    text: "Point OpenHack at a folder. Agents map, hunt, validate and judge.",
  },
  {
    Icon: IconNavShield,
    title: "Findings",
    text: "Triage by project. Mark resolved or dismissed, add notes.",
  },
  {
    Icon: Copy,
    title: "Copy for Agent",
    text: "Hand a finding, or a whole scan, to your coding agent to fix.",
  },
  {
    Icon: IconNavSettings,
    title: "Settings",
    text: "Choose OpenAI, Anthropic, Codex or Claude Code. Keys stay local.",
  },
];

/** First-run welcome: one short screen, shown once per browser. */
export function Onboarding({
  hasScans,
  onRunScan,
}: {
  hasScans: boolean;
  onRunScan: () => void;
}) {
  const [open, setOpen] = useState(shouldOnboard);
  const primary = useRef<HTMLButtonElement>(null);
  function finish() {
    try {
      localStorage.setItem(KEY, "1");
    } catch {}
    setOpen(false);
  }
  if (!open) return null;
  return (
    <Dialog
      open
      initialFocus={primary}
      onOpenChange={(next) => {
        if (!next) finish();
      }}
    >
      <div className="dialog-header">
        <div className="onboarding-title">
          <span className="onboarding-mark">
            <GroundMark />
          </span>
          <DialogTitle>Welcome to OpenHack</DialogTitle>
        </div>
        <Button
          className="icon-button"
          aria-label="Close welcome"
          onClick={finish}
        >
          <X size={15} />
        </Button>
      </div>
      <div className="dialog-body">
        <DialogDescription>
          Open-source security scanning that runs on your machine.
        </DialogDescription>
        <ul className="onboarding-list">
          {STEPS.map(({ Icon, title, text }) => (
            <li key={title}>
              <span className="onboarding-icon">
                <Icon size={14} />
              </span>
              <span>
                <strong>{title}</strong>
                <span>{text}</span>
              </span>
            </li>
          ))}
        </ul>
      </div>
      <div className="dialog-footer">
        {hasScans ? (
          <Button ref={primary} className="button primary" onClick={finish}>
            Got it
          </Button>
        ) : (
          <>
            <Button className="button" onClick={finish}>
              Skip
            </Button>
            <Button
              ref={primary}
              className="button primary"
              onClick={() => {
                finish();
                onRunScan();
              }}
            >
              Run your first scan
            </Button>
          </>
        )}
      </div>
    </Dialog>
  );
}
