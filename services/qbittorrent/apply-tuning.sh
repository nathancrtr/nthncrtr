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
# simultaneously in `downloading` / `stalledDL`. Each had only 3-9 connected
# peers (measured) — 500+ torrents × ~5 conn = ~2500, *over* the 2000 global
# cap — so connection allocation was the binding resource. Aggregate
# throughput collapsed to <1 MB/s against a 30 MiB/s ceiling.
#
# Queue 10 downloads at a time so each gets ~200 connections (matches
# max_connec_per_torrent). dont_count_slow_torrents is INTENTIONALLY FALSE: in
# this fleet's transient state every active torrent is below the 2 KB/s "slow"
# threshold, so `true` would mark every torrent slow → none counts toward the
# 10-slot cap → the cap nullifies itself (verified 2026-05-22: queuedDL=0 with
# `true`). With `false` the cap is hard, ~370 torrents move to queuedDL, the
# active 10 get the full connection budget and can actually ramp. Theoretical
# downside (10 dead-swarm torrents could block the queue) is mitigated by the
# 509-of-536 connected-peers observation — even "stalled" torrents have peers,
# just not enough. max_active_uploads=1000 keeps every seeder active so the
# previous "everything seeds" property is preserved — only downloads are
# queued.
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
#
# VPN-over-the-wire knobs — diagnosed 2026-05-23 against persistent low,
# high-variance per-peer throughput (top sender ~44 KB/s) despite a healthy
# tunnel (single-flow 31 MB/s to Hetzner DE; 4-flow aggregate 80 MB/s) and
# port-forwarding being open (external nc to the Proton-forwarded port
# succeeded). Confirmed root cause was uTP-over-WireGuard congestion
# collapse — see WORKLIST. Forcing TCP-only lifted aggregate from <1 MB/s
# to 2-7 MB/s in measured A/B.
#   bittorrent_protocol=1 (TCP only) — was 0 (TCP + uTP). uTP is UDP-based
#     and congestion-friendly, which means it backs off hard on the
#     micro-jitter the Proton WG path adds. The combination starves every
#     uTP flow to single-digit KB/s. Modern peers all support TCP fallback,
#     so we lose ~zero connectivity by disabling uTP.
#   peer_tos=0 — was 1. qBit's default-1 sets a "low cost" DSCP byte on
#     outbound packets; some upstream networks (Proton's egress included)
#     interpret it as "deprioritize." Zero = no marking, regular best-effort.
#   upload_choking_algorithm=2 (Anti-Leech) — was 1 (Fastest Upload). Fastest
#     Upload preferentially rewards peers we're *already* sending fast to,
#     which is the wrong policy when we're trying to bootstrap from a
#     mass-restore cold-start: we have no fast relationships yet. Anti-Leech
#     favors slot allocation to leechers, building ratio + reciprocity
#     across the swarm. Round-Robin (0) would be similarly fine; Anti-Leech
#     also actively penalizes leech-only clients which is a small bonus on
#     private trackers.
#
# Private-tracker leak defense + per-peer throughput — 2026-05-23 evening
# audit. Two clusters of knobs. Defense first:
#   dht=false / pex=false / lsd=false — was all true. Every torrent on this
#     box comes from Orpheus or BHD (both private), and libtorrent already
#     honors per-torrent `private` flag to suppress these. But: a single
#     accidentally-added public torrent (drag-and-drop, paste a wrong URL)
#     would happily enable DHT/PEX/LSD with these globally on. Off globally
#     removes that footgun; we lose nothing because no torrent here uses
#     them. Bonus: kills the steady ~33% idle CPU from DHT chatter (367
#     nodes routinely tracked).
# Throughput buffers — the libtorrent defaults are sized for an early-2000s
# DSL seedbox. We have a 31 MB/s single-flow VPN tunnel and a 30 MiB/s
# qBit upload cap, neither of which we ever come close to using. When a
# fast leecher does appear, undersized send/socket buffers cap the
# per-connection throughput well below the cap.
#   send_buffer_watermark=4000 (was 500 KB), send_buffer_low_watermark=200
#     (was 10 KB), send_buffer_watermark_factor=200 (was 50) — libtorrent
#     queues piece data ahead of the kernel write; with the defaults a
#     single connection's outflight is capped at ~500 KB. Bumping the
#     watermark + factor lets each connection have ~4 MB outflight, which
#     a fast leecher can actually consume.
#   socket_send_buffer_size=1048576 / socket_receive_buffer_size=1048576
#     (was 0=OS default ~200 KB) — sets SO_SNDBUF/SO_RCVBUF explicitly so
#     the TCP window can scale for the VPN's added RTT. Won't override the
#     kernel's tcp_wmem ceiling, but lifts the floor.
#   max_uploads_per_torrent=16 (was 8) — when two leechers land on the
#     same big movie, each gets a healthier slice. Bounded by max_uploads=100
#     globally, so this can't blow out the cap.
#   file_pool_size=256 (was 100) — qBit holds open file handles for
#     recently-accessed pieces; with 800+ torrents the LRU was thrashing
#     (peer asks for piece in already-closed file → reopen). 256 covers
#     normal working set.
#   disk_cache=1024 MB (was 512) — natto has 7 GB RAM and uses ~250 MB,
#     so the extra 512 MB is free. Helps the case where multiple peers
#     request the same hot piece — second read is from RAM, not the HDD.
#   save_resume_data_interval=600s (was 60) — every 60s qBit fsync'd resume
#     data for all 870+ torrents to /config/qBittorrent/BT_backup/, which
#     during the recheck batch was a non-trivial disk-write penalty.
#     Worst-case loss on unclean crash: ~10 min of newly-grabbed pieces'
#     bookkeeping, all recoverable on next recheck (the actual file data
#     was already fsync'd by libtorrent piece-write paths).
read -r -d '' PREFS_JSON <<'JSON' || true
{
  "queueing_enabled": true,
  "max_active_downloads": 10,
  "max_active_uploads": 1000,
  "max_active_torrents": 1010,
  "dont_count_slow_torrents": false,
  "slow_torrent_dl_rate_threshold": 2048,
  "slow_torrent_ul_rate_threshold": 2048,
  "slow_torrent_inactive_timer": 60,
  "bittorrent_protocol": 1,
  "peer_tos": 0,
  "upload_choking_algorithm": 2,
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
  "max_uploads_per_torrent": 16,
  "upnp": false,
  "random_port": false,
  "temp_path_enabled": true,
  "temp_path": "/incomplete",
  "disk_cache": 1024,
  "enable_piece_extent_affinity": true,
  "enable_coalesce_read_write": true,
  "hashing_threads": 4,
  "checking_memory_use": 128,
  "reannounce_when_address_changed": true,
  "dht": false,
  "pex": false,
  "lsd": false,
  "send_buffer_watermark": 4000,
  "send_buffer_low_watermark": 200,
  "send_buffer_watermark_factor": 200,
  "socket_send_buffer_size": 1048576,
  "socket_receive_buffer_size": 1048576,
  "file_pool_size": 256,
  "save_resume_data_interval": 600
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
