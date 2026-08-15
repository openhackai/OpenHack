# OpenHack Scanner Guidance

## Repository scope

- This is the public OpenHack repository. Keep it focused on the scanner/CLI
  and its interactive TUI.
- Do not add hosted-service implementations or private deployment artifacts
  here, including HTTP service wrappers, cloud task definitions, account
  identifiers, or service-specific Dockerfiles.
- The AI pentest HTTP service and its deployment configuration belong in the
  separate sibling `../openhack_pentest_service/` directory.

## CLI contract

- Running `openhack` with no flags always opens the interactive OpenHack security agent.
- All other CLI entry points and non-interactive behaviors use flags on the
  `openhack` command.
- Do not introduce or recommend command-style subcommands such as
  `openhack doctor` or `openhack demo`. Express proposed CLI functionality as
  flags, while functionality that belongs inside the interactive agent should
  remain part of that agent experience.
- Tailor documentation, examples, product plans, tests, and user-facing copy to
  this contract.
