#!/usr/bin/env bash
# Weekly space-efficient mirror of Nextcloud's user-data dir to the 5TB drive.
#
# The nightly natto-backup deliberately EXCLUDES /srv/nextcloud/data (too
# large to duplicate every night; see services/backup/backup.sh). This script
# keeps a single up-to-date mirror instead — one copy, rsync --delete, not
# seven rotating tarballs. After a migration off Google Drive, Nextcloud's
# data dir is the sole copy of that data, so this mirror is its real backup.
#
# Source:      /srv/nextcloud/data
# Destination: /mnt/media/backups/nextcloud-data/   (mirror, --delete)
#
# Run as root (the data dir is owned by the in-container www-data UID).
# Idempotent: re-running with no changes transfers nothing. No-op (exit 0)
# if Nextcloud isn't deployed on this host yet — so it's safe on the Pi.
#
# Usage:
#   sudo /usr/local/sbin/nextcloud-data-sync
# (or via nextcloud-data-sync.timer, weekly)

set -euo pipefail

SRC=/srv/nextcloud/data
DEST_DIR=/mnt/media/backups/nextcloud-data

if [[ $EUID -ne 0 ]]; then
  echo "Run as root or via sudo." >&2
  exit 1
fi

# Not-yet-deployed host (e.g. the current Pi): nothing to mirror. Succeed
# quietly so the timer doesn't flap before the Beelink cutover.
if [[ ! -d "$SRC" ]]; then
  echo "[nextcloud-data-sync] $SRC absent — Nextcloud not deployed here; nothing to do."
  exit 0
fi

if [[ ! -d /mnt/media/backups ]]; then
  echo "destination parent missing: /mnt/media/backups (is the 5TB drive mounted?)" >&2
  exit 1
fi
mkdir -p "$DEST_DIR"

# Trailing slash on SRC: copy the dir's *contents* into DEST_DIR.
# --delete so the mirror tracks deletions (it's a mirror, not an archive).
# -H preserves hardlinks Nextcloud may use; --numeric-ids keeps the www-data
# UID stable across the /srv → /mnt/media copy (both ext4).
rsync -aH --delete --numeric-ids --info=stats2 "$SRC/" "$DEST_DIR/"

echo "[nextcloud-data-sync] mirror updated: $DEST_DIR"
