# OpenHack local

A Bun CLI and a local React dashboard for scanning source code, reviewing scan history, and managing findings. Both use the same files under `~/.openhack`. No OpenHack login, hosted database, organization, or subscription is needed.

## Run

Requires Bun 1.3 or newer.

```sh
bun install --frozen-lockfile
bun run build
bun link

openhack
```

`openhack` opens the local dashboard directly in your default browser, starting it in the background if needed. There is no terminal menu. Use the dashboard to run scans, view findings, configure API keys or existing subscriptions, and change settings.

The dashboard binds to **127.0.0.1:4900** by default and keeps running after the command exits. Running `openhack` again reopens the existing dashboard. Add `--no-open` to start it without launching a browser, or `--port 490` to choose another port (low ports may require privileges on some systems).

Explicit commands remain available for scripts and direct use:

```sh
export OPENAI_API_KEY='your-api-key'
openhack scan                    # current directory
openhack scan --codex
openhack scan --claude-code
openhack scan /path/to/project
openhack dashboard status
openhack dashboard stop
```

The explicit `openhack dashboard` command runs the server in the foreground until Ctrl+C. It reuses an existing OpenHack dashboard on the requested port for the same data directory. Add `--no-open` to print the URL without launching a browser, or `--port 490` to use a different port for this invocation.

The default command behaves the same in a terminal or pipe; use `--help` for command guidance and explicit commands with `--json` for automation.

The package's executable is also available directly as `./bin/openhack` after building, or `bun src/cli/index.ts` during development. `bun link` registers the executable in Bun's bin directory; ensure that directory is on your PATH. If another OpenHack installation exists, `command -v openhack` shows which one will run.

## Providers and subscriptions

Configure a provider from dashboard Settings: save, replace, or remove an API key, or select an automatically detected Codex or Claude Code subscription. Provider availability is shown alongside the provider selection. API settings let you change a key or model; subscription providers use their existing login. You can also select a configured provider in the dashboard's **Settings → Provider**, or with `--provider`. Switching providers picks its default model; you can then customize it. Reports save their provider and model, so resuming a scan keeps the original configuration.

| Provider | Authentication | Default model |
| --- | --- | --- |
| `openai` | Saved API key or `OPENAI_API_KEY` | `gpt-5-mini` |
| `anthropic` | Saved API key or `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` |
| `codex` | Installed official Codex CLI with an existing ChatGPT login or saved API key | `gpt-6-sol` |
| `claude-code` | Installed official Claude Code signed in with Claude | `claude-opus-4-8` |

```sh
# Use an existing subscription login.
codex login
openhack scan --codex

claude auth login
openhack scan --claude-code

# Or use your own API key.
export ANTHROPIC_API_KEY='your-api-key'
openhack scan --anthropic

openhack providers
openhack config set provider claude-code
```

The CLI options call the official CLI on your machine using its existing authentication. Codex accepts its saved ChatGPT login or saved API key; provider status identifies which mode is configured. A Codex API-key login uses API billing, not a ChatGPT subscription. Claude Code uses its existing Claude subscription login. OpenHack does not copy login tokens or API keys out of either CLI. Parent API-key environment variables are excluded from these child processes. Your configured account's model access and usage limits apply; the CLI must be installed and available on `PATH`. Use a current CLI version supporting the isolation flags; incompatible versions fail with an explicit error.

OpenHack retains control of the source scanner and its read-only tools. Subscription requests run in a temporary empty directory with native tools, user rules, plugins, hooks and external MCP configuration disabled. Structured requests return either a scanner tool call or a schema-validated result. Only source excerpts selected by the scanner are passed into the model conversation. Subscription mode can be slower than direct API access because each tool round invokes a CLI process.

