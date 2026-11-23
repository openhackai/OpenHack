import { afterEach, expect, test } from "bun:test";
import { mkdir, mkdtemp, readFile, readdir, realpath, rm, stat, symlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { atomicJson, privateDirectory, processLock, readRegular } from "../src/core/files";
import { readCredentials, saveCredential } from "../src/core/credentials";
import { LocalWorkspace } from "../src/core/workspace";

const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });
async function fixture() {
  const root = await realpath(await mkdtemp(join(tmpdir(), "openhack-storage-hardening-")));
  roots.push(root);
  return root;
}

test("atomic writes preserve old data on serialization failure and refuse symlink targets", async () => {
  const root = await fixture(), path = join(root, "data.json");
  await atomicJson(path, { value: "original" });
  const circular: Record<string, unknown> = {};
  circular.self = circular;
  await expect(atomicJson(path, circular)).rejects.toThrow();
  expect(JSON.parse(await readFile(path, "utf8"))).toEqual({ value: "original" });
  expect((await readdir(root)).filter(name => name.endsWith(".tmp"))).toEqual([]);
  await atomicJson(path, { value: "replacement" });
  expect(JSON.parse(await readFile(path, "utf8"))).toEqual({ value: "replacement" });
  expect((await stat(path)).mode & 0o777).toBe(0o600);
  const alias = join(root, "alias.json");
  await symlink(path, alias);
  await expect(atomicJson(alias, { value: "bad" })).rejects.toThrow("regular files");
  expect(JSON.parse(await readFile(path, "utf8"))).toEqual({ value: "replacement" });
});

test("private directory creation refuses links without changing the target permissions", async () => {
  const root = await fixture(), target = join(root, "target"), alias = join(root, "alias");
  await mkdir(target, { mode: 0o755 });
  const originalMode = (await stat(target)).mode;
  await symlink(target, alias);
  await expect(privateDirectory(alias)).rejects.toThrow("real directory");
  expect((await stat(target)).mode).toBe(originalMode);
});

test("locks publish an owner atomically and recover dead recovery gates", async () => {
  const root = await fixture(), lock = join(root, "operation.lock");
  const release = await processLock(lock);
  expect(await readRegular(lock, 512)).toMatch(new RegExp(`^${process.pid}:[a-f0-9-]+$`));
  const attempts = await Promise.allSettled(Array.from({ length: 8 }, () => processLock(lock)));
  expect(attempts.every(attempt => attempt.status === "rejected")).toBe(true);
  await release();
  await writeFile(lock, "2147483647:dead");
  await writeFile(`${lock}.recovery`, "2147483647:dead");
  await writeFile(`${lock}.recovery.recovery`, "2147483647:dead");
  const recovered = await processLock(lock);
  expect(await readRegular(lock, 512)).toMatch(new RegExp(`^${process.pid}:`));
  await recovered();
  expect(await readdir(root)).toEqual([]);
});

test("live and invalid recovery gates are never stolen", async () => {
  const root = await fixture(), lock = join(root, "operation.lock");
  await writeFile(lock, "2147483647:dead");
  await writeFile(`${lock}.recovery`, `${process.pid}:abc`);
  await expect(processLock(lock)).rejects.toThrow("Another process owns");
  expect(await readFile(lock, "utf8")).toBe("2147483647:dead");
  await writeFile(`${lock}.recovery`, "");
  await expect(processLock(lock)).rejects.toThrow("incomplete or invalid");
  expect(await readFile(lock, "utf8")).toBe("2147483647:dead");
});

test("storage readers bound file size and reject FIFOs without waiting for a writer", async () => {
  const root = await fixture(), path = join(root, "large.txt");
  await writeFile(path, "oversized");
  await expect(readRegular(path, 4)).rejects.toThrow("size limit");
  if (process.platform === "win32") return;
  const fifo = join(root, "pipe");
  expect(Bun.spawnSync(["mkfifo", fifo]).exitCode).toBe(0);
  await expect(readRegular(fifo)).rejects.toThrow("regular files");
  expect(Bun.spawnSync(["mkfifo", join(root, "credentials.json")]).exitCode).toBe(0);
  await expect(readCredentials(root)).rejects.toThrow("regular file");
});

test("invalid saved configuration never exposes content and configuration reads skip credentials", async () => {
  const root = await fixture(), settings = join(root, "settings.json");
  const app = new LocalWorkspace({ home: root });
  await writeFile(settings, '"SENSITIVE_CONFIGURATION_FRAGMENT');
  const error = await app.getConfiguration().catch(error => error as Error);
  expect(error).toBeInstanceOf(Error);
  expect((error as Error).message).not.toContain("SENSITIVE_CONFIGURATION_FRAGMENT");
  await writeFile(settings, JSON.stringify({ SENSITIVE_CONFIGURATION_FRAGMENT: true }));
  const invalid = await app.getConfiguration().catch(error => error as Error);
  expect((invalid as Error).message).not.toContain("SENSITIVE_CONFIGURATION_FRAGMENT");
  await writeFile(settings, JSON.stringify({ model: "fixture-model" }));
  await writeFile(join(root, "credentials.json"), "malformed private credentials", { mode: 0o600 });
  expect((await app.getConfiguration()).model).toBe("fixture-model");
  await expect(saveCredential(root, "openai", "secret\0suffix")).rejects.toThrow("control characters");
});
