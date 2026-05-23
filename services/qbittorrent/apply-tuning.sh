#!/usr/bin/env bash
# Re-assert qBittorrent's seedbox tuning (queueing, rate limits, scheduler,
# connection limits) declaratively.
#
# qBit owns qBittorrent.conf and rewrites it at will, so we can't template it.
# Instead this script is the source of truth: it POSTs the desired prefs to
# qBit's WebUI setPreferences API. Idempotent — re-running with no drift is a
# harmless no-op (qBit just re-accepts the same values). deploy.sh runs this
# at the end of deploy_qbittorrent so every deploy re-asserts the tuning, and
# it survives a /srv/qbittorrent/config wipe.
#
# Runs as a one-shot curl container inside gluetun's network namespace, so
# qBit sees 127.0.0.1 and its "localhost auth bypass" applies — no password,
# nothing crosses the docker bridge (same pattern as qbit-bulk-add.sh).
#
# The intended values are documented in README.md (§ Seedbox tuning). Change
# them THERE and HERE together; the table in the README is the human-readable
# mirror of the JSON below.
#
# Usage:
#   sudo ./apply-tuning.sh            # apply
#   sudo ./apply-tuning.sh --show     # print current qBit prefs, change nothing

set -euo pipefail

SHOW=false
[[ "${1:-}" == "--show" ]] && SHOW=true

# --- Desired seedbox tuning -------------------------------------------------
# Off-peak (20:00-08:00): full 30 MiB/s up + down.
# Daytime (08:00-20:00):  alt download limit kicks in (15 MiB/s, household-safe).
# Daytime upload limit matches off-peak (30 MiB/s) because the actual upload
# ceiling is /mnt/media's USB-HDD random-read rate (~6 MB/s measured 2026-05-20)
# — qBit can't push past that regardless of the cap, so an "8 MiB/s daytime"
# cap doesn't gain household-DNS headroom; it just leaves ratio on the floor.
# Revisit after the NVMe upgrade lifts the disk-read ceiling.
#
# Queueing ON (changed 2026-05-22). Was off, on the theory that "every completed
# torrent seeds 24/7" needs no queueing. True in steady state — but after the
# Orpheus + BHD mass-restores the library now has 800+ torrents, with hundreds
# in `downloading` / `stalledDL` simultaneously. With private trackers (no
# DHT/PEX) and old/dead Orpheus swarms, those hundreds of in-flight downloads
# split the 2000-conn global cap into ~3-4 peers each and sum to <1 MB/s
# aggregate. The fix is to *queue* downloads: max_active_downloads=10 plus
# dont_count_slow_torrents=true so a stalled dead-swarm torrent doesn't squat
# in one of those 10 slots — it parks itself after 60s below 2 KB/s, freeing
# the slot for a healthy one. max_active_uploads=1000 keeps all seeders active
# (the previous "queueing off → all seed forever" guarantee is preserved).
#   30 MiB/s = 31457280   15 MiB/s = 15728640   8 MiB/s = 8388608
# temp_path: in-progress pieces land on /incomplete (bind: /srv/qbit-incomplete,
# the SATA SSD) instead of /mnt/media (USB HDD on exfat), where the small
# random writes + per-piece fsyncs were capping aggregate downloads at ~10 MB/s
# regardless of how much VPN/peer throughput was available. On completion qBit
# moves the file to save_path on /mnt/media (cross-fs copy, USB-HDD-bound at
# ~10 MB/s, runs in the background — and note that Radarr/Sonarr cannot
# hardlink off the result either, because /mnt/media is exfat and exfat has
# no hardlink support; both *arrs silently fall back to copy too).
#
# Advanced libtorrent knobs — diagnosed 2026-05-20 as a chronic disk-thrash
# situation. The defaults assume a fast local SSD; on USB-HDD+exfat they
# starve the seeder:
#   disk_cache=512 (MB) — was -1 (auto, ~64 MB). Keeps hot pieces in RAM so
#     popular seeds don't pound the disk on every leecher request. We have
#     ~5 GB available RAM after vm.swappiness=10 stops the swap thrash.
#   enable_piece_extent_affinity — serves piece requests in extent order
#     instead of arrival order; turns scattered random reads into more
#     sequential ones. Single biggest HDD-seeder knob in libtorrent.
#   enable_coalesce_read_write — merges adjacent small I/Os into larger ones.
#   hashing_threads=4 (was 2) — faster post-restart recheck on our 4-core
#     host so torrents return to seeding sooner.
#   checking_memory_use=128 MB (was 32) — bigger recheck buffer, fewer
#     re-reads when verifying.
#   reannounce_when_address_changed=true — when Proton rotates the forwarded
#     port (~every few hours), reannounce immediately so trackers serve
#     leechers the current port instead of the stale one. Critical for
#     ratio: a stale tracker entry means leechers fail to connect to us
#     until the next scheduled announce (could be 30+ min).
read -r -d '' PREFS_JSON <<'JSON' || true
{
  "queueing_enabled": true,
  "max_active_downloads": 10,
  "max_active_uploads": 1000,
  "max_active_torrents": 1010,
  "dont_count_slow_torrents": true,
  "slow_torrent_dl_rate_threshold": 2048,
  "slow_torrent_ul_rate_threshold": 2048,
  "slow_torrent_inactive_timer": 60,
  "dl_limit": 31457280,
  "up_limit": 31457280,
  "scheduler_enabled": true,
  "alt_dl_limit": 15728640,
  "alt_up_limit": 31457280,
  "schedule_from_hour": 8,
  "schedule_from_min": 0,
  "schedule_to_hour": 20,
  "schedule_to_min": 0,
  "scheduler_days": 0,
  "max_connec": 2000,
  "max_connec_per_torrent": 200,
  "max_uploads": 100,
  "max_uploads_per_torrent": 8,
  "upnp": false,
  "random_port": false,
  "temp_path_enabled": true,
  "temp_path": "/incomplete",
  "disk_cache": 512,
  "enable_piece_extent_affinity": true,
  "enable_coalesce_read_write": true,
  "hashing_threads": 4,
  "checking_memory_use": 128,
  "reannounce_when_address_changed": true
}
JSON
# ---------------------------------------------------------------------------

if ! docker ps --filter name=^gluetun$ --filter status=running --format '{{.Names}}' | grep -q '^gluetun$'; then
  echo "gluetun container is not running; aborting" >&2
  exit 1
fi

if [[ "$SHOW" == true ]]; then
  docker run --rm --network "container:gluetun" curlimages/curl:latest \
    -sS "http://127.0.0.1:8080/api/v2/app/preferences"
  echo
  exit 0
fi

# Compact the JSON to a single line for the form-encoded body.
PREFS_ONE=$(printf '%s' "$PREFS_JSON" | tr -d '\n' | tr -s ' ')
echo "[apply-tuning] asserting seedbox prefs via qBit WebUI API"

docker run --rm \
  --network "container:gluetun" \
  -e PREFS="$PREFS_ONE" \
  curlimages/curl:latest \
  sh -c '
    set -u
    code=$(curl -sS -o /dev/null -w "%{http_code}" \
      -X POST "http://127.0.0.1:8080/api/v2/app/setPreferences" \
      --data-urlencode "json=$PREFS") || code="000"
    if [ "$code" = "200" ]; then
      echo "[apply-tuning] ok (http=200)"
    else
      echo "[apply-tuning] FAILED (http=$code) — is qBit localhost-bypass enabled?" >&2
      exit 1
    fi
  '
