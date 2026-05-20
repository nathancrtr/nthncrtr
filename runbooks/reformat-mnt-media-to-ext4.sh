#!/usr/bin/env bash
# Reformat /mnt/media from exfat (USB HDD) to ext4, restoring music/video
# from the operator's Feral seedbox at cottus.feralhosting.com.
#
# WHY: exfat on /mnt/media is the chronic ceiling on this host:
#   - no hardlink support — *arrs always copy on import (every 4K release
#     doubles /mnt/media usage and locks the disk for ~30 min)
#   - no journal — kernel exfat_write_failed/__exfat_truncate events under
#     memory pressure cause silent torrent write retries (diagnosed
#     2026-05-20)
#   - poor random IOPS — capped seeding throughput at ~6 MB/s and downloads
#     at ~10 MB/s, regardless of network
# ext4 + noatime,nodiratime fixes all three.
#
# REQUIREMENTS: operator's Feral slot at cottus.feralhosting.com must
# contain the canonical music + movies + tv archive (verified via this
# script's --inventory phase). Operator's slot expires within days; this
# migration must complete before that.
#
# USAGE:
#   sudo ./reformat-mnt-media-to-ext4.sh --phase=NAME [--dry-run]
#
# Phases (run in order):
#   inventory   compare what's on natto vs what's on Feral; prints diff
#   stop        stop services that touch /mnt/media (qBit, Jellyfin,
#               Navidrome, Sonarr, Radarr, Prowlarr) + disable scheduled
#               jobs (natto-backup.timer, nextcloud-data-sync.timer)
#   wipe        umount /mnt/media → mkfs.ext4 → mount → update fstab
#               (PROMPTS for explicit confirmation; --yes-wipe to skip)
#   transfer    lftp mirror music + movies + tv from Feral → /mnt/media
#               (10-14 hours; designed to run inside tmux)
#   start       restart services, trigger library rescans
#   resnatch    re-add any qBit torrents whose data didn't survive
#               (uses /srv/qbittorrent/config/qBittorrent/BT_backup/)
#
# DESIGN NOTES:
#   - All phases are idempotent. Re-running a partially-completed phase
#     resumes from where it left off (lftp mirror is incremental; service
#     state changes are checked before re-applying).
#   - Operator owns /home/nthncrtr/.ssh/id_ed25519_feral which is the
#     key authorized on Feral. ~/.ssh/config has a `cottus` alias.
#   - rsync 3.4.1 (natto) is incompatible with rsync 3.1.2 (Feral); we
#     use sftp via lftp to avoid this. --parallel=4 gave 64 MB/s aggregate
#     in the 2026-05-20 benchmark — sized for that ceiling.
#   - The 5TB USB HDD's identity is preserved (device name, partition
#     layout). Only the *filesystem* on /dev/sda2 changes.
#
# ROLLBACK: there is no rollback past the `wipe` phase — the exfat
# filesystem is destroyed by mkfs. The Feral copy IS the only remaining
# source of truth until restore completes. Do not run `wipe` without
# verifying `inventory` first.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEVICE=/dev/sda2
MOUNTPOINT=/mnt/media
LABEL=media
MOUNT_OPTS=defaults,noatime,nodiratime
FERAL_ALIAS=cottus   # configured in /home/nthncrtr/.ssh/config
# Feral's nthncrtr home is at /media/zdn/nthncrtr/ (shared-host slot layout,
# not /home/nthncrtr/) — discovered 2026-05-20 when inventory phase failed
# with "No such file or directory" on /home/nthncrtr/...
FERAL_ARCHIVE_ROOT='/media/zdn/nthncrtr/data-from-nthncrtr@tethys'
LFTP_PARALLEL=4
LFTP_PGET_N=1
LOG=/var/log/reformat-mnt-media.log
SCRIPT_USER=nthncrtr
SCRIPT_GROUP=nthncrtr

# Services that hold open files on /mnt/media. Stopped at `stop`, started
# at `start`. Order matters on start (qbittorrent depends on gluetun being
# already up; gluetun is NOT stopped because it doesn't touch /mnt/media).
SERVICES_HALT=(
  qbittorrent
  jellyfin
  navidrome-navidrome-1
  sonarr
  radarr
  prowlarr
  # homepage bind-mounts /mnt/media for its free-space widget; Docker
  # keeps the bind alive while the container runs, which pins sda2 enough
  # that mkfs.ext4 refuses with "apparently in use" (caught 2026-05-20).
  # Stopping homepage releases the bind; restart in phase_start.
  homepage
)
# Systemd timers that may write to /mnt/media on a schedule.
TIMERS_DISABLE=(
  natto-backup.timer
  nextcloud-data-sync.timer
)

