#!/usr/bin/env bash
#
# Plain-English summary of what this installer does:
#
# 1. Reads the runtime ref to install. By default it installs channels/lts.
#    You can override that with TINYHAT_RUNTIME_REF, --ref, or --channel.
# 2. Reads the install locations. By default it writes program files under
#    /opt/tinyhat-hermes-runtime and runtime state under
#    /var/lib/tinyhat-hermes-runtime. You can override those with env vars or
#    --prefix / --state-dir.
# 3. Reads optional Tinyhat connection values: platform URL, computer id, and a
#    local-dev token. These are written only when explicitly provided. The
#    local-dev token is for local Docker-style development; production machine
#    authentication is handled by the runtime through cloud identity
#    attestation, not by this installer minting or storing a platform token.
# 4. Requires python3 and install. If it needs to download the runtime source,
#    it also requires curl and tar.
# 5. Gets the runtime source either from --source-dir, when you already have a
#    checkout, or by downloading the selected ref from
#    tinyloophub/tinyhat--runtimes--hermes as a GitHub tarball.
# 6. Stops immediately if the downloaded or supplied source does not contain
#    the hermes_runtime Python package.
# 7. Records the installed commit when it can. For a git checkout it reads
#    HEAD; for a downloaded ref it asks the GitHub commits API. If it cannot
#    resolve a commit, install still continues without COMMIT_SHA.
# 8. Creates the install directory, bin directory, state directory, and
#    state/current directory if they do not already exist.
# 9. Replaces only the installed hermes_runtime package under the install
#    prefix. It does not delete the runtime state directory.
# 10. Writes audit files: INSTALL_REF under the install prefix, VERSION under
#     state/current, and COMMIT_SHA under state/current when the commit is
#     known.
# 11. Writes an executable wrapper named tinyhat-hermes-runtime. The wrapper
#     sets PYTHONPATH, points the runtime at the state directory, and runs
#     python3 -m hermes_runtime.main.
# 12. Writes a private env file at <prefix>/env/runtime.env with mode 0600.
#     That file contains the runtime ref, state directory, and any optional
#     Tinyhat connection values that were passed to the installer.
# 13. Unless --no-systemd is passed, tries to install the runtime as a systemd
#     service on Linux. On non-systemd systems it leaves the files installed
#     and prints a message. On systemd systems it requires root.
# 14. When installing systemd as root, copies the private env file to
#     /etc/tinyhat/hermes-runtime.env, writes
#     /etc/systemd/system/tinyhat-hermes-runtime.service, reloads systemd, and
#     enables and starts tinyhat-hermes-runtime.service.
# 15. The systemd service restarts automatically, starts after the network is
#     online, runs with Nice=-5, and uses OOMScoreAdjust=-900 so the OS strongly
#     prefers keeping the heartbeat/runtime process alive.
# 16. This installer installs only the Tinyhat Hermes runtime process. It does
#     not install upstream Hermes Agent yet, create a Tinyhat Computer row, or
#     assign a Computer to an Agent.
set -euo pipefail

REPO_SLUG="tinyloophub/tinyhat--runtimes--hermes"
DEFAULT_REF="channels/lts"
DEFAULT_PREFIX="/opt/tinyhat-hermes-runtime"
DEFAULT_STATE_DIR="/var/lib/tinyhat-hermes-runtime"

runtime_ref="${TINYHAT_RUNTIME_REF:-$DEFAULT_REF}"
prefix="${TINYHAT_RUNTIME_PREFIX:-$DEFAULT_PREFIX}"
state_dir="${TINYHAT_RUNTIME_STATE_DIR:-$DEFAULT_STATE_DIR}"
source_dir="${TINYHAT_RUNTIME_SOURCE_DIR:-}"
platform_url="${TINYHAT_PLATFORM_URL:-}"
computer_id="${TINYHAT_COMPUTER_ID:-}"
local_dev_token="${TINYHAT_LOCAL_DEV_TOKEN:-}"
install_systemd=1

usage() {
  cat <<'USAGE'
Tinyhat Hermes runtime installer.

Usage:
  curl -fsSL https://raw.githubusercontent.com/tinyloophub/tinyhat--runtimes--hermes/channels/lts/install.sh | bash -s -- [options]

Options:
  --ref REF                 Runtime git ref to install. Defaults to channels/lts.
  --channel lts|latest      Shortcut for channels/lts or channels/latest.
  --source-dir PATH         Install from an already checked-out source tree.
  --prefix PATH             Install destination. Defaults to /opt/tinyhat-hermes-runtime.
  --state-dir PATH          Runtime state directory. Defaults to /var/lib/tinyhat-hermes-runtime.
  --platform-url URL        Tinyhat platform URL written to the runtime env file.
  --computer-id ID          Computer id written to the runtime env file.
  --local-dev-token TOKEN   Local-dev bearer token written to the runtime env file.
  --no-systemd              Do not install or restart the systemd unit.
  -h, --help                Show this help.

The installer installs only the Tinyhat runtime process. It does not install
upstream Hermes Agent yet.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref)
      runtime_ref="${2:?--ref requires a value}"
      shift 2
      ;;
    --channel)
      case "${2:?--channel requires a value}" in
        lts) runtime_ref="channels/lts" ;;
        latest) runtime_ref="channels/latest" ;;
        *)
          echo "install.sh: --channel must be lts or latest" >&2
          exit 2
          ;;
      esac
      shift 2
      ;;
    --source-dir)
      source_dir="${2:?--source-dir requires a value}"
      shift 2
      ;;
    --prefix)
      prefix="${2:?--prefix requires a value}"
      shift 2
      ;;
    --state-dir)
      state_dir="${2:?--state-dir requires a value}"
      shift 2
      ;;
    --platform-url)
      platform_url="${2:?--platform-url requires a value}"
      shift 2
      ;;
    --computer-id)
      computer_id="${2:?--computer-id requires a value}"
      shift 2
      ;;
    --local-dev-token)
      local_dev_token="${2:?--local-dev-token requires a value}"
      shift 2
      ;;
    --no-systemd)
      install_systemd=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "install.sh: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "install.sh: missing required command: $1" >&2
    exit 1
  }
}