Dashboard provider setup saves API keys to `~/.openhack/credentials.json` with owner-only permissions (`0600`). They are stored as local plaintext, separate from scan reports and settings. A nonempty `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in the process environment takes precedence over the corresponding saved key. Dashboard Settings can refresh provider availability and accepts new keys through a masked input. Saved key values are never returned to the browser; only their configuration status is shown. With a custom `--home` or `OPENHACK_HOME`, saved credentials belong to that workspace instead.

For a persistent provider default use `openhack config set provider PROVIDER`. `OPENAI_MODEL`, `OPENAI_REASONING_EFFORT`, and `ANTHROPIC_MODEL` provide initial API defaults. Codex model `default` leaves model selection to the CLI. Codex and Claude Code logins remain in their own CLI-managed credential stores; OpenHack does not save their login tokens.

See the official [Codex non-interactive documentation](https://developers.openai.com/codex/noninteractive) and [Claude Code headless documentation](https://code.claude.com/docs/en/headless) for CLI authentication and plan details.

## Commands

```sh
openhack scan . --model gpt-5-mini --workers 4 --steps 20
openhack scan . --reasoning high --context 'Multi-tenant API; verify tenant isolation.'
openhack scan . --json --fail-on high
openhack scans                   # current directory
openhack scans --all             # all projects
openhack show SCAN_ID
openhack resume SCAN_ID --steps 30
openhack findings --severity high --state open
openhack status SCAN_ID:FINDING_ID resolved
openhack note SCAN_ID:FINDING_ID 'Reviewed the authorization boundary.'
openhack help scan
openhack config
openhack config set model gpt-5-mini
openhack config set workers 4
openhack config set reasoning default
```

`scan` defaults to your current working directory. `scans` and `findings` also default to that project; supply another directory or `--all` to expand their scope. The dashboard always shows all projects. Provider shortcuts (`--codex`, `--claude-code`, `--openai`, `--anthropic`) apply to one scan and preserve saved settings. Multiple provider selectors are rejected.

Use `openhack help`, `openhack help COMMAND`, or `COMMAND --help` for guidance. `openhack version` prints the installed version. `settings`, `doctor`, `history`, and `list` are aliases for `config`, `providers`, `scans`, and `findings`.

`scans`, `show`, `findings`, `status`, and `config` accept `--json`. Findings print the full `SCAN_ID:FINDING_ID` key used by `status` and `note`. `--home DIRECTORY` or `OPENHACK_HOME` selects another workspace without changing your shell's home directory.

Scan exit codes: **0** complete, **1** failed/partial or invalid input, **2** an open validated finding meets `--fail-on`, **130** cancelled. Interactive scans show the provider and configured model, an animated elapsed-time indicator, current stage, active/completed tasks, files read, findings, request count and latest activity. Piped output gets plain progress logs with a heartbeat every 15 seconds. Progress goes to stderr; `--json` writes the final report to stdout. Ctrl+C cancels requests and saves a checkpoint. Resuming requires the same source snapshot; changed source needs a new scan.

## Dashboard

- **Scans:** all project directories, status and search filters, browse local folders to select a project path, start a scan, inspect coverage, findings, agent tasks and activity, resume or stop work.
- **Findings:** current findings across projects, severity/status filters, source evidence, validation, remediation, notes, and manual open/resolved/dismissed status. Historical findings remain available through each scan.
- **Settings:** provider, saved API-key management, model, worker count, tool rounds, reasoning effort, project context, port, storage location and refreshable provider availability. API keys are never returned to the browser or written to settings.

Visual tokens, Inter typography, sidebar, table measurements, and light/dark styles are adapted from the existing OpenHack app. Existing-control icons match the main OpenHack app; new controls use the Desktop Nucleo UI library. Source paths and checksums are recorded in `web/src/components/nucleo-icons.manifest.json`. Shared Base UI controls provide custom selects, checkboxes, number inputs, dialogs and tooltips, with consistent field and button styling. The local app contains only these three sections.

The CLI and dashboard can run together. The dashboard refreshes every three seconds. A scan can only be stopped by the process that started it: use Ctrl+C while a terminal scan runs, or Stop in the dashboard for a dashboard scan. Use `openhack dashboard start`, `openhack dashboard status`, and `openhack dashboard stop` to manage the background server. Closing the browser does not stop a managed background dashboard. Stopping the dashboard checkpoints its active scans. Older dashboards without lifecycle support must be stopped in their original terminal.

## Storage and scope

```text
~/.openhack/
  settings.json
  credentials.json
  dashboard-<port>.json
  dashboard-<port>.log
  projects/<hash-of-canonical-project-path>/
    project.json
    scans/<scan-id>.json
    triage.json
