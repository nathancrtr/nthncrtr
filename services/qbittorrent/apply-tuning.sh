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
# Daytime (08:00-20:00):  alt limits kick in -> 15 down / 8 up, household-safe.
# Queueing OFF so every completed torrent seeds 24/7.
#   30 MiB/s = 31457280   15 MiB/s = 15728640   8 MiB/s = 8388608
# temp_path: in-progress pieces land on /incomplete (bind: /srv/qbit-incomplete,
# the SATA SSD) instead of /mnt/media (USB HDD on exfat), where the small
# random writes + per-piece fsyncs were capping aggregate downloads at ~10 MB/s
# regardless of how much VPN/peer throughput was available. On completion qBit
# moves the file to save_path on /mnt/media (cross-fs copy, USB-HDD-bound at
# ~10 MB/s, runs in the background and is what Radarr/Sonarr hardlink off).
read -r -d '' PREFS_JSON <<'JSON' || true
{
  "queueing_enabled": false,
  "dl_limit": 31457280,
  "up_limit": 31457280,
  "scheduler_enabled": true,
  "alt_dl_limit": 15728640,
  "alt_up_limit": 8388608,
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
  "temp_path": "/incomplete"
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
