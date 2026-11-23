import { realpath, stat } from "node:fs/promises";
import { extname, isAbsolute, relative, resolve, sep } from "node:path";
import { embeddedAssets } from "./embedded";

function contained(root: string, candidate: string): boolean {
  const path = relative(root, candidate);
  return path !== ".." && !path.startsWith(`..${sep}`) && !isAbsolute(path);
}

/** Resolves only regular assets inside the built dashboard, including symlink checks. */
export async function dashboardFile(webDir: string, pathname: string, embedded = false): Promise<string | null> {
  let decoded: string;
  try { decoded = decodeURIComponent(pathname); } catch { return null; }
  if (decoded.includes("\0") || decoded.includes("\\") || decoded.split("/").some(part => part === ".." || part.startsWith("."))) {
    return null;
  }
  if (embedded) {
    return Object.hasOwn(embeddedAssets, decoded) ? embeddedAssets[decoded]!
      : extname(decoded) ? null : embeddedAssets["/index.html"] ?? null;
  }
  const root = await realpath(webDir).catch(() => null);
  if (!root) return null;
  const candidate = resolve(root, `.${decoded}`);
  if (!contained(root, candidate)) return null;
  const requested = await realpath(candidate).catch(() => null);
  if (requested && contained(root, requested) && (await stat(requested)).isFile()) return requested;
  if (extname(decoded)) return null;
  const index = await realpath(resolve(root, "index.html")).catch(() => null);
  return index && contained(root, index) && (await stat(index)).isFile() ? index : null;
}
