#!/usr/bin/env bash
set -euo pipefail

apt_lock_timeout_seconds="${TINYHAT_APT_LOCK_TIMEOUT_SECONDS:-300}"

is_truthy() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

find_google_chrome() {
  command -v google-chrome-stable 2>/dev/null ||
    command -v google-chrome 2>/dev/null || true
}

chrome_bin="$(find_google_chrome)"
if [[ -n "$chrome_bin" ]]; then
  echo "install-google-chrome: Google Chrome is already installed: $($chrome_bin --version 2>/dev/null || printf 'version unavailable')"
  exit 0
fi

if is_truthy "${TINYHAT_SKIP_GOOGLE_CHROME:-0}"; then
  echo "install-google-chrome: skipping because TINYHAT_SKIP_GOOGLE_CHROME is set"
  exit 0
fi

if [[ "$(uname -s)" != "Linux" ]] || ! command -v apt-get >/dev/null 2>&1; then
  echo "install-google-chrome: Google Chrome installation is only supported on apt-based Linux Computers"
  exit 0
fi

if [[ "$(id -u)" != "0" ]]; then
  echo "install-google-chrome: installation requires root" >&2
  exit 1
fi

architecture=""
if command -v dpkg >/dev/null 2>&1; then
  architecture="$(dpkg --print-architecture 2>/dev/null || true)"
fi
if [[ -z "$architecture" ]]; then
  case "$(uname -m)" in
    x86_64|amd64) architecture="amd64" ;;
    aarch64|arm64) architecture="arm64" ;;
  esac
fi

case "$architecture" in
  amd64|arm64) ;;
  *)
    echo "install-google-chrome: unsupported Linux architecture: ${architecture:-unknown}" >&2
    exit 1
    ;;
esac

if ! command -v curl >/dev/null 2>&1; then
  echo "install-google-chrome: curl is required" >&2
  exit 1
fi

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

chrome_deb="$tmp_dir/google-chrome-stable.deb"
chrome_url="https://dl.google.com/linux/direct/google-chrome-stable_current_${architecture}.deb"

echo "install-google-chrome: downloading Google Chrome Stable for $architecture"
curl -fsSL -o "$chrome_deb" "$chrome_url"

export DEBIAN_FRONTEND="${DEBIAN_FRONTEND:-noninteractive}"
apt-get -o "DPkg::Lock::Timeout=${apt_lock_timeout_seconds}" update
apt-get -o "DPkg::Lock::Timeout=${apt_lock_timeout_seconds}" \
  install -y --no-install-recommends "$chrome_deb"

chrome_bin="$(find_google_chrome)"
if [[ -z "$chrome_bin" ]]; then
  echo "install-google-chrome: package installation finished, but Google Chrome was not found on PATH" >&2
  exit 1
fi

echo "install-google-chrome: installed $($chrome_bin --version 2>/dev/null || printf 'Google Chrome Stable')"
