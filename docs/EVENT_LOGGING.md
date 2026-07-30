# OpenHack event journal

OpenHack writes two complementary records for each session:

- `~/.openhack/scans/<session-id>.json` is the human-facing report.
- `~/.openhack/scans/<session-id>.events.jsonl` is the append-only operational
  journal used for crash recovery and forensic debugging.

The report uses schema version 3. The event journal uses schema version 1.
Each JSONL event has a sequence number, stable event ID, session/turn/model/tool
correlation IDs, a monotonic timestamp, the preceding event hash, and its own
SHA-256 hash. `EventJournal.verify()` checks the complete chain.

## What is recorded

The journal records session and status transitions, repository/worktree state,
user and assistant messages, context compaction, every model request, streamed
content and tool-argument fragment, provider finish reasons, usage and cost,
latency, retries and failures, tool inputs/results, completion-guard decisions,
report writes, cancellations, and partial output present at an error.

Reasoning streams are represented by character counts rather than private
chain-of-thought text. Common credentials are redacted from journal and report
records. Files containing events and large-output artifacts are created with
owner-only permissions.

## Large outputs

Normal model context still receives bounded tool output. The durable record does
not silently discard the remainder:

- Report trace output is not truncated.
- When shell output exceeds the context cap, its complete stdout/stderr is saved
  under `~/.openhack/scans/artifacts/<session-id>/` with mode `0600`.
- The tool result and journal contain the artifact path, size, and SHA-256.

## Completion contract

The interactive agent must call `finish_task` with its complete user-facing
answer. A text-only response is treated as progress and triggers a continuation
turn. Provider `length`/`max_tokens` finishes also continue automatically;
provider safety/content-filter finishes are retained and returned as errors.
Iteration and no-progress limits remain bounded safety backstops.

## Resume behavior

Version 3 reports contain the exact API message history, including assistant
tool calls and tool results. If a process exits before the report is refreshed,
resume reconstructs the same history and system prompt from `message_appended`
and `agent_configuration` journal events. Older reports fall back to their
compact trace reconstruction.

## Filesystem policy

The operator-driven interactive and read-only planning agents may resolve
absolute paths anywhere the current OS account can access. The autonomous scan
pipeline remains target-jailed, and jailed checks use path-component semantics
to prevent sibling-prefix and symlink escapes.
