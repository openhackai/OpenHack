import { build } from "vite";
import { resolve } from "node:path";
import { mkdir } from "node:fs/promises";
import { executableName, releaseTarget, releaseTargets } from "./targets";

const args = process.argv.slice(2);
const targetArg = args.find(arg => arg.startsWith("--target="));
const all = args.includes("--all-targets");
if (all && targetArg) throw new Error("Choose --all-targets or --target, not both.");
if (all || targetArg) {
  const expected = (await Bun.file(new URL("../.bun-version", import.meta.url)).text()).trim();
  if (Bun.version !== expected) throw new Error(`Release builds require Bun ${expected}; found ${Bun.version}.`);
}
const targets = all ? [...releaseTargets] : targetArg ? [releaseTarget(targetArg.slice("--target=".length))] : [undefined];
for (const arg of args) {
  if (!["--standalone", "--all-targets", targetArg].includes(arg)) throw new Error(`Unknown build option: ${arg}`);
}
await build();
const result = await Bun.build({entrypoints:["src/cli/index.ts"], outdir:"dist", naming:"cli.js", target:"bun", minify:false});
if (!result.success) { for (const log of result.logs) console.error(log); process.exit(1); }

if (args.includes("--standalone") || all || targetArg) {
  const webDir = resolve("dist/web");
  const files = [...new Bun.Glob("**/*").scanSync({ cwd: webDir, onlyFiles: true })].sort();
  const imports = files.map((file, index) => `import asset${index} from ${JSON.stringify(resolve(webDir, file))} with { type: "file" };`);
  const entries = files.map((file, index) => `${JSON.stringify("/" + file.replaceAll("\\", "/"))}: asset${index}`);
  for (const target of targets) {
    const outDir = target ? `dist/standalone/${target.replace("bun-", "")}` : "dist";
    await mkdir(outDir, { recursive: true });
    const outfile = `${outDir}/${executableName(target ?? `bun-${process.platform === "win32" ? "windows" : process.platform}-${process.arch}`)}`;
    const compiled = await Bun.build({
      entrypoints: ["src/cli/index.ts"],
      target: "bun",
      compile: { outfile, ...(target ? { target } : {}) },
      plugins: [{
        name: "embed-dashboard",
        setup(builder) {
          builder.onLoad({ filter: /[/\\]server[/\\]embedded\.ts$/ }, () => ({
            contents: `${imports.join("\n")}\nexport const standalone = true;\nexport const embeddedAssets = {${entries.join(",")}};`,
            loader: "ts",
          }));
        },
      }],
    });
    if (!compiled.success) { for (const log of compiled.logs) console.error(log); process.exit(1); }
    console.log(`Standalone executable: ${outfile} (${files.length} embedded dashboard files)`);
  }
}
