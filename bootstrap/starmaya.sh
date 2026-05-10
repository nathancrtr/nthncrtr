#!/usr/bin/env bash
# Idempotent bootstrap for starmaya — the host that runs the Behmor roasting
# profiler. Installs the roaster system user, /var/lib/roaster, the udev rule
# for /dev/behmor-arduino, and the roaster-{daemon,web} systemd units. Verifies
# Node.js is present at the version the units need.
#
# Run as root or via sudo on a fresh Debian/Ubuntu host. The script does NOT
# install Node.js (the systemd units pin /usr/bin/node, and your Node source
# is your call — NodeSource, nvm, distro package, etc.) and does NOT deploy
# the Starmaya application code (separate repo; deploy to /opt/starmaya/).
#
# Usage:
#   sudo bootstrap/starmaya.sh

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root or via sudo." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVC_DIR="$REPO_ROOT/services/starmaya"

# --- Node.js sanity check ----------------------------------------------------
# The systemd units invoke /usr/bin/node. We don't care which apt source it
# came from, but it has to exist and be a recent-enough major version.
NODE_REQUIRED_MAJOR=22
if ! command -v node >/dev/null 2>&1; then
  echo "node is not installed. Install Node.js >=${NODE_REQUIRED_MAJOR} from your preferred source" >&2
  echo "(NodeSource: https://github.com/nodesource/distributions) and re-run." >&2
  exit 1
fi
NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
if (( NODE_MAJOR < NODE_REQUIRED_MAJOR )); then
  echo "node is v${NODE_MAJOR}; need >=${NODE_REQUIRED_MAJOR}." >&2
  exit 1
fi

# --- roaster user + group ----------------------------------------------------
if ! getent group roaster >/dev/null; then
  groupadd --system roaster
fi
if ! id -u roaster >/dev/null 2>&1; then
  useradd --system --gid roaster --home-dir /var/lib/roaster \
    --shell /usr/sbin/nologin roaster
fi

# --- /var/lib/roaster (SQLite DB) -------------------------------------------
install -d -o roaster -g roaster -m 0755 /var/lib/roaster

# --- udev rule ---------------------------------------------------------------
install -o root -g root -m 0644 \
  "$SVC_DIR/99-behmor-arduino.rules" \
  /etc/udev/rules.d/99-behmor-arduino.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=tty

# --- systemd units -----------------------------------------------------------
install -o root -g root -m 0644 \
  "$SVC_DIR/roaster-daemon.service" \
  /etc/systemd/system/roaster-daemon.service
install -o root -g root -m 0644 \
  "$SVC_DIR/roaster-web.service" \
  /etc/systemd/system/roaster-web.service
systemctl daemon-reload

# --- summary ----------------------------------------------------------------
cat <<'EOF'

Bootstrap complete.

Next steps:
  1. Deploy the Starmaya application code to /opt/starmaya/. The systemd units
     expect built artifacts at:
       /opt/starmaya/packages/daemon/dist/main.js
       /opt/starmaya/packages/server/dist/main.js
     /opt/starmaya should be readable by the roaster user.
  2. Plug in the Behmor's Arduino. Verify the udev rule:
       ls -l /dev/behmor-arduino
  3. Enable + start the services:
       systemctl enable --now roaster-daemon.service roaster-web.service
       systemctl status roaster-daemon.service roaster-web.service
EOF
