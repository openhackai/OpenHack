import { opendir, realpath, open, stat } from "node:fs/promises";
import { constants, type Dirent } from "node:fs";
import { relative, resolve, extname, basename, sep, isAbsolute } from "node:path";
import ignore, { type Ignore } from "ignore";
import { readSnapshotBytes } from "./source-reader";

const excludedDirs = new Set([".git", ".openhack", ".codex", ".claude", ".ssh", ".aws", ".azure", ".gnupg", "node_modules", "vendor", ".next", "dist", "build", "out", "coverage", ".venv", "venv", "__pycache__", ".cache", ".terraform"]);
const extensions = new Set(".ts .tsx .js .jsx .mjs .cjs .py .go .rs .rb .php .java .kt .kts .cs .c .h .cpp .hpp .swift .scala .ex .exs .erl .sh .bash .zsh .sql .prisma .graphql .gql .vue .svelte .html .json .yaml .yml .toml .xml .tf .md .conf .config .ini .gradle .r .dart .lua .pl".split(" "));
const secretName = /^(\.env(?:\..*)?|\.npmrc|\.pypirc|\.netrc|credentials(?:\..*)?|id_(rsa|ed25519|ecdsa)(?:\..*)?)$/i;
const secretExt = /\.(pem|key|p12|pfx|keystore)$/i;
const lockName = /^(bun\.lockb?|package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock|cargo\.lock)$/i;
type Rule = { dir: string; matcher: Ignore };
export interface Project {
  root: string; files: Map<string, string>; bytes: number; skipped: number; limited: boolean; omissions: { path: string; reason: string }[]; readFiles: Set<string>;
}

// Snapshot once: model tools never reopen paths supplied by the model.
export async function inventory(path: string, signal?: AbortSignal): Promise<Project> {
  const root = await realpath(resolve(path));
  const project: Project = { root, files: new Map(), bytes: 0, skipped: 0, limited: false, omissions: [], readFiles: new Set() };
  async function safeRead(full: string, max: number) {
    const target = await realpath(full), relativeTarget = relative(root, target);
    if (!relativeTarget || relativeTarget === ".." || relativeTarget.startsWith(`..${sep}`) || isAbsolute(relativeTarget)) throw new Error("Outside project");
    const before = await stat(target);
    if (!before.isFile() || before.size > max) throw new Error("File too large or not regular");
    // NONBLOCK prevents a concurrent replacement with a FIFO from hanging inventory.
    const handle = await open(full, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const opened = await handle.stat();
      if (!opened.isFile() || opened.dev !== before.dev || opened.ino !== before.ino || await realpath(full) !== target) throw new Error("Source changed while being read");
      const bytes = await readSnapshotBytes(handle, before.size, max, signal);
      const after = await handle.stat(), current = await stat(target);
      if (after.size !== before.size || after.mtimeMs !== before.mtimeMs || after.ctimeMs !== before.ctimeMs || current.dev !== opened.dev || current.ino !== opened.ino || await realpath(full) !== target) throw new Error("Source changed while being read");
      const controls = bytes.reduce((n, c) => n + (c < 32 && c !== 9 && c !== 10 && c !== 13 ? 1 : 0), 0);
      if (controls > Math.max(3, bytes.length * 0.01)) throw new Error("Binary source");
      try { return new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
      catch { throw new Error("Source is not valid UTF-8"); }
    } finally { await handle.close(); }
  }
  let visited = 0, traversalLimited = false;
  async function walk(dir: string, inherited: Rule[], depth = 0) {
    signal?.throwIfAborted();
    if (traversalLimited) return;
    if (depth > 128) { project.limited = true; project.omissions.push({path: relative(root, dir), reason: "Directory depth limit"}); return; }
    if (await realpath(dir) !== dir) throw new Error("A source directory changed during inventory. Retry the scan.");
    const rules = [...inherited];
    for (const name of [".gitignore", ".openhackignore"]) {
      try { rules.push({ dir, matcher: ignore().add(await safeRead(resolve(dir, name), 65536)) }); }
      catch (error: any) { if (error.code !== "ENOENT") throw new Error(`Cannot safely read ${relative(root, dir)}/${name}`); }
    }
    const entries: Dirent[] = [];
    const directory = await opendir(dir, {bufferSize: 128});
    for await (const entry of directory) {
      signal?.throwIfAborted();
      if (++visited > 100_000) {
        traversalLimited = true; project.limited = true;
        project.omissions.push({path: relative(root, dir) || ".", reason: "Directory entry limit"});
        break;
      }
      entries.push(entry);
    }
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      signal?.throwIfAborted();
      const full = resolve(dir, entry.name), name = relative(root, full).split(sep).join("/");
      let ignored = false;
      for (const rule of rules) {
        const match = rule.matcher.test(relative(rule.dir, full).split(sep).join("/") + (entry.isDirectory() ? "/" : ""));
        if (match.ignored) ignored = true;
        else if (match.unignored) ignored = false;
      }
      if (entry.isSymbolicLink() || excludedDirs.has(entry.name) || secretName.test(entry.name) || secretExt.test(entry.name) || lockName.test(entry.name) || /\.min\.[cm]?js$/i.test(entry.name) || ignored) {
        project.skipped++; continue;
      }
      if (entry.isDirectory()) { await walk(full, rules, depth + 1); continue; }
      if (!entry.isFile() || (!extensions.has(extname(entry.name).toLowerCase()) && !["Dockerfile", "Makefile", "Gemfile", "Procfile"].includes(basename(full)))) { project.skipped++; continue; }
      if (project.files.size >= 5000 || project.bytes >= 64_000_000) { project.skipped++; project.limited = true; project.omissions.push({path:name,reason:"Snapshot file or byte limit"}); continue; }
      try {
        const source = await safeRead(full, 256_000), size = Buffer.byteLength(source);
        if (project.bytes + size > 64_000_000) { project.skipped++; project.limited = true; project.omissions.push({path:name,reason:"Snapshot file or byte limit"}); continue; }
        project.files.set(name, source); project.bytes += size;
      } catch (error) { signal?.throwIfAborted(); project.skipped++; project.limited = true; project.omissions.push({path:name,reason:error instanceof Error && ["Outside project", "File too large or not regular", "File exceeds source size limit", "Source changed while being read", "Binary source", "Source is not valid UTF-8"].includes(error.message) ? error.message : "Unreadable source"}); }
    }
  }
  await walk(root, []);
  return project;
}

