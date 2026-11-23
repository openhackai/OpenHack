import { afterEach, describe, expect, test } from "bun:test";
import { mkdtemp, mkdir, rm, symlink, writeFile, realpath } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { readSnapshotBytes, type SourceReader } from "../../src/harness/source-reader";
import { inventory } from "../../src/harness/project";

function reader(source: string, chunkSize = 3): SourceReader {
  const bytes = Buffer.from(source);
  return {async read(buffer, offset, length, position) {
    const count = Math.max(0, Math.min(chunkSize, length, bytes.length - position));
    bytes.copy(buffer, offset, position, position + count);
    return {bytesRead: count};
  }};
}
const roots: string[] = [];
afterEach(async () => {await Promise.all(roots.splice(0).map(root => rm(root, {recursive: true, force: true})));});
async function root() {const path = await realpath(await mkdtemp(join(tmpdir(), "openhack-source-safety-"))); roots.push(path); return path;}

describe("stable source snapshot", () => {
  test("reassembles legal short reads without silently truncating UTF-8 source", async () => {
    const source = "export const value = '😀 many bytes';\n";
    expect((await readSnapshotBytes(reader(source), Buffer.byteLength(source), 256000)).toString()).toBe(source);
  });
  test("rejects source that shrank or grew and honors cancellation between reads", async () => {
    await expect(readSnapshotBytes(reader("longer"), 3, 100)).rejects.toThrow("Source changed");
    await expect(readSnapshotBytes(reader("short"), 20, 100)).rejects.toThrow("Source changed");
    const abort = new AbortController(); let calls = 0;
    const changing: SourceReader = {async read(buffer, offset) {calls++; buffer[offset] = 1; abort.abort(new Error("stop snapshot")); return {bytesRead: 1};}};
    await expect(readSnapshotBytes(changing, 20, 100, abort.signal)).rejects.toThrow("stop snapshot");
    expect(calls).toBe(1);
  });
  test("skips agent/cloud credential directories, ignores symlinks, and keeps source exact", async () => {
    const path = await root(), outside = await root();
    for (const directory of [".codex", ".claude", ".aws", ".ssh", ".azure", ".gnupg"]) {
      await mkdir(join(path, directory)); await writeFile(join(path, directory, "auth.json"), '{"secret":"DO_NOT_SCAN"}');
    }
    const source = "export const safe = '🌱';\n";
    await writeFile(join(path, "app.ts"), source);
    await writeFile(join(outside, "secret.ts"), "DO_NOT_SCAN");
    await symlink(outside, join(path, "linked-directory"));
    await symlink(join(outside, "secret.ts"), join(path, "linked-file.ts"));
    const project = await inventory(path);
    expect([...project.files]).toEqual([["app.ts", source]]);
    expect(project.limited).toBe(false);
  });
  test("marks excessive directory depth incomplete instead of recursing without a bound", async () => {
    const path = await root(); let nested = path;
    await writeFile(join(path, "app.ts"), "export const safe = true;");
    for (let level = 0; level < 131; level++) {nested = join(nested, "d"); await mkdir(nested);}
    await writeFile(join(nested, "deep.ts"), "not reached");
    const project = await inventory(path);
    expect(project.limited).toBe(true);
    expect(project.omissions.some(item => item.reason === "Directory depth limit")).toBe(true);
    expect(project.files.size).toBe(1);
  });
});
