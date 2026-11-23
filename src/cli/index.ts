#!/usr/bin/env bun
import { z } from "zod";
import { runCli } from "./run";
import { safe } from "./display";

runCli(process.argv.slice(2)).catch(error => {
  console.error(`OpenHack: ${safe(error instanceof z.ZodError ? error.issues.map(issue => `${issue.path.join(".")}: ${issue.message}`).join("; ") : error instanceof Error ? error.message : "Unexpected error")}`);
  process.exitCode = 1;
});