DRY_RUN=0
YES_WIPE=0
PHASE=""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { local m="[$(date +%H:%M:%S)] $*"; echo "$m"; echo "$m" >> "$LOG" 2>/dev/null || true; }
die()  { log "ERROR: $*"; exit 1; }
run()  { log "+ $*"; (( DRY_RUN )) || eval "$@"; }
note() { log "NOTE: $*"; }

require_root() {
  [[ $EUID -eq 0 ]] || die "Run as root (or via sudo). Current uid=$EUID."
}

require_feral_alias() {
  sudo -u "$SCRIPT_USER" ssh -o BatchMode=yes -o ConnectTimeout=5 "$FERAL_ALIAS" \
    "echo feral-ssh-ok" >/dev/null 2>&1 \
    || die "ssh ${FERAL_ALIAS} (as ${SCRIPT_USER}) is broken. Fix ~/.ssh/config + id_ed25519_feral first."
}

# ---------------------------------------------------------------------------
# Phase: inventory
# ---------------------------------------------------------------------------
phase_inventory() {
  log "=== INVENTORY ==="
  require_feral_alias
  log "natto /mnt/media top-level layout:"
  ls -la "$MOUNTPOINT" | tee -a "$LOG"
  log ""
  log "natto subdirectory file counts (slow on busy exfat, be patient):"
  for d in "$MOUNTPOINT"/music "$MOUNTPOINT"/video/movies "$MOUNTPOINT"/video/tv; do
    [[ -d "$d" ]] && log "  $d: $(find "$d" -type f 2>/dev/null | wc -l) files"
  done
  log ""
  log "feral source layout:"
  sudo -u "$SCRIPT_USER" ssh "$FERAL_ALIAS" "ls -la $FERAL_ARCHIVE_ROOT/music $FERAL_ARCHIVE_ROOT/private/rtorrent/data 2>&1 | head -10" | tee -a "$LOG"
  log ""
  log "feral file counts (this takes a few minutes — Feral disk is busy):"
  for fpath in \
      "$FERAL_ARCHIVE_ROOT/music" \
      "$FERAL_ARCHIVE_ROOT/private/rtorrent/data/movies" \
      "$FERAL_ARCHIVE_ROOT/private/rtorrent/data/tv"; do
    cnt=$(sudo -u "$SCRIPT_USER" ssh "$FERAL_ALIAS" "find $fpath -type f 2>/dev/null | wc -l")
    log "  $fpath: $cnt files"
  done
  log ""
  log "feral total size (one du, may take ~5 min):"
  sudo -u "$SCRIPT_USER" ssh "$FERAL_ALIAS" "du -sh $FERAL_ARCHIVE_ROOT/music $FERAL_ARCHIVE_ROOT/private/rtorrent/data/movies $FERAL_ARCHIVE_ROOT/private/rtorrent/data/tv 2>/dev/null" | tee -a "$LOG"
}

# ---------------------------------------------------------------------------
# Phase: stop
# ---------------------------------------------------------------------------
phase_stop() {
  log "=== STOP services + timers ==="
  for svc in "${SERVICES_HALT[@]}"; do
    if docker ps --format '{{.Names}}' | grep -qx "$svc"; then
      run "docker stop $svc"
    else
      note "$svc not running"
    fi
  done
  for t in "${TIMERS_DISABLE[@]}"; do
    if systemctl is-enabled "$t" >/dev/null 2>&1; then
      run "systemctl stop $t; systemctl disable $t"
    else
      note "$t not enabled"
    fi
  done
  log "remaining processes holding /mnt/media open:"
  fuser -mv "$MOUNTPOINT" 2>&1 | tee -a "$LOG" || true
}

