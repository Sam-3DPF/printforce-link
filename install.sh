#!/usr/bin/env bash
#
# PrintForce Link installer for macOS.
#
# Don't run this by hand — get your personalized command from 3D Print Force:
#   Integrations -> PrintForce Link -> "Get install command"
# It looks like:
#   curl -fsSL https://raw.githubusercontent.com/Sam-3DPF/printforce-link/main/install.sh | bash -s -- <PAIR_CODE> <DPF_URL>
#
# The whole script is wrapped in main() and called on the last line, so a truncated
# download fails to parse instead of half-running.
set -euo pipefail

main() {
  local PAIR_TOKEN="${1:-}"
  local DPF_URL="${2:-https://app.3dprintforce.com}"
  local REPO="Sam-3DPF/printforce-link"
  local ROOT="$HOME/.printforce-link"
  local LABEL="com.3dprintforce.printforce-link"
  local PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

  say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
  warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
  die()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

  [ "$(uname -s)" = "Darwin" ] || die "This installer is for macOS. On Windows, use the PowerShell command instead."
  command -v curl   >/dev/null || die "curl is required (it ships with macOS)."
  command -v tar    >/dev/null || die "tar is required (it ships with macOS)."
  command -v shasum >/dev/null || die "shasum is required (it ships with macOS)."
  [ -n "$PAIR_TOKEN" ] || die "No pairing code. Copy the whole command from 3D Print Force (Integrations -> PrintForce Link -> Get install command)."

  local arch; arch="$(uname -m)"                 # arm64 (Apple Silicon) or x86_64 (Intel)
  [ "$arch" = "arm64" ] || die "PrintForce Link currently supports Apple Silicon Macs (M1/M2/M3/M4) only. Intel Mac support is coming soon — reach out to 3D Print Force if you need it."
  local asset="printforce-link-macos-${arch}.tar.gz"
  local base="https://github.com/${REPO}/releases/latest/download"

  say "Downloading PrintForce Link for $arch..."
  local tmp; tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fSL "$base/$asset"    -o "$tmp/$asset"      || die "Download failed — check your internet connection and try again."
  curl -fSL "$base/SHA256SUMS" -o "$tmp/SHA256SUMS" || die "Download failed — check your internet connection and try again."

  say "Verifying the download..."
  ( cd "$tmp" && grep " ${asset}\$" SHA256SUMS | shasum -a 256 -c - ) >/dev/null 2>&1 \
    || die "The download didn't verify. Please run the command again."

  say "Installing..."
  mkdir -p "$ROOT"
  rm -rf "$ROOT/printforce-link"
  tar -xzf "$tmp/$asset" -C "$ROOT"                # -> $ROOT/printforce-link/
  # Downloading with curl sets no quarantine, but strip it defensively in case of MDM.
  xattr -dr com.apple.quarantine "$ROOT/printforce-link" 2>/dev/null || true

  # config.toml holds only the 3DPF URL; the agent stores its own credential + printer
  # codes in printers.json (chmod 600). Keep an existing config on reinstall.
  if [ ! -f "$ROOT/config.toml" ]; then
    printf 'dpf_base_url = "%s"\n' "$DPF_URL" > "$ROOT/config.toml"
    chmod 600 "$ROOT/config.toml"
  fi

  say "Setting it to start automatically and stay running..."
  mkdir -p "$HOME/Library/LaunchAgents"
  local exe="$ROOT/printforce-link/printforce-link"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key><array><string>${exe}</string></array>
  <key>WorkingDirectory</key><string>${ROOT}</string>
  <key>EnvironmentVariables</key><dict><key>BRIDGE_PAIR_TOKEN</key><string>${PAIR_TOKEN}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${ROOT}/printforce-link.log</string>
  <key>StandardErrorPath</key><string>${ROOT}/printforce-link.log</string>
</dict>
</plist>
PLIST
  chmod 600 "$PLIST"                               # the pair code (one-time, then inert) lives here

  launchctl bootout   "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"      || die "Could not start the agent. Try again, or see the troubleshooting steps in 3D Print Force."

  say "Done! PrintForce Link is installed and connecting."
  say "Go back to 3D Print Force — the PrintForce Link card turns green, then you can add your printers."
  say "Log file (if you ever need it): $ROOT/printforce-link.log"
}

main "$@"
