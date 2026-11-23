import { providerDefinitions } from "../shared/providers";
import type { FindingRecord, Report } from "../shared/contracts";
// Source and project names are untrusted terminal input.
export const safe = (value: unknown) => String(value).replace(/[\x00-\x1f\x7f-\x9f]/g, " ");
export function printScans(scans: Report[]) {
  if (!scans.length) return console.log("No scans found. Run openhack scan in your project, or openhack scans --all.");
  for (const scan of scans) console.log(`${safe(scan.id)}  ${scan.state.padEnd(10)}  ${safe(scan.project)}  ${scan.findings.length} findings\n  ${safe(scan.root)}`);
}
export function printFindings(findings: FindingRecord[]) {
  if (!findings.length) return console.log("No findings match.");
  for (const finding of findings) console.log(`${safe(finding.key)}  ${finding.severity.toUpperCase()}  ${finding.status}  ${finding.validation}\n  ${safe(finding.title)}\n  ${safe(finding.root)}/${safe(finding.file)}:${finding.line}`);
}
export function printScan(scan: Report) {
  console.log(`${safe(scan.project)} · ${scan.state}\nScan: ${safe(scan.id)}\nPath: ${safe(scan.root)}\nProvider: ${providerDefinitions[scan.provider ?? "openai"].label}\nModel: ${safe(scan.model)}\nFiles: ${scan.inventory.files} · Findings: ${scan.findings.length}\n${safe(scan.phase)}`);
  if (scan.error) console.log(`Error: ${safe(scan.error)}`);
  for (const finding of scan.findings) console.log(`  ${finding.severity.toUpperCase()} [${finding.validation}] ${safe(finding.title)} (${safe(finding.file)}:${finding.line})`);
}
