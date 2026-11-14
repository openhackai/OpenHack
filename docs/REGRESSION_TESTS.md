# Security regression tests

When OpenHack verifies a vulnerability by exploiting it in its sandbox, it can
also write a **security regression test** for it. The test goes into your
repository's existing test suite and keeps the bug from coming back.

A generated test is only accepted after OpenHack shows, in its Docker
sandbox, that the test **fails on the vulnerable code because the
vulnerability exists** and **passes on the remediated code**. An accepted
test is an ordinary test file. It runs with your normal test command and in
your CI, with no OpenHack install and no LLM calls.

The feature is **opt-in and disabled by default**. It needs Docker, the same
as sandbox verification.

## Where it fits

```
recon → hunters → validation → sandbox exploit verification → (browser verification)
                                      │ records redacted exploit evidence
                                      ▼
                       Step 6: security regression tests
                         invariant → generate → sandbox validation
                                      ▼
            finding.regressionTest · patch artifact · /copy fix prompt
```

The open-source scanner does not apply fixes or open pull requests. The
validated test is delivered in three ways: as a patch you can `git apply`,
alongside the finding in the report and the TUI, and inside the `/copy`
AI-fix prompt. That way it lands in the same change as the fix.

## Requirements

- **Docker** running locally (the same daemon sandbox verification uses).
- **Sandbox verification** enabled (`sandbox_enabled`, or `/verify sandbox`
  in the TUI). The invariant is derived from the recorded exploit, so a
  finding that was only reviewed by the LLM validator is reported as
  `missing_evidence`.
- **A remediated revision** to validate against: a git commit, branch or tag
  containing the fix, or a directory with the fixed code.
- **An existing test suite** that OpenHack supports (currently pytest).
  OpenHack never bootstraps a test framework your repository doesn't use.

## Enabling and configuring

Each setting can go in `~/.openhack/config`, an environment variable (the
upper-cased name), or a `.env` file. `regression_tests_enabled` and
`regression_tests_fixed_ref` can also be set from the TUI with `/config`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `regression_tests_enabled` | `false` | Run Step 6 for every scan |
| `regression_tests_fixed_ref` | unset | Git revision or directory holding the fix |
| `regression_tests_max_attempts` | `3` | Generation attempts per finding, each with validation feedback |
| `regression_tests_validation_runs` | `2` | Sandbox runs per revision (outcomes must be identical) |
| `regression_tests_timeout` | `180` | Seconds per test run before the container is killed |
| `regression_tests_build_timeout` | `900` | Seconds to build a sandbox image |
| `regression_tests_base_image` | `python:<version>-slim` | Base image for sandbox images (`<version>` from `.python-version`/`runtime.txt`) |
| `regression_test_model_id` | session model | Model used by the generator agent |

```bash
export SANDBOX_ENABLED=true
export REGRESSION_TESTS_ENABLED=true
openhack --scan .
```

## Workflows

### Scan, fix, then validate

This is the most common order of events, because a fix usually doesn't exist
yet when the scan runs.

1. Run `openhack --scan .` with the feature enabled. Each verified finding
   gets a test that has been shown to fail on the vulnerable code. With no fix
   to validate against, its status is `remediation_unavailable` and the test
   is saved as a pending artifact.
2. Fix the vulnerability on a branch, for example with the `/copy` prompt,
   which already contains the pending test as an acceptance criterion.
3. Validate against the fix:

   ```bash
   openhack --regression-tests <session-id> fix/idor
   ```

   The vulnerable revision is the commit that was scanned (recorded in the
   report). If the scan ran on uncommitted changes, the working tree is used
   instead. The remediated revision is, in order of preference: the argument;
   then `regression_tests_fixed_ref`; then `HEAD`, if it has moved past the
   scanned commit. Pass a directory (for example `.`) to validate an
   uncommitted fix in the working tree. A pending test is re-validated
   **without any LLM call**. A new test is generated only if the pending one
   no longer reproduces the vulnerability.

The command updates the saved report and exits non-zero if any eligible
finding ended without a validated test. Unsupported classes and findings
without exploit evidence don't count as failures.

### The fix already exists

Set `regression_tests_fixed_ref` (for example in CI, to the PR branch) and the
scan validates against it immediately.

### In the TUI

`/config regression_tests_enabled true`, then `/scan`. The Findings tab shows
each finding's regression-test status, the generated test, its run command
and the patch path. `/copy` includes the test in the AI-fix prompt.

## How a test is generated and validated

1. **Security invariant.** For IDOR, the sandbox exploit request (for example
   `GET /api/notes/1`) is generalized into an endpoint template
   (`/api/notes/{note_id}`), the object identifier, and the operation. From
   those, OpenHack states the property the exploit violated: *an
   authenticated principal must not read/update/delete an object owned by
   another principal, whatever identifier it supplies*. It also records what
   must keep working: the owner still has access. Captured credentials are
   redacted before any of this is stored or shown to a model.
2. **Generation.** An agent reads a private copy of the repository
   (read-only, jailed) and writes one new test file using your existing
   framework, fixtures and conventions. It must declare which test functions
   are **invariant tests** (expected to fail on vulnerable code) and which are
   **control tests** (legitimate access, expected to pass on both versions).
3. **Checks before anything runs.** The test must be a new file under your
   existing test directory that pytest will discover. It can never overwrite,
   modify or delete an existing test. A static policy rejects skips and
   xfails, swallowed assertions, network or subprocess use, dynamic code
   execution, sleeps, pytest hooks, and embedded credentials.
