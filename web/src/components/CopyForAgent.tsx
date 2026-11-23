import { useEffect, useState } from "react";
import { Button } from "./controls/Button";
import { Check, Copy } from "./nucleo-icons";

/** "Copy for Agent": copies markdown a coding agent can act on. */
export function CopyForAgent({
  markdown,
  label = "Copy for Agent",
  disabled,
  className = "",
}: {
  markdown: () => string;
  label?: string;
  disabled?: boolean;
  className?: string;
}) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const timer = window.setTimeout(() => setState("idle"), 2000);
    return () => window.clearTimeout(timer);
  }, [state]);
  return (
    <Button
      className={`button copy-agent ${className}`}
      disabled={disabled}
      onClick={() =>
        void navigator.clipboard
          .writeText(markdown())
          .then(() => setState("copied"))
          .catch(() => setState("failed"))
      }
    >
      {state === "copied" ? <Check size={13} /> : <Copy size={13} />}
      <span aria-live="polite">
        {state === "copied"
          ? "Copied"
          : state === "failed"
            ? "Copy failed"
            : label}
      </span>
    </Button>
  );
}
