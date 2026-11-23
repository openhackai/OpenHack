# Third-party notices

The v2 application uses Bun, React, Vite, Base UI, Zod, and the `ignore` package. These projects and their dependencies retain their respective licenses; see their package distributions and the lockfile for the exact versions.

Standalone release builds embed the Bun runtime pinned in `.bun-version`. Its upstream license and linked-library notices are recorded in `licenses/BUN-LICENSE.md` and included in release archives. Update this file when updating the pinned runtime. Release archives also include the available license and notice files from installed dependencies, and the Inter font license.

Inter is distributed under the SIL Open Font License 1.1. Its complete license is included in `web/public/fonts/OFL.txt` and in the built dashboard.

Icon provenance for the app-supplied Nucleo assets is recorded in `web/src/components/nucleo-icons.manifest.json`. Third-party assets retain their applicable licenses.

Codex and Claude Code are separately installed applications invoked through their public CLIs. Their executables, credentials, and source code are not included in this repository.
