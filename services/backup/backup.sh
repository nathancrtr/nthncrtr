#!/usr/bin/env bash
# Snapshot natto's stateful bits to a dated tarball on the 5TB drive.
#
# Sources:
#   /srv/                          — Pi-hole, Navidrome, Homepage data + their compose files
#   /usr/local/bin/caddy           — the built Caddy binary
#   /etc/caddy/Caddyfile           — Caddy routing config
#   /etc/caddy/caddy.env           — Cloudflare API token (acceptable: the
#                                     tarball stays on the local-only drive)
#   /etc/systemd/system/caddy.service
#
# Destination:
#   /mnt/media/backups/natto-YYYY-MM-DD.tgz
#
# Run as root (needed to read /etc/caddy/caddy.env, mode 0600). Idempotent:
# re-running on the same day overwrites that day's tarball.
#
# Usage:
#   sudo /usr/local/sbin/natto-backup
# (or via the natto-backup.timer for daily runs at 03:30)

set -euo pipefail

DEST_DIR=/mnt/media/backups
DEST="$DEST_DIR/natto-$(date +%F).tgz"

SOURCES=(
  /srv
  /usr/local/bin/caddy
  /etc/caddy/Caddyfile
  /etc/caddy/caddy.env
  /etc/systemd/system/caddy.service
)

# --- preflight ---------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  echo "Run as root or via sudo." >&2
  exit 1
fi

for src in "${SOURCES[@]}"; do
  if [[ ! -e "$src" ]]; then
    echo "missing source: $src" >&2
    exit 1
  fi
done

if [[ ! -d "$DEST_DIR" ]]; then
  echo "destination dir missing: $DEST_DIR (mkdir it first)" >&2
  exit 1
fi
if ! touch "$DEST_DIR/.write-test" 2>/dev/null; then
  echo "destination not writable: $DEST_DIR" >&2
  exit 1
fi
rm -f "$DEST_DIR/.write-test"

# Free-space check: require at least the size of the source set, plus 10% headroom.
src_kb=$(du -sk --total "${SOURCES[@]}" 2>/dev/null | tail -1 | awk '{print $1}')
free_kb=$(df --output=avail -k "$DEST_DIR" | tail -1)
need_kb=$(( src_kb * 110 / 100 ))
if (( free_kb < need_kb )); then
  echo "not enough free space: have ${free_kb}KB, need ${need_kb}KB" >&2
  exit 1
fi

# --- archive -----------------------------------------------------------------
# tar -P keeps absolute paths so restore is just `tar -xzf ... -C /`.
# Atomic write: build into .partial, rename on success.
tmp="$DEST.partial"
trap 'rm -f "$tmp"' EXIT

tar -czPf "$tmp" "${SOURCES[@]}"
mv -f "$tmp" "$DEST"
trap - EXIT

bytes=$(stat -c%s "$DEST")
printf '[backup] wrote %s (%d bytes)\n' "$DEST" "$bytes"
