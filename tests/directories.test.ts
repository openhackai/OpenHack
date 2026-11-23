import { afterEach, expect, test } from "bun:test";
import { chmod, mkdir, mkdtemp, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { dirname, join, parse } from "node:path";
import { DirectoryListingError, listDirectories } from "../src/server/directories";
import { LocalWorkspace } from "../src/core/workspace";
import { startDashboard } from "../src/server";

const roots: string[] = [];
const servers: Bun.Server<void>[] = [];
afterEach(async () => {
  for (const server of servers.splice(0)) server.stop(true);
  await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true })));
});
async function fixture() {
  const root = await realpath(await mkdtemp(join(tmpdir(), "openhack-directories-")));
  roots.push(root);
  await Promise.all(["zebra", "Alpha", "project10", "project2", ".hidden"].map(name => mkdir(join(root, name))));
  await writeFile(join(root, "private.txt"), "File contents are not part of folder listings.");
  await symlink(join(root, "Alpha"), join(root, "linked-folder"));
  await symlink(join(root, "private.txt"), join(root, "linked-file"));
  await symlink(join(root, "missing"), join(root, "broken-link"));
  return root;
}

test("lists only sorted folders, supports hidden folders, and canonicalizes symlink navigation", async () => {
  const root = await fixture();
  const listing = await listDirectories({ path: root });
  expect(listing.path).toBe(root);
  expect(listing.parent).toBe(dirname(root));
  expect(listing.truncated).toBe(false);
  expect(listing.directories.map(entry => entry.name)).toEqual(["Alpha", "linked-folder", "project2", "project10", "zebra"]);
  expect(listing.directories.every(entry => entry.path === join(root, entry.name))).toBe(true);
  expect((await listDirectories({ path: root, showHidden: true })).directories.some(entry => entry.name === ".hidden")).toBe(true);
  const linked = await listDirectories({ path: join(root, "linked-folder") });
  expect(linked.path).toBe(join(root, "Alpha"));
  expect(linked.parent).toBe(root);
});

test("defaults to home and represents the filesystem root without a parent", async () => {
  expect((await listDirectories({})).path).toBe(await realpath(homedir()));
  expect((await listDirectories({ path: "  " })).path).toBe(await realpath(homedir()));
  expect((await listDirectories({ path: parse(homedir()).root })).parent).toBeNull();
});

test("bounds very large directory listings and reports truncation", async () => {
  const root = await realpath(await mkdtemp(join(tmpdir(), "openhack-directories-large-")));
  roots.push(root);
  for (let start = 0; start < 2001; start += 100) {
    await Promise.all(Array.from({ length: Math.min(100, 2001 - start) }, (_, index) => mkdir(join(root, `folder-${start + index}`))));
  }
  const listing = await listDirectories({ path: root });
  expect(listing.directories).toHaveLength(2000);
  expect(listing.truncated).toBe(true);
});

test("rejects missing folders, files, invalid paths, and reports permission failures clearly", async () => {
  const root = await fixture();
  await expect(listDirectories({ path: join(root, "missing") })).rejects.toThrow("no longer exists");
  await expect(listDirectories({ path: join(root, "private.txt") })).rejects.toThrow("not a file");
  await expect(listDirectories({ path: "relative/folder" })).rejects.toThrow("absolute folder path");
  await expect(listDirectories({ path: "/bad\0path" })).rejects.toThrow("absolute folder path");
  // Removing file read permission has no effect: listings only inspect directory metadata.
  await chmod(join(root, "private.txt"), 0o000);
  expect((await listDirectories({ path: root })).directories).toHaveLength(5);
  await chmod(join(root, "Alpha"), 0o000);
  try {
    if (process.getuid?.() !== 0) {
      const error = await listDirectories({ path: join(root, "Alpha") }).catch(error => error);
      expect(error).toBeInstanceOf(DirectoryListingError);
      expect(error.status).toBe(403);
      expect(error.message).toContain("permission");
    }
  } finally { await chmod(join(root, "Alpha"), 0o700); }
});

test("folder listing HTTP endpoint requires local origin, JSON, and dashboard header", async () => {
  const root = await fixture();
  const server = startDashboard({ workspace: new LocalWorkspace({ home: join(root, "home") }), port: 0 });
  servers.push(server);
  const url = `http://127.0.0.1:${server.port}/api/directories/list`;
  const headers = { "Content-Type": "application/json", "X-OpenHack-Client": "dashboard" };
  expect((await fetch(url)).status).toBe(405);
  expect((await fetch(url, { method: "POST", body: JSON.stringify({ path: root }) })).status).toBe(403);
  expect((await fetch(url, { method: "POST", headers: { ...headers, Origin: "https://untrusted.example" }, body: JSON.stringify({ path: root }) })).status).toBe(403);
  const response = await fetch(url, { method: "POST", headers, body: JSON.stringify({ path: root }) });
  expect(response.status).toBe(200);
  expect((await response.json()).directories.map((entry: { name: string }) => entry.name)).toEqual(["Alpha", "linked-folder", "project2", "project10", "zebra"]);
  expect((await fetch(url, { method: "POST", headers, body: JSON.stringify({ path: join(root, "missing") }) })).status).toBe(400);
  expect((await fetch(url, { method: "POST", headers, body: JSON.stringify({ path: root, showHidden: "yes" }) })).status).toBe(400);
  expect((await fetch(url, { method: "POST", headers, body: JSON.stringify({ path: root, command: "ls" }) })).status).toBe(400);
});
