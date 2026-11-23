import { constants } from "node:fs";
import { mkdir, lstat, link, open, rename, unlink } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, resolve } from "node:path";

export function dataHome(home?: string): string {
  return resolve(home ?? process.env.OPENHACK_HOME ?? `${homedir()}/.openhack`);
}
export async function privateDirectory(path: string): Promise<void> {
  await mkdir(path, { recursive: true, mode: 0o700 });
  const info = await lstat(path);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error(`Storage must be a real directory: ${path}`);
  // Windows uses inherited ACLs, not POSIX directory modes; fchmod fails there.
  if (process.platform === "win32") return;
  const directory = await open(path, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try { await directory.chmod(0o700); } finally { await directory.close(); }
}
export async function regularDirectory(path: string): Promise<void> {
  const info = await lstat(path);
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error(`Storage must be a real directory: ${path}`);
}
export async function readRegular(path: string, maxBytes = 128 * 1024 * 1024): Promise<string> {
  const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await handle.stat();
    if (!info.isFile()) throw new Error("Storage must contain regular files only.");
    if (info.size > maxBytes) throw new Error("Storage file exceeds its supported size limit.");
    const chunks: Buffer[] = [];
    let size = 0;
    for (;;) {
      const chunk = Buffer.allocUnsafe(Math.min(64 * 1024, maxBytes - size + 1));
      const { bytesRead } = await handle.read(chunk, 0, chunk.length, null);
      if (!bytesRead) break;
      size += bytesRead;
      if (size > maxBytes) throw new Error("Storage file exceeds its supported size limit.");
      chunks.push(chunk.subarray(0, bytesRead));
    }
    return Buffer.concat(chunks, size).toString("utf8");
  } finally { await handle.close(); }
}
async function syncDirectory(path: string): Promise<void> {
  if (process.platform === "win32") return;
  const directory = await open(path, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try { await directory.sync(); }
  catch (error) { if (!hasCode(error, "EINVAL") && !hasCode(error, "ENOTSUP")) throw error; }
  finally { await directory.close(); }
}
export async function atomicJson(path: string, value: unknown, options: { durable?: boolean } = {}): Promise<void> {
  const content = `${JSON.stringify(value, null, 2)}\n`;
  const existing = await lstat(path).catch(error => { if (isMissing(error)) return null; throw error; });
  if (existing && !existing.isFile()) throw new Error("Storage must contain regular files only.");
  const temp = `${path}.${crypto.randomUUID()}.tmp`;
  const handle = await open(temp, "wx", 0o600);
  try {
    try { await handle.writeFile(content); if (options.durable) await handle.sync(); }
    finally { await handle.close(); }
    await rename(temp, path);
    if (options.durable) await syncDirectory(dirname(path));
  } finally { await unlink(temp).catch(() => {}); }
}
export function isMissing(error: unknown): boolean {
  return hasCode(error, "ENOENT");
}
function hasCode(error: unknown, code: string): boolean { return !!error && typeof error === "object" && "code" in error && error.code === code; }
export function processAlive(pid: number): boolean {
  if (!Number.isSafeInteger(pid) || pid < 1) return false;
  try { process.kill(pid, 0); return true; }
  catch (error) { return !(error && typeof error === "object" && "code" in error && error.code === "ESRCH"); }
}
/** Atomic nonempty publication prevents a crash between creating a lock and writing its owner. */
export async function processLock(path: string): Promise<() => Promise<void>> {
  return acquireLock(path, 0);
}
async function acquireLock(path: string, depth: number): Promise<() => Promise<void>> {
  if (depth > 8) throw new Error("Storage recovery is busy. Retry shortly.");
  const token = `${process.pid}:${crypto.randomUUID()}`;
  const claim = async () => {
    const temporary = `${path}.${crypto.randomUUID()}.tmp`;
    const handle = await open(temporary, "wx", 0o600);
    try {
      try { await handle.writeFile(token); } finally { await handle.close(); }
      await link(temporary, path);
    } finally { await unlink(temporary).catch(() => {}); }
  };
  try { await claim(); }
  catch (error) {
    if (!hasCode(error, "EEXIST")) throw error;
    // Recovery gates carry their own owner and can themselves recover after a crash.
    const releaseRecovery = await acquireLock(`${path}.recovery`, depth + 1);
    try {
      const existing = await readRegular(path, 512).catch(e => { if (isMissing(e)) return null; throw e; });
      if (existing !== null && !/^[1-9]\d*(?::[a-f0-9-]+)?$/.test(existing)) throw new Error(`Lock is incomplete or invalid: ${path}. Stop scan processes before removing it.`);
      if (existing && processAlive(Number(existing.split(":")[0]))) throw new Error("Another process owns this operation. Wait for it to finish.");
      if (existing) await unlink(path);
      await claim();
    } finally { await releaseRecovery(); }
  }
  return async () => {
    const existing = await readRegular(path, 512).catch(e => { if (isMissing(e)) return ""; throw e; });
    if (existing === token) await unlink(path);
  };
}