# ---------------------------------------------------------------------------
# Phase: wipe (DESTRUCTIVE — requires --yes-wipe)
# ---------------------------------------------------------------------------
phase_wipe() {
  log "=== WIPE — destructive ==="
  if (( ! YES_WIPE )); then
    log "REFUSING to proceed without --yes-wipe."
    log "This will: umount $MOUNTPOINT → mkfs.ext4 on $DEVICE → mount."
    log "There is no rollback after this. The exfat filesystem is destroyed."
    log ""
    log "Re-run with --yes-wipe to proceed."
    exit 1
  fi
  log "snapshotting current fstab line for $MOUNTPOINT:"
  grep -E "($DEVICE|UUID=[0-9a-f-]+) +$MOUNTPOINT " /etc/fstab | tee -a "$LOG" || note "no fstab line for $MOUNTPOINT"
  log ""
  if mountpoint -q "$MOUNTPOINT"; then
    run "umount $MOUNTPOINT" \
      || die "umount $MOUNTPOINT failed. Check fuser -mv $MOUNTPOINT and try again."
  else
    note "$MOUNTPOINT already unmounted"
  fi
  log "device check:"
  blkid "$DEVICE" | tee -a "$LOG" || note "no existing fs label on $DEVICE"
  log ""
  log "mkfs.ext4 on $DEVICE (label=$LABEL) — this takes 10-30 min on USB HDD..."
  run "mkfs.ext4 -F -L $LABEL -E lazy_itable_init=1,lazy_journal_init=1 $DEVICE"
  log "new fs:"
  blkid "$DEVICE" | tee -a "$LOG"
  log ""
  log "updating /etc/fstab — replacing exfat line with ext4 line:"
  local newuuid
  newuuid=$(blkid -o value -s UUID "$DEVICE")
  [[ -z "$newuuid" ]] && die "couldn't read new UUID from $DEVICE"
  (( DRY_RUN )) || {
    # Remove any old line for this mountpoint
    sed -i.bak "\,^[^#].* $MOUNTPOINT ,d" /etc/fstab
    echo "UUID=$newuuid  $MOUNTPOINT  ext4  $MOUNT_OPTS  0  2" >> /etc/fstab
  }
  grep -E "$MOUNTPOINT|$newuuid" /etc/fstab | tee -a "$LOG"
  log ""
  log "mounting via fstab:"
  run "mount $MOUNTPOINT"
  log ""
  log "creating directory layout:"
  # `install -d` only sets ownership on the leaf, so for nested paths we
  # have to create each level explicitly — otherwise the intermediate dirs
  # (video/, _unsorted/) get the default root:root from sudo (caught
  # 2026-05-20). Shallow-first ordering.
  for d in music video video/movies video/tv _unsorted _unsorted/torrents backups; do
    run "install -d -o $SCRIPT_USER -g $SCRIPT_GROUP -m 0755 $MOUNTPOINT/$d"
  done
  log ""
  log "final state:"
  df -hT "$MOUNTPOINT" | tee -a "$LOG"
  ls -la "$MOUNTPOINT" | tee -a "$LOG"
}

# ---------------------------------------------------------------------------
# Phase: transfer (the long one — 10-14h)
# ---------------------------------------------------------------------------
phase_transfer() {
  log "=== TRANSFER (Feral → natto via lftp mirror) ==="
  require_feral_alias
  command -v lftp >/dev/null 2>&1 || run "apt-get update && apt-get install -y lftp"
  mountpoint -q "$MOUNTPOINT" || die "$MOUNTPOINT is not mounted"
  [[ "$(stat -c%T -f "$MOUNTPOINT")" == "ext2/ext3" ]] || note "warning: $MOUNTPOINT is not ext4 (got: $(stat -c%T -f "$MOUNTPOINT"))"

  # Subshell each phase so we can resume individually. lftp's `mirror -c`
  # continues partial transfers; re-running is safe.
  local PAIRS=(
    "$FERAL_ARCHIVE_ROOT/music|$MOUNTPOINT/music"
    "$FERAL_ARCHIVE_ROOT/private/rtorrent/data/movies|$MOUNTPOINT/video/movies"
    "$FERAL_ARCHIVE_ROOT/private/rtorrent/data/tv|$MOUNTPOINT/video/tv"
  )
  for pair in "${PAIRS[@]}"; do
    local src="${pair%|*}" dst="${pair#*|}"
    log "MIRROR  $src  →  $dst"
    log "  start: $(date)"
    if (( DRY_RUN )); then
      log "  (dry-run; skipping)"
      continue
    fi
    # ssh options notes:
    #  -i pins the key (the dedicated passphrase-less natto→Feral one)
    #  BatchMode=yes prevents ANY interactive prompt — if key auth fails,
    #    ssh exits with an error instead of falling through to a password
    #    prompt (caught 2026-05-20: previous version did fall through and
    #    a Password: prompt appeared in tmux, into which something got
    #    typed; that's now treated as compromised)
    #  StrictHostKeyChecking=yes requires the host key to already be in
    #    UserKnownHostsFile — we added cottus's keys to nthncrtr's
    #    known_hosts during setup
    #  UserKnownHostsFile is given explicitly because sudo -u may not
    #    propagate HOME, leaving ssh searching /root/.ssh which has
    #    none of the cottus credentials
    # Using the full hostname (not the cottus alias) eliminates the
    # ~/.ssh/config lookup dependency for the same HOME reason.
    sudo -u "$SCRIPT_USER" lftp \
      -e "set sftp:connect-program 'ssh -a -x -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/home/$SCRIPT_USER/.ssh/known_hosts -i /home/$SCRIPT_USER/.ssh/id_ed25519_feral'; \
          set net:max-retries 3; \
          set net:reconnect-interval-base 30; \
          set mirror:use-pget-n $LFTP_PGET_N; \
          set mirror:parallel-transfer-count $LFTP_PARALLEL; \
          mirror -c --verbose=1 --no-perms --parallel=$LFTP_PARALLEL '$src' '$dst'; \
          bye" \
      "sftp://${SCRIPT_USER}:@cottus.feralhosting.com" 2>&1 \
      | tee -a "$LOG"
      # NOTE the literal ':' between user and host above. lftp parses URLs
      # like sftp://user:password@host; passing the empty password (':' with
      # nothing after) suppresses lftp's interactive "Password:" prompt for
      # its credential cache. ssh handles all actual auth via the key, so
      # the empty password value is never used for anything. Without the
      # colon, lftp would prompt — which hangs forever in detached tmux.
    log "  end:   $(date)"
  done
  log ""
  log "final file counts on natto:"
  for d in music video/movies video/tv; do
    log "  $MOUNTPOINT/$d: $(find "$MOUNTPOINT/$d" -type f 2>/dev/null | wc -l) files, $(du -sh "$MOUNTPOINT/$d" 2>/dev/null | awk '{print $1}')"
  done
}

