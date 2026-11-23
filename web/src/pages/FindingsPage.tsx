import { Button } from "../components/controls/Button";
import { Checkbox } from "../components/controls/Checkbox";
import { Select } from "../components/controls/Select";
import { useState } from "react";
import { RefreshCw } from "../components/nucleo-icons";
import type { FindingRecord } from "../../../src/shared/contracts";
import { routeHash } from "../App";
import { EmptyState, Loading } from "../components/ui";
import { CopyForAgent } from "../components/CopyForAgent";
import { findingsMarkdown } from "../agent-markdown";
import { FindingsInbox } from "../components/FindingsInbox";

export function FindingsPage({
  findings,
  loading,
  selectedId,
  refresh,
}: {
  findings: FindingRecord[];
  loading: boolean;
  selectedId?: string;
  refresh: () => Promise<void>;
}) {
  const [project, setProject] = useState("all");
  const [includeRejected, setIncludeRejected] = useState(false);
  const projects = [
    ...new Set(findings.map((finding) => finding.project)),
  ].sort((a, b) => a.localeCompare(b));
  const scoped = findings.filter(
    (finding) =>
      (includeRejected || finding.validation !== "rejected") &&
      (project === "all" || finding.project === project),
  );
  const open = scoped.filter((finding) => finding.status === "open").length;
  return (
    <section className="page">
      <div className="pagebar">
        <div className="page-title">
          <h1>Findings</h1>
          <Select
            className="bar-filter"
            prefix="Project"
            aria-label="Filter by project"
            value={project}
            onValueChange={setProject}
            options={[
              { value: "all", label: "All" },
              ...projects.map((name) => ({ value: name, label: name })),
            ]}
          />
          <span className="muted">{open} open</span>
        </div>
        <div className="actions">
          <label className="checkbox-label">
            <Checkbox
              checked={includeRejected}
              onCheckedChange={setIncludeRejected}
              aria-label="Include rejected findings"
            />
            Include rejected
          </label>
          <CopyForAgent
            disabled={!open}
            label={open > 1 ? `Copy ${open} open for Agent` : "Copy for Agent"}
            markdown={() =>
              findingsMarkdown(
                scoped.filter((finding) => finding.status === "open"),
                project === "all" ? {} : { project, root: scoped[0]?.root },
              )
            }
          />
          <Button
            className="button icon-button"
            aria-label="Refresh findings"
            onClick={() => void refresh()}
          >
            <RefreshCw size={14} />
          </Button>
        </div>
      </div>
      {loading ? (
        <Loading />
      ) : findings.length === 0 && !selectedId ? (
        <EmptyState title="No findings yet">
          Findings from your latest project scans will appear here.
        </EmptyState>
      ) : (
        <FindingsInbox
          findings={scoped}
          selectedId={selectedId}
          hrefFor={(key) => routeHash("findings", key)}
          listHref="#/findings"
          groupBy="project"
          loadMissing
          onSaved={() => refresh()}
        />
      )}
    </section>
  );
}
