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

# Nextcloud's bulk data and the live MariaDB datadir are excluded from the
# file tar: hot-copying an InnoDB datadir yields an unrestorable archive, and
# the user data is too large to duplicate nightly. Instead we dump the DB
# logically (below) into /srv/nextcloud/db-dump.sql.gz, which IS under /srv
# and so IS captured; the user data is mirrored weekly by
# nextcloud-data-sync.timer. See services/nextcloud/README.md.
EXCLUDES=(
  --exclude=/srv/nextcloud/data
  --exclude=/srv/nextcloud/db
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

# --- Nextcloud logical DB dump ------------------------------------------------
# Only if Nextcloud is actually deployed here (the current Pi has no such
# container, so this is a no-op there). A dump failure is loud but non-fatal:
# it must not sink the whole nightly backup of every other service.
if command -v docker >/dev/null 2>&1 \
   && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx nextcloud-db; then
  nc_dump=/srv/nextcloud/db-dump.sql.gz
  nc_tmp="$nc_dump.partial"
  if docker exec nextcloud-db sh -c \
       'exec mariadb-dump --single-transaction --quick --default-character-set=utf8mb4 -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' \
       2>/dev/null | gzip > "$nc_tmp"; then
    mv -f "$nc_tmp" "$nc_dump"
    printf '[backup] wrote Nextcloud DB dump %s (%d bytes)\n' "$nc_dump" "$(stat -c%s "$nc_dump")"
  else
    rm -f "$nc_tmp"
    echo "[backup] WARNING: Nextcloud DB dump failed — tarball will carry the previous dump (if any)" >&2
  fi
fi

# Free-space check: require at least the size of the source set, plus 10%
# headroom. EXCLUDES are applied here too so the estimate isn't inflated by
# the (excluded) Nextcloud data/datadir.
src_kb=$(du -sk --total "${EXCLUDES[@]}" "${SOURCES[@]}" 2>/dev/null | tail -1 | awk '{print $1}')
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

tar "${EXCLUDES[@]}" -czPf "$tmp" "${SOURCES[@]}"
mv -f "$tmp" "$DEST"
trap - EXIT

bytes=$(stat -c%s "$DEST")
printf '[backup] wrote %s (%d bytes)\n' "$DEST" "$bytes"
