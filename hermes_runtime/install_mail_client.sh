#!/usr/bin/env bash
set -euo pipefail

case "${TINYHAT_SKIP_DESKTOP_APPS:-0}" in
  1|true|TRUE|yes|YES|on|ON) exit 0 ;;
esac
if [[ "$(uname -s)" != Linux ]] || ! command -v apt-get >/dev/null 2>&1; then
  echo "install-mail-client: supported on apt-based Linux Computers"
  exit 0
fi
[[ "$(id -u)" == 0 ]] || { echo "install-mail-client: root required" >&2; exit 1; }

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
apt_args=(-o "DPkg::Lock::Timeout=${TINYHAT_APT_LOCK_TIMEOUT_SECONDS:-300}")
export DEBIAN_FRONTEND="${DEBIAN_FRONTEND:-noninteractive}"
# Use Mozilla's native package, avoiding Ubuntu's Snap stub on headless images.
if [[ ! -x /usr/lib/thunderbird/thunderbird ]]; then
  apt-get "${apt_args[@]}" update
  apt-get "${apt_args[@]}" install -y --no-install-recommends curl ca-certificates gnupg
  install -d -m 0755 /etc/apt/keyrings
  key_file="$(mktemp)"
  trap 'rm -f "$key_file"' EXIT
  curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg -o "$key_file"
  fingerprint="$(gpg --show-keys --with-colons "$key_file" | awk -F: '$1 == "fpr" {print $10; exit}')"
  [[ "$fingerprint" == 35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3 ]] || {
    echo "install-mail-client: Mozilla signing key mismatch" >&2; exit 1;
  }
  install -m 0644 "$key_file" /etc/apt/keyrings/packages.mozilla.org.asc
  printf '%s\n' 'deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt thunderbird-deb main' > /etc/apt/sources.list.d/mozilla-thunderbird.list
  printf '%s\n' 'Package: thunderbird*' 'Pin: origin packages.mozilla.org' 'Pin-Priority: 1000' > /etc/apt/preferences.d/mozilla-thunderbird
  apt-get "${apt_args[@]}" update
  candidate="$(apt-cache policy thunderbird | awk '/Candidate:/ {print $2; exit}')"
  if [[ -z "$candidate" || "$candidate" == '(none)' || "$candidate" == *snap* ]]; then
    echo "install-mail-client: a native Thunderbird package is unavailable for this distribution/architecture; refusing the Snap stub" >&2
    exit 1
  fi
  apt-get "${apt_args[@]}" install -y --no-install-recommends thunderbird
fi
[[ -x /usr/lib/thunderbird/thunderbird ]] || { echo "install-mail-client: native Thunderbird missing" >&2; exit 1; }
install -d -m 0755 /usr/lib/thunderbird/defaults/pref /usr/local/bin
install -m 0644 "$source_dir/tinyhat-mail.cfg" /usr/lib/thunderbird/tinyhat-mail.cfg
cat > /usr/lib/thunderbird/defaults/pref/tinyhat-mail.js <<'CONFIG'
pref("general.config.filename", "tinyhat-mail.cfg");
pref("general.config.obscure_value", 0);
pref("general.config.sandbox_enabled", false);
CONFIG
cat > /usr/local/bin/tinyhat-mail <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail
export TINYHAT_MAIL_SETTINGS_FILE="$HOME/.config/tinyhat/mail/settings.json"
if [[ ! -f "$TINYHAT_MAIL_SETTINGS_FILE" ]]; then
  echo "Your Tinyhat mailbox is still being set up. Try again shortly." >&2
  exit 1
fi
exec /usr/lib/thunderbird/thunderbird -profile "$HOME/.config/tinyhat/mail/profile" -mail "$@"
LAUNCHER
chmod 0755 /usr/local/bin/tinyhat-mail
echo "install-mail-client: Thunderbird ready"
