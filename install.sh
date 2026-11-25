#!/usr/bin/env bash
# Install the Verifier: its commands (`verifier`, which watches a project and checks a
# paper with `verifier static-check`, and the agent's vcheck, vjob and vwake), on Python 3.13, with uv; and the
# pinned OpenCode the auditing agent runs in. The pinned OpenCode goes in
# ~/.local/share/verifier, so an OpenCode you already have is untouched.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-$HOME/.local/bin}"
OPENCODE_VERSION="1.19.0"
OPENCODE_DIR="$HOME/.local/share/verifier/opencode-$OPENCODE_VERSION"

# The OpenCode build for this machine. VERIFIER_PLATFORM ("<uname -s>-<uname -m>") and
# VERIFIER_AVX2 (yes or no) stand in for the machine's in tests. Each build is pinned by
# SHA-256, the digest that GitHub publishes for the release asset. The macOS builds are zip
# files, the Linux builds tar files. An Intel Mac without AVX2 (or under Rosetta) gets the
# baseline build.
platform="${VERIFIER_PLATFORM:-$(uname -s)-$(uname -m)}"
avx2="${VERIFIER_AVX2:-}"
if [ -z "$avx2" ] && [ "$platform" = "Darwin-x86_64" ]; then
  if sysctl -n machdep.cpu.leaf7_features 2>/dev/null | grep -qw AVX2; then avx2=yes; else avx2=no; fi
fi
case "$platform" in
  Linux-x86_64)
    asset="opencode-linux-x64.tar.gz"
    sha256="7bb9e61ad6feacf2e89c10a6a2c1840e3de4220bb50ba1fc7110f31806076aec" ;;
  Linux-aarch64)
    asset="opencode-linux-arm64.tar.gz"
    sha256="e9841d43d3e1a8f36ed2691592d3b8b4fb097e606c8b559a277a1b845840e26b" ;;
  Darwin-arm64)
    asset="opencode-darwin-arm64.zip"
    sha256="0d5f511948d6fbfedf9bb16753c0ac42613dcc8de75d9deb7e8d0cf584c8fba6" ;;
  Darwin-x86_64)
    if [ "$avx2" = "yes" ]; then
      asset="opencode-darwin-x64.zip"
      sha256="515380b03f3410f2178c8c1c7ba6d1e9556a3bdee2a01d114e7b1681952131a0"
    else
      asset="opencode-darwin-x64-baseline.zip"
      sha256="e96a33a56d1e1a8cfe33e49497a6d2069b63331bdfd02c4d215a22d3cbfc0601"
    fi ;;
  *) echo "error: no pinned OpenCode build for $platform" >&2; exit 1 ;;
esac
if [ "${VERIFIER_INSTALL_DRY_RUN:-}" = "1" ]; then
  echo "opencode: $asset sha256 $sha256 into $OPENCODE_DIR"
  exit 0
fi

command -v uv >/dev/null || { echo "error: uv is required (https://docs.astral.sh/uv/)" >&2; exit 1; }

if [ ! -x "$OPENCODE_DIR/opencode" ]; then
  url="https://github.com/anomalyco/opencode/releases/download/v$OPENCODE_VERSION/$asset"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -o "$tmp/$asset" "$url"
  if command -v sha256sum >/dev/null; then got="$(sha256sum "$tmp/$asset")"
  elif command -v shasum >/dev/null; then got="$(shasum -a 256 "$tmp/$asset")"
  else echo "error: sha256sum or shasum is required" >&2; exit 1; fi
  [ "${got%% *}" = "$sha256" ] \
    || { echo "error: $asset does not have the pinned SHA-256 $sha256" >&2; exit 1; }
  mkdir -p "$OPENCODE_DIR"
  case "$asset" in
    *.tar.gz) tar xz -C "$OPENCODE_DIR" -f "$tmp/$asset" ;;
    *.zip)
      command -v unzip >/dev/null || { echo "error: unzip is required" >&2; exit 1; }
      unzip -q -o "$tmp/$asset" opencode -d "$OPENCODE_DIR"
      chmod +x "$OPENCODE_DIR/opencode" ;;
  esac
fi
"$OPENCODE_DIR/opencode" --version | grep -qx "$OPENCODE_VERSION"

# Editable, so a change to this checkout takes effect without reinstalling.
UV_TOOL_BIN_DIR="$DEST" uv tool install --python 3.13 --editable --force "$ROOT"
echo "installed: verifier, vcheck, vjob and vwake in $DEST, OpenCode $OPENCODE_VERSION in $OPENCODE_DIR"
case ":$PATH:" in
  *":$DEST:"*) ;;
  *) echo "note: $DEST is not on your PATH" ;;
esac
"$DEST/verifier" static-check --help >/dev/null && echo "self-test: ok"
echo "to check a paper on the local page, run: verifier web"
