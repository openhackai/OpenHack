# OpenHack v2 guidance

## Scope

This branch contains the local Bun/TypeScript source-scanning CLI and its React/Vite dashboard. Keep scanning in the existing source harness. The dashboard binds to loopback and stores scans, findings, settings, and credentials under the user's `.openhack` directory.

Do not add hosted services, account requirements, deployment secrets, unrelated scanning engines, or automatic remediation.

## CLI and UI contract

- `openhack` opens the local dashboard, starting it in the background when needed.
- `openhack scan` scans the current directory. Explicit commands and provider shortcuts are supported; maintain command-specific help.
- Use the shared Base UI controls and existing app styles. Use the existing app/Nucleo icons; do not add another icon library.
- Preserve explicit user models and saved scan configuration when resuming.

## Validation

Use Bun. Run `bun install --frozen-lockfile`, `bun run check`, and `bun run test:package` before shipping. Automated tests must not call paid model APIs. Keep credentials, local scan data, build outputs, and `node_modules` out of commits. Never log credential values or raw provider errors that may contain source or secrets.
