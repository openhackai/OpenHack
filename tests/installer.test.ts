import { afterEach, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { chmod, mkdir, mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const roots: string[] = [];
const installer = resolve(import.meta.dir, "../install.sh");
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });
async function fixture(os = "Darwin", arch = "arm64", missingBinary = false) {
  const root = await mkdtemp(join(tmpdir(), "openhack-installer-")); roots.push(root);
  const home = join(root, "home with spaces"), mocks = join(root, "commands");
  await mkdir(home); await mkdir(mocks);
  const platform = os === "Darwin" ? "darwin" : "linux";
  const target = arch === "arm64" || arch === "aarch64" ? "arm64" : "x64";
  const name = `openhack-2.0.0-${platform}-${target}`;
  const folder = join(root, name); await mkdir(folder);
  if (!missingBinary) await writeFile(join(folder, "openhack"), "fixture executable; must not be run by installer\n");
  for (const notice of ["LICENSE", "THIRD_PARTY_NOTICES.md", "BUN-LICENSE.md", "FONT-LICENSE.txt", "DEPENDENCY-LICENSES.txt"])
    await writeFile(join(folder, notice), "license fixture\n");
  const archive = join(root, name + ".tar.gz");
  const tar = Bun.spawn(["tar", "-czf", archive, "-C", root, name], { stdout: "pipe", stderr: "pipe" });
  expect(await tar.exited).toBe(0);
  const digest = createHash("sha256").update(await readFile(archive)).digest("hex");
  const checksum = join(root, "checksum");
  await writeFile(checksum, `${digest}  ${name}.tar.gz\n`);
  const scripts = {
    uname: '#!/bin/sh\nif [ "$1" = -s ]; then printf "%s\\n" "$TEST_OS"; else printf "%s\\n" "$TEST_ARCH"; fi\n',
    ldd: '#!/bin/sh\nprintf "%s\\n" "${TEST_LIBC:-glibc}"\n',
    curl: `#!/bin/sh
[ -z "$TEST_DOWNLOAD_FAIL" ] || exit 22
while [ "$#" -gt 0 ]; do
  case "$1" in https://*) url=$1; shift ;; -o) dest=$2; shift 2 ;; *) shift ;; esac
done
printf '%s\\n' "$url" >> "$TEST_URL_LOG"
case "$url" in *.sha256\\?download=1) cp "$TEST_CHECKSUM" "$dest" ;; *) cp "$TEST_ARCHIVE" "$dest" ;; esac
`,
  };
  for (const [name, body] of Object.entries(scripts)) {
    await writeFile(join(mocks, name), body); await chmod(join(mocks, name), 0o755);
  }
  const log = join(root, "urls");
  const env = { ...process.env, HOME: home, PATH: `${mocks}:${process.env.PATH}`, TEST_OS: os, TEST_ARCH: arch,
    TEST_ARCHIVE: archive, TEST_CHECKSUM: checksum, TEST_URL_LOG: log, TEST_DOWNLOAD_FAIL: "", TEST_LIBC: "glibc" };
  async function run(args: string[] = [], extra = {}) {
    const child = Bun.spawn(["/bin/sh", installer, ...args], { env: { ...env, ...extra }, stdout: "pipe", stderr: "pipe" });
    const [code, out, err] = await Promise.all([child.exited, new Response(child.stdout).text(), new Response(child.stderr).text()]);
    return { code, out, err };
  }
  return { home, root, checksum, run, log, name };
}

for (const [os, arch, target] of [["Darwin", "arm64", "darwin-arm64"], ["Darwin", "x86_64", "darwin-x64"], ["Linux", "aarch64", "linux-arm64"], ["Linux", "x86_64", "linux-x64"]]) {
  test(`installer verifies and installs ${target} without executing it`, async () => {
    const f = await fixture(os, arch);
    const result = await f.run();
    expect(result.code).toBe(0);
    const binary = join(f.home, ".local/bin/openhack");
    expect(await readFile(binary, "utf8")).toContain("must not be run");
    expect((await stat(binary)).mode & 0o777).toBe(0o755);
    expect(await readFile(join(f.home, ".local/bin/openhack-licenses/BUN-LICENSE.md"), "utf8")).toContain("license fixture");
    expect(await readFile(f.log, "utf8")).toContain(`/releases/download/v2.0.0/openhack-2.0.0-${target}.tar.gz`);
    expect(result.out).toContain('export PATH="$HOME/.local/bin:$PATH"');
  });
}

test("installer preserves an existing installation after bad checksum or failed download", async () => {
  const f = await fixture();
  const dir = join(f.root, "custom bin"); await mkdir(dir);
  const binary = join(dir, "openhack"); await writeFile(binary, "existing");
  await writeFile(f.checksum, `${"0".repeat(64)}  ${f.name}.tar.gz\n`);
  expect((await f.run(["--install-dir", dir])).err).toContain("Checksum mismatch");
  expect(await readFile(binary, "utf8")).toBe("existing");
  const missing = await f.run(["--install-dir", dir], { TEST_DOWNLOAD_FAIL: "1" });
  expect(missing.code).not.toBe(0);
  expect(missing.err).toContain("Draft releases are not installable");
  expect(await readFile(binary, "utf8")).toBe("existing");
});

test("installer rejects malformed metadata, missing executable, unsupported hosts and unsafe version paths", async () => {
  const f = await fixture();
  await writeFile(f.checksum, `${"0".repeat(64)}  unrelated.tar.gz\n`);
  expect((await f.run()).err).toContain("Invalid release checksum");
  expect((await f.run(["--version", "../../evil"])).code).not.toBe(0);
  expect((await f.run(["--version", "2.0.0\n3.0.0"])).code).not.toBe(0);
  expect((await f.run(["--version", "0.2.4"])).code).not.toBe(0);
  expect((await f.run([], { TEST_OS: "Windows_NT" })).err).toContain("For Windows");
  expect((await f.run([], { TEST_ARCH: "riscv64" })).err).toContain("Unsupported processor");
  expect((await f.run([], { TEST_OS: "Linux", TEST_LIBC: "musl" })).err).toContain("Alpine/musl");
  const incomplete = await fixture("Darwin", "arm64", true);
  expect((await incomplete.run()).err).toContain("missing the executable");
});

test("installer supports custom directories and replaces an old binary", async () => {
  const f = await fixture();
  const dir = join(f.root, "custom bin"); await mkdir(dir);
  await writeFile(join(dir, "openhack"), "old");
  expect((await f.run(["--version", "v2.0.0", "--install-dir", dir])).code).toBe(0);
  expect(await readFile(join(dir, "openhack"), "utf8")).toContain("fixture executable");
});
