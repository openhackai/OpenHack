import type { Finding } from "../../src/shared/contracts";

const ORDER = ["critical", "high", "medium", "low"];

/** Markdown a coding agent can act on, matching company/app "Copy for Agent". */
export function findingMarkdown(
  finding: Finding,
  root?: string,
  heading = "##",
) {
  const range =
    finding.endLine && finding.endLine !== finding.line
      ? `${finding.line}-${finding.endLine}`
      : `${finding.line}`;
  let md = `${heading} ${finding.title}\n\n`;
  md += `**Severity:** ${finding.severity.toUpperCase()}\n`;
  md += `**Category:** ${finding.cwe}\n`;
  md += `**File:** \`${root ? `${root}/` : ""}${finding.file}:${range}\`\n`;
  md += `\n### Description\n\n${finding.summary}\n`;
  if (finding.impact) md += `\n### Impact\n\n${finding.impact}\n`;
  if (finding.evidence)
    md += `\n### Vulnerable Code\n\n\`\`\`\n${finding.evidence}\n\`\`\`\n`;
  if (finding.dataFlow) md += `\n### Data Flow\n\n${finding.dataFlow}\n`;
  if (finding.remediation)
    md += `\n### Recommended Fix\n\n${finding.remediation}\n`;
  return md.trim();
}

/** Several findings as one task, most severe first. */
export function findingsMarkdown(
  findings: (Finding & { root?: string; project?: string })[],
  context: { project?: string; root?: string } = {},
) {
  const sorted = [...findings].sort(
    (a, b) => ORDER.indexOf(a.severity) - ORDER.indexOf(b.severity),
  );
  if (sorted.length === 1)
    return findingMarkdown(sorted[0]!, sorted[0]!.root ?? context.root);
  const scope = context.project
    ? `in \`${context.project}\`${context.root ? ` (\`${context.root}\`)` : ""}`
    : "across these projects";
  let md = `# Fix ${sorted.length} security findings ${scope}\n\n`;
  md +=
    "Work through them in order. For each one, confirm the issue in the code, apply the recommended fix, and keep behaviour otherwise unchanged.\n\n";
  md += sorted
    .map((finding, index) =>
      findingMarkdown(
        { ...finding, title: `${index + 1}. ${finding.title}` },
        finding.root ?? context.root,
        "##",
      ),
    )
    .join("\n\n---\n\n");
  return md;
}