export function useTool(project: Project, name: string, args: Record<string, unknown>): unknown {
  if (name === "list_files") {
    const offset = Number.isInteger(args.offset) ? Math.max(0, args.offset as number) : 0;
    const files = [...project.files.keys()].filter(f => f.includes(String(args.contains ?? "")));
    return { files: files.slice(offset, offset + 150), total: files.length, nextOffset: offset + 150 < files.length ? offset + 150 : null };
  }
  if (name === "read_file") {
    const path = String(args.path), source = project.files.get(path);
    if (source === undefined) return { error: "File not in scan snapshot. Use list_files for exact relative paths." };
    const lines = source.split("\n"), start = Math.max(1, Number.isInteger(args.start) ? args.start as number : 1);
    const end = Math.min(lines.length, start + 159);
    project.readFiles.add(path);
    return { path, totalLines: lines.length, start, end, source: lines.slice(start - 1, end).map((l, i) => `${start + i}: ${l.slice(0, 1200)}`).join("\n").slice(0, 18000) };
  }
  if (name === "search") {
    const query = String(args.query ?? "").toLowerCase();
    if (!query) return { error: "Supply a non-empty literal search query." };
    const matches: { file: string; line: number; text: string }[] = [];
    for (const [file, source] of project.files) {
      if (!file.includes(String(args.pathContains ?? ""))) continue;
      for (const [index, line] of source.split("\n").entries()) {
        if (line.toLowerCase().includes(query)) matches.push({ file, line: index + 1, text: line.slice(0, 450) });
        if (matches.length >= 40) return { matches, truncated: true };
      }
    }
    return { matches, truncated: false };
  }
  return { error: "Unknown tool. Only list_files, read_file, and search are available." };
}
