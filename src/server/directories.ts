import { opendir, realpath, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, isAbsolute, join } from "node:path";
import { z } from "zod";
import type { DirectoryEntry, DirectoryListing } from "../shared/contracts";

const MAX_DIRECTORIES = 2000;
export const directoryListSchema = z.object({
  path: z.string().max(4096).optional(),
  showHidden: z.boolean().optional(),
}).strict();

export class DirectoryListingError extends Error {
  constructor(readonly status: 400 | 403, message: string) { super(message); }
}

function readableError(error: unknown): DirectoryListingError {
  if (error instanceof DirectoryListingError) return error;
  const code = (error as NodeJS.ErrnoException)?.code;
  if (code === "EACCES" || code === "EPERM") return new DirectoryListingError(403, "OpenHack does not have permission to open this folder. Choose another folder or grant access in your system settings.");
  if (code === "ENOENT") return new DirectoryListingError(400, "This folder no longer exists. Choose another folder.");
  if (code === "ENOTDIR") return new DirectoryListingError(400, "Choose a folder, not a file.");
  if (code === "ELOOP") return new DirectoryListingError(400, "This folder shortcut could not be opened because it contains a circular link.");
  return new DirectoryListingError(400, "This folder could not be opened. Check its path and try again.");
}

/** Reads folder names and metadata only; never opens project file contents. */
export async function listDirectories(input: z.infer<typeof directoryListSchema>): Promise<DirectoryListing> {
  const valid = directoryListSchema.parse(input);
  const requested = valid.path?.trim() || homedir();
  if (!isAbsolute(requested) || requested.includes("\0")) throw new DirectoryListingError(400, "Enter an absolute folder path.");
  try {
    const path = await realpath(requested);
    if (!(await stat(path)).isDirectory()) throw new DirectoryListingError(400, "Choose a folder, not a file.");
    const directories: DirectoryEntry[] = [];
    let truncated = false;
    const entries = await opendir(path);
    for await (const entry of entries) {
      if (!valid.showHidden && entry.name.startsWith(".")) continue;
      const child = join(path, entry.name);
      const isDirectory = entry.isDirectory() || (entry.isSymbolicLink() && await stat(child).then(info => info.isDirectory(), () => false));
      if (!isDirectory) continue;
      if (directories.length === MAX_DIRECTORIES) { truncated = true; break; }
      directories.push({ name: entry.name, path: child });
    }
    directories.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }) || a.name.localeCompare(b.name));
    const parent = dirname(path);
    return { path, parent: parent === path ? null : parent, directories, truncated };
  } catch (error) { throw readableError(error); }
}
