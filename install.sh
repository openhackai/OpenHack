#!/bin/sh
# Install the standalone OpenHack release. No runtime, sudo, or shell-profile edits.
set -eu

fail() { printf 'OpenHack: %s\n' "$*" >&2; exit 1; }
usage() {
  printf '%s\n' 'Usage: install.sh [--version 2.0.0] [--install-dir /absolute/path]' \
    'Defaults: version 2.0.0, installation in $HOME/.local/bin.'
}
cleanup() {
  [ -z "${work:-}" ] || rm -rf "$work"
  [ -z "${pending:-}" ] || rm -f "$pending"
}
download() {
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --connect-timeout 15 --max-time 300 --retry 3 -fsSL "$1" -o "$2"
}
main() {
  version=2.0.0
  install_dir=${HOME:?HOME must be set}/.local/bin
  work=
  pending=
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --version) [ "$#" -ge 2 ] || fail '--version requires a value'; version=${2#v}; shift 2 ;;
      --install-dir) [ "$#" -ge 2 ] || fail '--install-dir requires a value'; install_dir=$2; shift 2 ;;
      -h|--help) usage; return ;;
      *) fail "Unknown option: $1" ;;
    esac
  done
  # Release names become URL and archive path components; accept stable v2+ only.
  printf '%s\n' "$version" | awk -F. 'NF != 3 || $1 !~ /^[0-9]+$/ || $2 !~ /^[0-9]+$/ || $3 !~ /^[0-9]+$/ || $1 < 2 {exit 1} END {if (NR != 1) exit 1}' \
    || fail 'Use a stable version such as 2.0.0 (v2 or newer).'
  case "$install_dir" in /*) ;; *) fail '--install-dir must be an absolute path' ;; esac
  case "$(uname -s)" in
    Darwin) platform=darwin ;;
    Linux) platform=linux ;;
    *) fail 'This installer supports macOS and Linux. For Windows, download the ZIP from GitHub Releases.' ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) arch=arm64 ;;
    x86_64|amd64) arch=x64 ;;
    *) fail 'Unsupported processor. Supported architectures: ARM64 and x64.' ;;
  esac
  if [ "$platform" = linux ] && command -v ldd >/dev/null 2>&1; then
    if ldd --version 2>&1 | grep -qi musl; then
      fail 'These Linux downloads require glibc; Alpine/musl is not supported.'
    fi
  fi
  for tool in curl tar mktemp mkdir chmod mv rm; do
    command -v "$tool" >/dev/null 2>&1 || fail "Required system tool not found: $tool"
  done
  if command -v sha256sum >/dev/null 2>&1; then hash_tool=sha256sum
  elif command -v shasum >/dev/null 2>&1; then hash_tool=shasum
  else fail 'SHA-256 verification requires sha256sum or shasum.'; fi
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  work=$(mktemp -d "${TMPDIR:-/tmp}/openhack-install.XXXXXX")
  name="openhack-$version-$platform-$arch"
  archive="$name.tar.gz"
  base="https://github.com/openhackai/OpenHack/releases/download/v$version"
  printf 'Downloading OpenHack %s for %s-%s…\n' "$version" "$platform" "$arch"
  download "$base/$archive?download=1" "$work/$archive" \
    || fail "Could not download v$version for $platform-$arch. Check your connection and that this version is published at https://github.com/openhackai/OpenHack/releases. Draft releases are not installable."
  download "$base/$archive.sha256?download=1" "$work/checksum" || fail 'Could not download the release checksum; nothing was installed.'
  expected=$(awk -v file="$archive" 'NR == 1 && NF == 2 && $2 == file && length($1) == 64 && $1 !~ /[^0-9a-f]/ {hash=$1} END {if(NR != 1 || hash == "") exit 1; print hash}' "$work/checksum") \
    || fail 'Invalid release checksum file; nothing was installed.'
  if [ "$hash_tool" = sha256sum ]; then actual=$(sha256sum "$work/$archive")
  else actual=$(shasum -a 256 "$work/$archive"); fi
  actual=${actual%% *}
  [ "$actual" = "$expected" ] || fail 'Checksum mismatch; nothing was installed.'
  # Stream only known members to files we create. Never extract archive paths to disk.
  tar -xOzf "$work/$archive" "$name/openhack" > "$work/openhack" \
    || fail 'The release archive is missing the executable.'
  [ -s "$work/openhack" ] || fail 'The release executable is empty.'
  for notice in LICENSE THIRD_PARTY_NOTICES.md BUN-LICENSE.md FONT-LICENSE.txt DEPENDENCY-LICENSES.txt; do
    tar -xOzf "$work/$archive" "$name/$notice" > "$work/$notice" \
      || fail "The release archive is missing $notice."
  done
  mkdir -p "$install_dir"
  [ ! -d "$install_dir/openhack" ] || fail 'The destination openhack is a directory.'
  notices="$install_dir/openhack-licenses"
  [ ! -L "$notices" ] || fail 'The license directory must not be a symlink.'
  mkdir -p "$notices"
  for notice in LICENSE THIRD_PARTY_NOTICES.md BUN-LICENSE.md FONT-LICENSE.txt DEPENDENCY-LICENSES.txt; do
    [ ! -d "$notices/$notice" ] || fail 'A license destination is a directory.'
    mv -f "$work/$notice" "$notices/$notice"
  done
  # Stage on the destination filesystem, then replace atomically (safe for upgrades).
  pending=$(mktemp "$install_dir/.openhack.XXXXXX")
  cat "$work/openhack" > "$pending"
  chmod 755 "$pending"
  mv -f "$pending" "$install_dir/openhack"
  pending=
  printf 'Installed OpenHack %s to %s/openhack\n' "$version" "$install_dir"
  case ":${PATH:-}:" in
    *:"$install_dir":*) printf 'Run: openhack\n' ;;
    *) printf 'Add this directory to your PATH: %s\nThen run: openhack\n' "$install_dir"
       if [ "$install_dir" = "$HOME/.local/bin" ]; then
         printf '%s\n' 'For this terminal: export PATH="$HOME/.local/bin:$PATH"'
       fi ;;
  esac
  printf '%s\n' 'License notices are in openhack-licenses beside the executable.'
  printf '%s\n' 'If upgrading, stop and restart the existing dashboard to use the new version.'
}
main "$@"
