import { copyFile, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { version } from "../package.json";
import { executableName, releaseTarget, releaseTargets } from "./targets";

const expectedBun = (await Bun.file(new URL("../.bun-version", import.meta.url)).text()).trim();
if (Bun.version !== expectedBun) throw new Error(`Release packaging requires Bun ${expectedBun}; found ${Bun.version}.`);
const requested = process.argv.slice(2);
if (!requested.length) throw new Error("Usage: bun run package:standalone --all | bun-OS-ARCH [...]");
const targets = requested.length === 1 && requested[0] === "--all" ? releaseTargets : requested.map(releaseTarget);
const outDir = resolve("dist/releases");
await mkdir(outDir, { recursive: true });
async function run(cmd: string[], cwd: string, env = process.env) {
  const child = Bun.spawn(cmd, { cwd, env, stdout: "inherit", stderr: "inherit" });
  if (await child.exited !== 0) throw new Error(`Archive command failed: ${cmd[0]}`);
}
for (const target of targets) {
  const platform = target.replace("bun-", "");
  const name = `openhack-${version}-${platform}`;
  const staging = await mkdtemp(join(tmpdir(), "openhack-release-"));
  try {
    const folder = join(staging, name);
    await mkdir(folder);
    const binary = executableName(target);
    await copyFile(resolve("dist/standalone", platform, binary), join(folder, binary));
    for (const file of ["LICENSE", "README.md", "THIRD_PARTY_NOTICES.md"]) {
      await copyFile(file, join(folder, file));
    }
    await copyFile("licenses/BUN-LICENSE.md", join(folder, "BUN-LICENSE.md"));
    await copyFile("web/public/fonts/OFL.txt", join(folder, "FONT-LICENSE.txt"));
    // Preserve notices from the installed dependency distributions as well.
    const notices: string[] = [];
    for (const file of new Bun.Glob("**/*").scanSync({ cwd: "node_modules", onlyFiles: true, dot: true })) {
      if (/(?:^|[/\\])(?:LICENSE|LICENCE|COPYING|NOTICE)(?:\.[^/\\]+)?$/i.test(file)) {
        notices.push(`\n--- ${file} ---\n${await Bun.file(join("node_modules", file)).text()}`);
      }
    }
    await writeFile(join(folder, "DEPENDENCY-LICENSES.txt"), notices.sort().join("\n"));
    const digest = createHash("sha256").update(Buffer.from(await Bun.file(join(folder, binary)).arrayBuffer())).digest("hex");
    await writeFile(join(folder, "build.json"), JSON.stringify({ version, target, bun: Bun.version, binary, sha256: digest }, null, 2) + "\n");
    const windows = target.startsWith("bun-windows-");
    const archive = `${name}.${windows ? "zip" : "tar.gz"}`;
    const archivePath = join(outDir, archive);
    await rm(archivePath, { force: true });
    if (!windows) {
      await run(["tar", "-czf", archivePath, name], staging);
    } else if (process.platform === "win32") {
      await run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "Compress-Archive -LiteralPath $env:OPENHACK_ARCHIVE_SOURCE -DestinationPath $env:OPENHACK_ARCHIVE_DEST"], staging,
        { ...process.env, OPENHACK_ARCHIVE_SOURCE: folder, OPENHACK_ARCHIVE_DEST: archivePath });
    } else {
      await run(["zip", "-qr", archivePath, name], staging);
    }
    const hash = createHash("sha256").update(Buffer.from(await Bun.file(archivePath).arrayBuffer())).digest("hex");
    await writeFile(`${archivePath}.sha256`, `${hash}  ${archive}\n`);
    console.log(`Release archive: ${archivePath}`);
  } finally { await rm(staging, { recursive: true, force: true }); }
}