4. **Sandbox validation.** For each revision, OpenHack builds an image from a
   private copy of that revision plus your project's own test dependencies,
   then runs the test in a fresh locked-down container (by default twice per
   revision). The test is accepted only if:
   - on the **vulnerable** code, every control test passes and at least one
     invariant test fails *on an assertion*. A crash, import error, missing
     fixture, skip or error doesn't count;
   - on the **remediated** code, every test passes;
   - every declared test ran, and all runs of a revision produced identical
     outcomes.
5. **Feedback.** A rejected test is sent back to the generator with the
   precise reason and per-test outcomes, up to `regression_tests_max_attempts`
   times. One case is never retried: if an invariant test still fails its
   assertion on the remediated code, the fix may be incomplete. Retrying would
   mean weakening the test until it passes, so the finding is reported as
   `validation_failed` instead.

## Sandbox isolation

Generated tests and the repository code they exercise are treated as
untrusted. They only ever execute inside a container started with:

- `--network none`. Dependencies are installed during the image build, but
  the test run can't reach the network.
- No host mounts. The test goes in, and results come out, via `docker cp`.
- A non-root user (`65534`), `--cap-drop ALL` and `no-new-privileges`.
- Memory, CPU and PID limits, plus a wall-clock timeout after which the
  container is killed.
- A fresh container per run, always removed afterwards. Images are removed
  when the run finishes.

The private workspace copies leave out `.env` files (templates like
`.env.example` are kept), private keys, `.npmrc`/`.pypirc`/`.netrc`, cloud
credential directories, symlinks and git submodules. Your checkout is never
modified.

## Results and artifacts

Every finding gets a `regressionTest` entry in the scan report
(`~/.openhack/scans/<session-id>.json`). It holds the status, the reason, the
invariant, the test path and code, the run command, and per-revision
outcomes. Verified findings also carry their redacted `exploitEvidence`.

Validated and pending tests are written, owner-only, to
`~/.openhack/scans/artifacts/<session-id>/regression-tests/<finding-id>/`:

- the test file, at its repository path;
- `regression-test.patch`, to apply from the repository root with
  `git apply <patch>`;
- `result.json`, the full validation record.

Rejected tests are kept in the report for review but are never written as
artifacts.

### Statuses

| Status | Meaning | What to do |
| --- | --- | --- |
| `validated` | Fails on the vulnerable revision, passes on the remediated one | Apply the patch with your fix |
| `remediation_unavailable` | Reproduces the vulnerability; there was no fix to validate against | Fix it, then run `openhack --regression-tests <session-id> <fixed-revision>` |
| `unsupported_vulnerability` | The class isn't supported yet | — |
| `unsupported_framework` | No supported existing test suite was found | — |
| `missing_evidence` | The finding wasn't verified by sandbox exploitation | Enable sandbox verification |
| `generation_failed` | No valid test was produced (the policy rejected every attempt, or the model never submitted one) | Retry, or use a stronger `regression_test_model_id` |
| `validation_failed` | Candidate tests ran but didn't meet the criteria (including an incomplete fix) | Read the reason; check that the fix covers every operation |
| `sandbox_error` | Docker was unavailable, or an image couldn't be built | Start Docker; check dependency installation or `regression_tests_base_image` |

## Supported classes and frameworks

- **Vulnerability classes:** IDOR / broken object-level authorization
  (CWE-639).
- **Test frameworks:** pytest (Python), including Flask, FastAPI, Starlette and
  pytest-django projects. Requirements files (including `-r`/`-c` includes),
  `pyproject.toml` test extras and installable projects are supported, and so
  are monorepos: the project containing the finding is used.
- **Not yet supported** (reported as unsupported): other vulnerability
  classes; Jest, Vitest, Mocha and other JavaScript runners; Go `testing`;
  JUnit; RSpec; PHPUnit; and Django's `manage.py test` runner without pytest.

To add a vulnerability class, subclass `VulnerabilityClassSpec` in
`openhack/regression/invariants.py` and register it in `CLASS_SPECS`. To add a
test framework, subclass `FrameworkAdapter` in
`openhack/regression/frameworks.py` and register it in `ADAPTERS`.

## Limitations

- Tests that need external services (databases, caches, message brokers)
  can't run, because the container has no network and no service containers.
  Suites that use in-process test clients with in-memory or SQLite state work.
- Building a sandbox image installs your dependencies, which needs network
  access to your package index. Behind a private index or TLS interception,
  set `regression_tests_base_image` to an image configured for it.
- Validation proves the test tells the two revisions apart. Validate against
  a focused fix commit: unrelated changes in the remediated revision make that
  signal less specific.
- The static policy is a guardrail, not the isolation boundary — the container
  is. A test runs inside the same process as pytest, so result integrity also
  relies on the policy and on cross-checking declared tests against reported
  outcomes.
- During a scan, the vulnerable revision is the working tree that was scanned.
- Only one test file is generated per finding, and existing tests are never
  edited.

## Testing this feature

```bash
uv run python -m pytest tests/test_regression_*.py
# Real Docker end-to-end (needs network access for image builds):
OPENHACK_DOCKER_TESTS=1 uv run python -m pytest tests/test_regression_e2e_docker.py
```

`OPENHACK_TEST_BASE_IMAGE` overrides the base image used by the end-to-end
tests. The fixture is an intentionally vulnerable Flask notes API with its own
pytest suite, plus its remediated counterpart, in
`tests/fixtures/regression/idor_notes_app/`.
