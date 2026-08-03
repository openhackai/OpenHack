#!/usr/bin/env python3
"""Patch EOL-Debian Dockerfiles in an XBOW benchmark dir so apt still works.

Debian buster/stretch/jessie are past EOL; deb.debian.org/security.debian.org
404 for them. Many benchmark base images are buster-based without saying so in
the tag (e.g. python:2.7.18-slim). So instead of guessing from the FROM tag, we
inject a self-detecting RUN right after every FROM: at build time it checks
/etc/os-release and only rewrites apt repos to archive.debian.org when the distro
is actually buster/stretch/jessie. On bullseye/bookworm/alpine it's a no-op, so
it's safe to inject unconditionally.
"""
import os
import sys

MARKER = "# xbow-eol-apt-fix"
FIX = (
    MARKER + "\n"
    "RUN set -eux; "
    "if [ -f /etc/os-release ] && grep -qiE 'buster|stretch|jessie' /etc/os-release; then "
    "for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do "
    "[ -f \"$f\" ] && sed -i -E "
    "'s|https?://[a-z.]*debian.org/debian-security|http://archive.debian.org/debian-security|g; "
    "s|https?://[a-z.]*debian.org/debian|http://archive.debian.org/debian|g; "
    "s|https?://deb.debian.org|http://archive.debian.org|g; "
    "s|https?://security.debian.org|http://archive.debian.org|g' \"$f\" || true; "
    "done; "
    "sed -i -E '/-updates/d; /-backports/d' /etc/apt/sources.list 2>/dev/null || true; "
    "printf 'Acquire::Check-Valid-Until \"false\";\\n' > /etc/apt/apt.conf.d/10no-check-valid; "
    "fi\n"
)


def patch(path: str) -> bool:
    with open(path, "r", errors="ignore") as f:
        text = f.read()
    if MARKER in text:  # already patched
        return False
    out, injected = [], False
    for line in text.splitlines(keepends=True):
        out.append(line)
        stripped = line.strip().lower()
        # Inject after every real FROM (skip `FROM scratch`, which has no shell).
        if stripped.startswith("from ") and "scratch" not in stripped:
            out.append(FIX)
            injected = True
    if injected:
        with open(path, "w") as f:
            f.write("".join(out))
    return injected


def main(root: str) -> None:
    n = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name == "Dockerfile" or name.startswith("Dockerfile"):
                if patch(os.path.join(dirpath, name)):
                    n += 1
    print(f"patched {n} Dockerfile(s)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
