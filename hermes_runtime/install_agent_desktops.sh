#!/usr/bin/env bash
# Explicit image-build / owner-requested installation. Never runs on heartbeat.
set -euo pipefail

[[ "$(uname -s)" == Linux && "$(id -u)" == 0 ]] || {
  echo 'Agent desktop installation requires an apt-based Linux Computer and root.' >&2; exit 2;
}
source /etc/os-release
case "$ID:$VERSION_ID" in
  ubuntu:24.04|ubuntu:26.04|debian:13) ;;
  *) echo 'Use Ubuntu 24.04/26.04 or Debian 13 for both desktop apps.' >&2; exit 2 ;;
esac
arch="$(dpkg --print-architecture)"
[[ "$arch" == amd64 || "$arch" == arm64 ]] || exit 2
export DEBIAN_FRONTEND=noninteractive
apt_opts=(-o "DPkg::Lock::Timeout=${TINYHAT_APT_LOCK_TIMEOUT_SECONDS:-300}")
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if ! command -v chatgpt >/dev/null || ! command -v claude-desktop >/dev/null; then
  apt-get "${apt_opts[@]}" update
  apt-get "${apt_opts[@]}" install -y --no-install-recommends ca-certificates curl gnupg
fi
if ! command -v chatgpt >/dev/null; then
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --connect-timeout 30 --max-time 900 \
    "https://persistent.oaistatic.com/codex-app-prod/linux/deb/latest/chatgpt_${arch}.deb" \
    -o "$tmp/chatgpt.deb"
  [[ "$(dpkg-deb -f "$tmp/chatgpt.deb" Package)" == chatgpt ]]
  [[ "$(dpkg-deb -f "$tmp/chatgpt.deb" Architecture)" == "$arch" ]]
  apt-get "${apt_opts[@]}" install -y --no-install-recommends "$tmp/chatgpt.deb"
fi
if ! command -v claude-desktop >/dev/null; then
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --connect-timeout 30 --max-time 120 \
    https://downloads.claude.ai/claude-desktop/key.asc -o "$tmp/claude.asc"
  fingerprint="$(gpg --batch --with-colons --show-keys "$tmp/claude.asc" | awk -F: '$1 == "fpr" { print $10; exit }')"
  [[ "$fingerprint" == 31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE ]] || {
    echo 'Anthropic repository signing key did not match.' >&2; exit 1;
  }
  install -m 0644 "$tmp/claude.asc" /usr/share/keyrings/claude-desktop-archive-keyring.asc
  printf '%s\n' 'deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/claude-desktop-archive-keyring.asc] https://downloads.claude.ai/claude-desktop/apt/stable stable main' \
    > /etc/apt/sources.list.d/claude-desktop.list
  apt-get "${apt_opts[@]}" update
  apt-get "${apt_opts[@]}" install -y --no-install-recommends claude-desktop
fi
command -v chatgpt >/dev/null
command -v claude-desktop >/dev/null
dpkg-query -W -f='${Package} ${Version}\n' chatgpt claude-desktop