# ---------------------------------------------------------------------------
# Phase: start (re-enable services)
# ---------------------------------------------------------------------------
phase_start() {
  log "=== START services + timers ==="
  for t in "${TIMERS_DISABLE[@]}"; do
    run "systemctl enable --now $t"
  done
  # gluetun is already up; qbittorrent comes back via deploy.sh's compose_up
  # which is idempotent and validates the temp_path mount.
  for svc in "${SERVICES_HALT[@]}"; do
    if docker ps -a --format '{{.Names}}' | grep -qx "$svc"; then
      run "docker start $svc"
    fi
  done
  log "wait for qBit WebUI to bind (signals safe to proceed):"
  for _ in $(seq 1 24); do
    if curl -sf -o /dev/null -m 4 http://127.0.0.1:8080/api/v2/app/version; then
      log "  qBit WebUI ready"
      break
    fi
    sleep 5
  done
  log "Library re-scans must be triggered MANUALLY in each service UI:"
  log "  Jellyfin:  Dashboard → Libraries → Scan All Libraries"
  log "  Navidrome: Settings → Scan Library Now"
  log "  Sonarr/Radarr: usually auto-scans on RefreshMonitoredDownloads"
}

# ---------------------------------------------------------------------------
# Phase: resnatch (re-add missing torrents via qBit BT_backup)
# ---------------------------------------------------------------------------
phase_resnatch() {
  log "=== RESNATCH ==="
  log "qBit BT_backup contains the .torrent files for every torrent qBit"
  log "has ever known about. The qbit-bulk-add.sh script in"
  log "/srv/qbittorrent/ takes a directory of .torrents and adds them via"
  log "the WebUI API (dedupe by infohash, so safe to re-run)."
  log ""
  log "Procedure:"
  log "  sudo /srv/qbittorrent/qbit-bulk-add.sh --dir /srv/qbittorrent/config/qBittorrent/BT_backup --limit 1"
  log "  # observe one torrent gets added cleanly"
  log "  sudo /srv/qbittorrent/qbit-bulk-add.sh --dir /srv/qbittorrent/config/qBittorrent/BT_backup"
  log ""
  log "qBit will then recheck each torrent against the restored files."
  log "Anything that recheck-fails (file not on Feral) will need to be"
  log "re-downloaded from the tracker."
}

# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase=*)    PHASE="${1#--phase=}"; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --yes-wipe)   YES_WIPE=1; shift ;;
    -h|--help)    sed -n '1,/^set -euo/p' "$0" | head -n -1 | sed 's/^# //;s/^#//'; exit 0 ;;
    *)            die "unknown arg: $1" ;;
  esac
done

require_root
mkdir -p "$(dirname "$LOG")"
touch "$LOG"

case "$PHASE" in
  inventory) phase_inventory ;;
  stop)      phase_stop ;;
  wipe)      phase_wipe ;;
  transfer)  phase_transfer ;;
  start)     phase_start ;;
  resnatch)  phase_resnatch ;;
  "")        die "no --phase= specified. See --help." ;;
  *)         die "unknown phase: $PHASE" ;;
esac
