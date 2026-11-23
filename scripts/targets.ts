export const releaseTargets = [
  "bun-darwin-arm64", "bun-darwin-x64",
  "bun-linux-arm64", "bun-linux-x64",
  "bun-windows-arm64", "bun-windows-x64",
] as const satisfies readonly Bun.Build.CompileTarget[];

export type ReleaseTarget = typeof releaseTargets[number];

export function releaseTarget(value: string): ReleaseTarget {
  if (!releaseTargets.includes(value as ReleaseTarget)) throw new Error(`Unsupported target: ${value}. Choose ${releaseTargets.join(", ")}`);
  return value as ReleaseTarget;
}

export function executableName(target: string): string {
  return target.startsWith("bun-windows-") ? "openhack.exe" : "openhack";
}
