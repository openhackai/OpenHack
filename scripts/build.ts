import { build } from "vite";
await build();
const result = await Bun.build({entrypoints:["src/cli/index.ts"], outdir:"dist", naming:"cli.js", target:"bun", minify:false});
if (!result.success) { for (const log of result.logs) console.error(log); process.exitCode=1; }
