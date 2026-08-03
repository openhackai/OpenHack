# OpenHack Scanner Guidance

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

