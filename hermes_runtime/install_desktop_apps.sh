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

install_browser_launcher() {
  browser_launcher_path="${TINYHAT_BROWSER_LAUNCHER_PATH:-/usr/local/bin/tinyhat-browser}"
  cat >"$browser_launcher_path" <<'BROWSER'
#!/usr/bin/env bash
set -euo pipefail

# Keep Chrome responsive on smaller Computers without disabling normal web
# capabilities. Operators can raise or remove the renderer limit at launch.
renderer_process_limit="${TINYHAT_CHROME_RENDERER_PROCESS_LIMIT:-4}"
for candidate in google-chrome-stable google-chrome chromium chromium-browser; do
  if command -v "$candidate" >/dev/null 2>&1; then
    exec "$candidate" \
      --no-sandbox \
      --disable-dev-shm-usage \
      --disable-background-mode \
      --disable-default-apps \
      --no-first-run \
      --no-default-browser-check \
      --disk-cache-size=67108864 \
      --media-cache-size=33554432 \
      --renderer-process-limit="$renderer_process_limit" \
      --user-data-dir=/root/.config/tinyhat-browser \
      "$@"
  fi
done
echo "No web browser is installed." >&2
exit 1
BROWSER
  chmod 0755 "$browser_launcher_path"
}

if is_truthy "${TINYHAT_SKIP_DESKTOP_APPS:-0}"; then
  echo "install-desktop-apps: skipping because TINYHAT_SKIP_DESKTOP_APPS is set"
  exit 0
fi

chrome_bin="$(find_google_chrome)"
thunar_bin="$(command -v thunar 2>/dev/null || true)"
skip_google_chrome=0
if is_truthy "${TINYHAT_SKIP_GOOGLE_CHROME:-0}"; then
  skip_google_chrome=1
fi

need_chrome=0
if [[ -z "$chrome_bin" && "$skip_google_chrome" -eq 0 ]]; then
  need_chrome=1
fi
need_thunar=0
if [[ -z "$thunar_bin" ]]; then
  need_thunar=1
fi

if [[ "$need_chrome" -eq 0 && "$need_thunar" -eq 0 ]]; then
  echo "install-desktop-apps: Google Chrome is already installed: $($chrome_bin --version 2>/dev/null || printf 'version unavailable')"
  echo "install-desktop-apps: Thunar is already installed: $($thunar_bin --version 2>/dev/null | head -n 1 || printf 'version unavailable')"
  install_browser_launcher
  exit 0
fi

if [[ "$(uname -s)" != "Linux" ]] || ! command -v apt-get >/dev/null 2>&1; then
  echo "install-desktop-apps: desktop application installation is only supported on apt-based Linux Computers"
  exit 0
fi

if [[ "$(id -u)" != "0" ]]; then
  echo "install-desktop-apps: installation requires root" >&2
  exit 1
fi

architecture=""
if [[ "$need_chrome" -eq 1 ]]; then
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
      echo "install-desktop-apps: unsupported Chrome architecture: ${architecture:-unknown}" >&2
      exit 1
      ;;
  esac
  if ! command -v curl >/dev/null 2>&1; then
    echo "install-desktop-apps: curl is required to install Google Chrome" >&2
    exit 1
  fi
fi

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

packages=()
if [[ "$need_thunar" -eq 1 ]]; then
  packages+=(thunar)
fi
if [[ "$need_chrome" -eq 1 ]]; then
  chrome_deb="$tmp_dir/google-chrome-stable.deb"
  chrome_url="https://dl.google.com/linux/direct/google-chrome-stable_current_${architecture}.deb"
  echo "install-desktop-apps: downloading Google Chrome Stable for $architecture"
  curl -fsSL -o "$chrome_deb" "$chrome_url"
  packages+=("$chrome_deb")
fi

export DEBIAN_FRONTEND="${DEBIAN_FRONTEND:-noninteractive}"
apt-get -o "DPkg::Lock::Timeout=${apt_lock_timeout_seconds}" update
apt-get -o "DPkg::Lock::Timeout=${apt_lock_timeout_seconds}" \
  install -y --no-install-recommends "${packages[@]}"

chrome_bin="$(find_google_chrome)"
thunar_bin="$(command -v thunar 2>/dev/null || true)"
if [[ "$skip_google_chrome" -eq 0 && -z "$chrome_bin" ]]; then
  echo "install-desktop-apps: package installation finished, but Google Chrome was not found on PATH" >&2
  exit 1
fi
if [[ -z "$thunar_bin" ]]; then
  echo "install-desktop-apps: package installation finished, but Thunar was not found on PATH" >&2
  exit 1
fi

if [[ -n "$chrome_bin" ]]; then
  install_browser_launcher
  echo "install-desktop-apps: Google Chrome ready: $($chrome_bin --version 2>/dev/null || printf 'version unavailable')"
fi
echo "install-desktop-apps: Thunar ready: $($thunar_bin --version 2>/dev/null | head -n 1 || printf 'version unavailable')"
