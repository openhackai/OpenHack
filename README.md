# OpenHack

OpenHack is a local AI security scanner for source code. Pick a project, connect your AI provider, and review findings in a browser dashboard or your terminal.

![OpenHack v2 demo showing onboarding, scanning, and reviewing findings](docs/assets/openhack-v2-demo.gif)

The v2 harness maps your codebase, assigns focused source reviews, challenges candidate findings, and judges the evidence. Scan history, findings, notes, and configuration stay in your local workspace. The selected AI provider processes the source excerpts needed for each review.

## Install

On macOS or Linux:

```sh
curl -fsSL https://install.openhack.com | sh
```

The installer currently selects **v2.0.0**. It detects your OS and processor, downloads the matching GitHub release archive, verifies its SHA-256 checksum, and installs `openhack` in `~/.local/bin`. License notices are preserved in `~/.local/bin/openhack-licenses`. It needs no sudo, does not modify shell profiles, and does not start the app automatically. If needed, add the directory to your current terminal's PATH:

```sh
export PATH="$HOME/.local/bin:$PATH"
openhack
```

To select a version or installation directory:

```sh
curl -fsSL https://install.openhack.com | sh -s -- --version 2.0.0 --install-dir "$HOME/.local/bin"
```

**Release status:** [v2.0.0 is available](https://github.com/openhackai/OpenHack/releases/tag/v2.0.0) for macOS and Linux. Both macOS builds are Developer ID signed and Apple notarized. Windows downloads are deferred until publisher signing is ready. Earlier releases use the previous harness and are not installed by this script.

To install manually, use the standalone downloads on [GitHub Releases](https://github.com/openhackai/OpenHack/releases). Choose the archive matching your operating system and processor:

| Operating system | Processor | Archive suffix |
| --- | --- | --- |
| macOS | Apple Silicon | `darwin-arm64.tar.gz` |
| macOS | Intel | `darwin-x64.tar.gz` |
| Linux | ARM64 | `linux-arm64.tar.gz` |
| Linux | x64 | `linux-x64.tar.gz` |

The standalone executable includes the Bun runtime and dashboard assets. Running it requires no separate Bun, Node.js, Python, or package installation. Linux archives target glibc systems; Alpine/musl is not supported by these builds.

Extract the archive, then run `openhack`. You can place the executable in a directory on your `PATH` to run it from any terminal. Archives also include licenses and a build manifest; a separate SHA-256 checksum file accompanies each download.

An AI provider is required to scan. API-key mode needs no additional provider software. To use a Codex or Claude Code subscription, install and sign in to that provider's official CLI.

## Start your first scan

```sh
openhack
```

OpenHack opens the dashboard at **http://127.0.0.1:6969**. The inline setup walks you through three steps:

1. **Connect a provider.** Save an OpenAI or Anthropic API key, or use an existing Codex or Claude Code login.
2. **Choose a project.** Browse with the native folder chooser or enter a directory path. On systems without a native chooser, use the built-in folder browser.
3. **Start the scan.** Review the settings and follow the scan's progress.

The dashboard runs in the background. Closing the browser leaves it running; running `openhack` again reopens it. Use `openhack --no-open` to start it without opening a browser, or `openhack --port 6970` to choose another port.

## Providers

| Provider | Authentication | Billing |
| --- | --- | --- |
| OpenAI API | OpenAI API key | Your API account |
| Anthropic API | Anthropic API key | Your API account |
| Codex | Official Codex CLI login | ChatGPT plan limits or API billing, depending on the login |
| Claude Code | Official Claude Code CLI login | Your Claude plan's usage limits |

Connect a provider in **Settings** or use environment variables for API access:

```sh
export OPENAI_API_KEY='your-api-key'
openhack scan /path/to/project --openai

export ANTHROPIC_API_KEY='your-api-key'
openhack scan /path/to/project --anthropic
```

For existing subscriptions:

```sh
codex login
openhack scan /path/to/project --codex

claude auth login
openhack scan /path/to/project --claude-code
```

OpenHack keeps control of the scanning workflow and source tools. Subscription adapters invoke the provider CLI in an isolated temporary directory with its native execution tools disabled. They reuse the CLI's authentication without copying its login tokens.

Use a current provider CLI that supports OpenHack's isolation flags. Model access and usage limits depend on your provider account. Subscription requests can be slower because each tool round invokes the CLI again.

Select a provider and model in Settings or from the terminal:

```sh
openhack providers
openhack config set provider codex
openhack config set model gpt-6-sol
openhack config set reasoning high
```

Switching the saved provider chooses its default model. A model supplied for an individual scan applies only to that scan. Resumed scans retain their original provider, model, and configuration.

## Dashboard

- **Dashboard:** project overview, open findings, severity counts, and recent scans.
- **Scans:** all projects, live activity, agent tasks, coverage, findings, and controls to stop or resume scans.
- **Findings:** source evidence, validation, remediation guidance, notes, and manual open/resolved/dismissed status.
- **OpenHack Pro:** learn about the enterprise product's business-impact assessment, triage, automatic fixes, and Slack integration. These are Pro capabilities, not integrations enabled in the local scanner.
- **Settings:** provider credentials, model, reasoning effort, worker count, tool rounds, project context, and local configuration.

A scan with useful results and unfinished work is marked **Partial**. Its findings remain available, and resume retries unfinished tasks. A partial scan does not mean the codebase is clean.

## Terminal commands

```sh
openhack scan                         # Scan the current directory
openhack scan /path/to/project
openhack scan . --workers 4 --steps 20
openhack scan . --context 'Multi-tenant API; verify tenant isolation.'
openhack scan . --json --fail-on high

openhack scans                       # Current project's history
openhack scans --all                 # All projects
openhack show SCAN_ID
openhack resume SCAN_ID --steps 30

openhack findings --severity high --state open
openhack status SCAN_ID:FINDING_ID resolved
openhack note SCAN_ID:FINDING_ID 'Reviewed the authorization boundary.'

openhack dashboard status
openhack dashboard stop
openhack version
openhack help scan
```

`scans` and `findings` default to the current project; pass a directory or `--all` to broaden the scope. The dashboard always shows all projects. Use `openhack help` or `openhack COMMAND --help` for the full command reference.

Progress goes to stderr; `--json` writes the final report to stdout. Scan exit codes are `0` for complete, `1` for failed/partial or invalid input, `2` when an open validated finding meets `--fail-on`, and `130` for cancellation.

Ctrl+C saves a checkpoint for a terminal scan. Use Stop for a dashboard scan. Resume requires the same source snapshot; after changing the source, start a new scan. `openhack dashboard stop` checkpoints scans owned by that dashboard before shutting it down.

## Data and source handling

OpenHack listens on loopback and stores its workspace under `~/.openhack`:

```text
settings.json
credentials.json
dashboard-<port>.json
dashboard-<port>.log
projects/<project-hash>/
  project.json
  scans/<scan-id>.json
  triage.json
```

Use `OPENHACK_HOME` or `--home DIRECTORY` to choose a different workspace. Reports include source evidence, coverage, configuration, usage, activity, and checkpoints. Triage carries forward across scans of the same project; older reports remain available in scan history.

Saved API keys are local plaintext in `credentials.json`, with owner-only file permissions where supported. They are not returned by the dashboard API or included in reports. An API key in the environment takes precedence over a saved key. Provider login tokens remain managed by the provider CLI.

Source collection honors `.gitignore` and `.openhackignore`, skips symlinks and common credential/dependency/build paths, and records exclusions and incomplete coverage. Add sensitive paths to `.openhackignore` before scanning.

Project code is read, not executed. Required source excerpts and paths are sent to your chosen remote AI provider; scanning is not offline inference. OpenAI API requests use `store: false`. Provider usage and retention policies still apply. Findings are AI source reviews, not proof of a successful runtime exploit. The local scanner does not automatically modify code or create pull requests.

## Updating

When a new standalone release is available, stop the dashboard, replace the executable with the matching new download, and restart it. Scan data remains in your workspace. There is no automatic updater yet.

On macOS/Linux, you can rerun the installer with `--version` to select a published v2-or-newer release. Downloads are verified before replacing the executable; restart a running dashboard to use the new binary.

```sh
openhack dashboard stop
# Replace the executable with the new download.
openhack
```

## Development

Install the Bun version pinned in `.bun-version`, then:

```sh
git clone https://github.com/openhackai/OpenHack.git
cd OpenHack
bun install --frozen-lockfile
bun run dev
```

The development UI runs at **http://localhost:5173** with hot reload, and its API defaults to port 6969. If an installed dashboard is already using that port:

```sh
OPENHACK_DEV_API_PORT=6970 bun run dev
```

Development uses the same workspace unless you override `OPENHACK_HOME`. To replay the complete onboarding flow against existing scan history, open `http://localhost:5173/?demo=onboarding#/`. Finishing setup starts a real scan and opens the full scan list. This replay is development-only and is excluded from production builds.

Validation does not call paid model APIs:

```sh
bun run check          # Types, deterministic tests, production build
bun run test:package   # Extracted package and dashboard lifecycle
bun audit
```

The terminal cancellation test uses Python 3's standard-library PTY support on macOS/Linux. This is a development test dependency, not an installation requirement for OpenHack.

## Building standalone executables

```sh
bun run build:standalone
bun run test:standalone
```

This creates `dist/openhack` (or `dist/openhack.exe`) for the build machine. To build all supported targets, or one target:

```sh
bun run build:all
bun run package:standalone --all

bun run build:standalone --target=bun-linux-x64
bun run package:standalone bun-linux-x64
```

Cross-platform executables are written to `dist/standalone/<platform>/`; versioned archives and checksums go to `dist/releases/`. CI builds and smoke-tests each of the six targets on its corresponding operating system and architecture, including dashboard assets and start/reuse/stop behavior with no runtime on `PATH`.

The general build workflow uploads unsigned CI artifacts. The separate **Signed macOS builds** workflow runs manually from `main`: it signs with Developer ID, requires Apple notarization to succeed, tests the signed executable on its native architecture, and only then packages the archive and final checksums. It never publishes a release automatically. Windows publisher signing remains a separate release prerequisite. A successful cross-compile alone is not a native runtime test.

The macOS workflow uses these Actions secrets: `MACOS_CERTIFICATE_P12` (base64 PKCS#12 identity including the intermediate certificate), `MACOS_CERTIFICATE_PASSWORD`, `MACOS_SIGNING_IDENTITY`, `APPLE_NOTARY_KEY_P8`, `APPLE_NOTARY_KEY_ID`, and `APPLE_NOTARY_ISSUER`. Signing keys are imported into an isolated temporary keychain and removed after the job. No credentials belong in this repository. The workflow refuses to run against unmerged branches.

For local signing, set `OPENHACK_SIGNING_IDENTITY`, `OPENHACK_SIGNING_KEYCHAIN`, `OPENHACK_NOTARY_KEY_FILE`, `OPENHACK_NOTARY_KEY_ID`, and `OPENHACK_NOTARY_ISSUER`, then run `bash scripts/macos/sign-and-notarize.sh dist/standalone/darwin-arm64/openhack` (or the `darwin-x64` executable). An Apple rejection or timeout fails the command. Bare executable files cannot have notarization tickets stapled; Apple records their signed hashes. Never modify or re-sign an accepted binary before packaging it.

## License

See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Standalone distributions include Bun, font, and dependency license notices.