need_cmd python3
need_cmd install

resolve_ref_sha() {
  local ref="$1"
  python3 - "$REPO_SLUG" "$ref" <<'PY' || true
import json
import sys
from urllib import error, parse, request

repo = sys.argv[1]
ref = sys.argv[2]
url = f"https://api.github.com/repos/{repo}/commits/{parse.quote(ref, safe='')}"
req = request.Request(url, headers={"Accept": "application/vnd.github+json"})
try:
    with request.urlopen(req, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
except (error.URLError, json.JSONDecodeError, TimeoutError):
    raise SystemExit(0)
sha = str(payload.get("sha") or "").strip() if isinstance(payload, dict) else ""
if sha:
    print(sha)
PY
}

tmp_dir=""
cleanup() {
  if [[ -n "$tmp_dir" && -d "$tmp_dir" ]]; then
    rm -rf "$tmp_dir"
  fi
}
trap cleanup EXIT

if [[ -n "$source_dir" ]]; then
  src="$source_dir"
else
  need_cmd curl
  need_cmd tar
  tmp_dir="$(mktemp -d)"
  src="$tmp_dir/src"
  mkdir -p "$src"
  tarball_url="https://codeload.github.com/$REPO_SLUG/tar.gz/$runtime_ref"
  echo "install.sh: downloading Tinyhat Hermes runtime from $tarball_url"
  curl -fsSL "$tarball_url" | tar -xz -C "$src" --strip-components=1
fi

if [[ ! -d "$src/hermes_runtime" ]]; then
  echo "install.sh: hermes_runtime package not found in $src" >&2
  exit 1
fi

runtime_sha=""
if command -v git >/dev/null 2>&1 && git -C "$src" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  runtime_sha="$(git -C "$src" rev-parse --verify HEAD 2>/dev/null || true)"
fi
if [[ -z "$runtime_sha" && -z "$source_dir" ]]; then
  runtime_sha="$(resolve_ref_sha "$runtime_ref")"
fi

echo "install.sh: installing Tinyhat Hermes runtime ref $runtime_ref"
install -d "$prefix" "$prefix/bin" "$state_dir" "$state_dir/current"
rm -rf "$prefix/hermes_runtime"
cp -R "$src/hermes_runtime" "$prefix/hermes_runtime"
printf '%s\n' "$runtime_ref" > "$prefix/INSTALL_REF"
printf '%s\n' "$runtime_ref" > "$state_dir/current/VERSION"
if [[ -n "$runtime_sha" ]]; then
  printf '%s\n' "$runtime_sha" > "$state_dir/current/COMMIT_SHA"
else
  rm -f "$state_dir/current/COMMIT_SHA"
fi

cat > "$prefix/bin/tinyhat-hermes-runtime" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="$prefix:\${PYTHONPATH:-}"
export TINYHAT_RUNTIME_STATE_DIR="\${TINYHAT_RUNTIME_STATE_DIR:-$state_dir}"
exec python3 -m hermes_runtime.main
EOF
chmod 0755 "$prefix/bin/tinyhat-hermes-runtime"

env_dir="$prefix/env"
install -d -m 0700 "$env_dir"
env_file="$env_dir/runtime.env"
umask 077
{
  printf 'TINYHAT_RUNTIME_REF=%q\n' "$runtime_ref"
  printf 'TINYHAT_RUNTIME_STATE_DIR=%q\n' "$state_dir"
  if [[ -n "$platform_url" ]]; then
    printf 'TINYHAT_PLATFORM_URL=%q\n' "$platform_url"
  fi
  if [[ -n "$computer_id" ]]; then
    printf 'TINYHAT_COMPUTER_ID=%q\n' "$computer_id"
  fi
  if [[ -n "$local_dev_token" ]]; then
    printf 'TINYHAT_LOCAL_DEV_TOKEN=%q\n' "$local_dev_token"
  fi
} > "$env_file"
chmod 0600 "$env_file"

if [[ "$install_systemd" -eq 1 ]]; then
  if [[ "$(uname -s)" != "Linux" ]] || ! command -v systemctl >/dev/null 2>&1; then
    echo "install.sh: systemd unavailable; installed files only"
  elif [[ "$(id -u)" != "0" ]]; then
    echo "install.sh: systemd install requires root; rerun as root or pass --no-systemd" >&2
    exit 1
  else
    service_env="/etc/tinyhat/hermes-runtime.env"
    install -d -m 0700 /etc/tinyhat
    cp "$env_file" "$service_env"
    chmod 0600 "$service_env"
    cat > /etc/systemd/system/tinyhat-hermes-runtime.service <<EOF
[Unit]
Description=Tinyhat Hermes Runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$service_env
ExecStart=$prefix/bin/tinyhat-hermes-runtime
Restart=always
RestartSec=2
Nice=-5
OOMScoreAdjust=-900

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now tinyhat-hermes-runtime.service
    echo "install.sh: systemd service tinyhat-hermes-runtime.service is enabled"
  fi
fi

echo "install.sh: installed Tinyhat Hermes runtime into $prefix"
