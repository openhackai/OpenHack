import { isAbsolute } from "node:path";

/** Opens a native folder chooser on this machine; resolves null when the user cancels. */
export type FolderPicker = (signal: AbortSignal) => Promise<string | null>;

export class FolderPickerError extends Error {
  constructor(readonly status: 408 | 409 | 500 | 501, message: string) { super(message); }
}

const PROMPT = "Choose a project to scan";
const PICK_TIMEOUT_MS = 10 * 60_000;
export const NO_NATIVE_PICKER = "No native folder picker is available on this system.";

interface Attempt { argv: string[]; env?: Record<string, string> }
interface Outcome { code: number; stdout: string; stderr: string }

async function run({ argv, env }: Attempt, signal: AbortSignal): Promise<Outcome> {
  const child = Bun.spawn(argv, {
    stdin: "ignore", stdout: "pipe", stderr: "pipe",
    env: env ? { ...process.env, ...env } : undefined,
  });
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; child.kill(); }, PICK_TIMEOUT_MS);
  const abort = () => child.kill();
  signal.addEventListener("abort", abort, { once: true });
  try {
    const [stdout, stderr, code] = await Promise.all([
      new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited,
    ]);
    if (timedOut) throw new FolderPickerError(408, "The folder chooser was open for too long and was closed. Click Browse to try again.");
    return { code, stdout, stderr };
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", abort);
  }
}

/** Trims the trailing newline and separator (keeping filesystem roots such as "/" and "C:\\") and requires an absolute path. */
function cleanPath(raw: string, windows = false): string | null {
  let path = raw.replace(/[\r\n]+$/, "");
  if (!path) return null;
  if (windows) { if (!/^[a-zA-Z]:\\$/.test(path)) path = path.replace(/\\+$/, ""); }
  else if (path.length > 1) path = path.replace(/\/+$/, "") || "/";
  const absolute = windows ? /^(?:[a-zA-Z]:\\|\\\\)/.test(path) : isAbsolute(path);
  if (!absolute || path.includes("\0")) {
    throw new FolderPickerError(500, "The folder chooser returned an unexpected path. Type the project path instead.");
  }
  return path;
}

const failed = () => new FolderPickerError(500, "The folder chooser could not be opened. Type the project path instead.");

/** System Events activation brings the dialog in front of the browser; falls back if automation is not permitted. */
async function pickMac(signal: AbortSignal): Promise<string | null> {
  const osascript = Bun.which("osascript");
  if (!osascript) throw new FolderPickerError(501, NO_NATIVE_PICKER);
  const choose = `set chosenFolder to choose folder with prompt "${PROMPT}"`;
  const attempts: Attempt[] = [
    { argv: [osascript, "-e", "tell application \"System Events\"", "-e", "activate", "-e", choose, "-e", "end tell", "-e", "POSIX path of chosenFolder"] },
    { argv: [osascript, "-e", "activate", "-e", choose, "-e", "POSIX path of chosenFolder"] },
  ];
  for (const attempt of attempts) {
    const result = await run(attempt, signal);
    if (signal.aborted) return null;
    if (result.code === 0) return cleanPath(result.stdout);
    if (/-128|User cancel+ed/i.test(result.stderr)) return null;
  }
  throw failed();
}

async function pickLinux(signal: AbortSignal): Promise<string | null> {
  if (!process.env.DISPLAY && !process.env.WAYLAND_DISPLAY) throw new FolderPickerError(501, NO_NATIVE_PICKER);
  const zenity = Bun.which("zenity");
  const kdialog = Bun.which("kdialog");
  const attempts: Attempt[] = [
    ...(zenity ? [{ argv: [zenity, "--file-selection", "--directory", `--title=${PROMPT}`] }] : []),
    ...(kdialog ? [{ argv: [kdialog, "--title", PROMPT, "--getexistingdirectory", process.env.HOME ?? "/"] }] : []),
  ];
  if (!attempts.length) throw new FolderPickerError(501, NO_NATIVE_PICKER);
  for (const attempt of attempts) {
    const result = await run(attempt, signal);
    if (signal.aborted) return null;
    if (result.code === 0) return cleanPath(result.stdout);
    // Both tools exit 1 on cancel (often with harmless toolkit warnings); other failures try the next tool.
    if (result.code === 1 && !/cannot open display|could not connect|failed to open|cannot connect/i.test(result.stderr)) return null;
  }
  throw failed();
}

const windowsScript = [
  "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
  "Add-Type -AssemblyName System.Windows.Forms",
  "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog",
  `$dialog.Description = '${PROMPT}'`,
  "$dialog.ShowNewFolderButton = $false",
  "$owner = New-Object System.Windows.Forms.Form -Property @{ TopMost = $true; ShowInTaskbar = $false }",
  "if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($dialog.SelectedPath) }",
  "$owner.Dispose()",
].join("; ");

async function pickWindows(signal: AbortSignal): Promise<string | null> {
  const shell = Bun.which("powershell.exe") ?? Bun.which("powershell") ?? Bun.which("pwsh");
  if (!shell) throw new FolderPickerError(501, NO_NATIVE_PICKER);
  const result = await run({ argv: [shell, "-NoProfile", "-STA", "-Command", windowsScript] }, signal);
  if (signal.aborted) return null;
  if (result.code !== 0) throw failed();
  return cleanPath(result.stdout, true);
}

export const pickNativeFolder: FolderPicker = signal => {
  if (process.platform === "darwin") return pickMac(signal);
  if (process.platform === "win32") return pickWindows(signal);
  if (process.platform === "linux" || process.platform === "freebsd" || process.platform === "openbsd") return pickLinux(signal);
  return Promise.reject(new FolderPickerError(501, NO_NATIVE_PICKER));
};