```

Reports contain source evidence, scan configuration, coverage, usage, agent activity and checkpoints. Files use owner-only permissions (0600), directories 0700, and writes are atomic. Triage follows the finding fingerprint across scans of the same project; identical findings in different projects are independent. The findings list uses each project's newest completed report or report with findings, retaining older findings while a new empty scan runs. Older reports remain in scan history. Existing project-local `.openhack` stores from the exploratory CLI are not automatically imported.

Dashboard runtime files identify the local server and authorize lifecycle actions; background server output is appended to the matching log. Clean shutdown removes its runtime file. API credentials and runtime tokens are not included in scan reports or returned by dashboard APIs.

Valid locks and recovery gates owned by dead processes are recovered automatically. Legacy malformed/empty lock files fail closed and require manual removal only after confirming no scan process is running. Do not copy or edit a workspace while its scans are running.

The scanning engine is the source-review harness from `exploration/cli-scan`, with its storage adapted here. It maps the project, assigns source to hunters, independently reviews candidates, and judges evidence. Only required scanner modules are included; there is no demo engine, shell tool, automatic patching, pull request creation, or additional scanning harness.

Scanning uses your selected remote model provider; it is **not offline inference**. The dashboard and saved results are local. Source excerpts and paths go to the selected provider, whose usage and retention policies apply. OpenAI API requests use `store: false`. Direct API requests may incur charges; subscription requests consume the existing plan’s usage allowance. The initial provider is OpenAI API, with four workers and twenty tool rounds.

Source collection honors `.gitignore` and `.openhackignore`, skips symlinks, `.env` files, common credential files, dependencies and build outputs, and bounds inventory size. Add sensitive paths to `.openhackignore`. Project code is read, never executed. Exclusions and incomplete coverage are recorded; partial scans are not presented as clean. A validated finding is an AI source review, not runtime exploit verification; resolved is a manual triage decision.

## Development

```sh
bun run dev        # Vite on 127.0.0.1:5173, API on 127.0.0.1:4900
bun run check      # types, deterministic tests, production build
bun run test:package # pack, extract, start/stop and check assets outside this repo
bun audit
```

Stop the normal dashboard before using `bun run dev`, since both use port 4900. Development and production share `OPENHACK_HOME` unless overridden. Build output contains the CLI and dashboard assets together. `bun pm pack` rebuilds before packing; do not publish a package that has not passed `check` and `test:package`. The package can be moved/packed with `bin/` and `dist/`; it does not depend on the original harness or app directories.

```text
src/cli/       commands, dashboard lifecycle, terminal formatting
src/core/      workspace orchestration and filesystem primitives
src/harness/   source inventory, coverage, scanner and report schemas
src/providers/ OpenAI/Anthropic API selection and subscription CLI adapters
src/server/    loopback HTTP API, request guards, static asset serving
src/shared/    Zod request schemas and shared TypeScript contracts
web/src/       React pages, components and app styles
scripts/       Vite development and package build
```

Tests exercise the actual orchestration with a deterministic model client, coverage, cross-project persistence, triage, cancellation/resume, lock handling and HTTP boundaries. They never call a paid model API. The Inter font license is included at `web/public/fonts/OFL.txt` and copied into the built dashboard.

On macOS and Linux, the terminal cancellation regression test uses Python 3's standard-library `pty` module to verify that Ctrl+C saves a real scan checkpoint before the process exits.
