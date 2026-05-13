#!/bin/sh
# Port-updater for qBittorrent behind Gluetun (Proton VPN).
#
# Proton's port-forwarding hands out a port that changes whenever Gluetun
# reconnects to the VPN. This script watches the forwarded-port file Gluetun
# maintains and pushes the current port to qBit's WebUI API when it changes,
# so qBittorrent is always listening on the right port for inbound peers.
#
# Runs in Gluetun's network namespace (network_mode: service:gluetun), so
# qBittorrent is reachable on localhost:8080. Requires qBit's
# "Bypass authentication for clients on localhost" to be enabled (Options
# → Web UI → Authentication) — since the sidecar appears to qBit as
# 127.0.0.1, it skips login.
set -eu

STATE_FILE="${STATE_FILE:-/state/forwarded_port}"
QBIT="${QBIT:-http://localhost:8080}"
INTERVAL="${POLL_INTERVAL:-60}"

log() { printf '%s qbit-port-updater: %s\n' "$(date -u +%FT%TZ)" "$*"; }

while [ ! -s "$STATE_FILE" ]; do
  log "waiting for $STATE_FILE..."
  sleep 5
done

while ! curl -fsS "$QBIT/api/v2/app/version" >/dev/null 2>&1; do
  log "waiting for qBittorrent WebUI..."
  sleep 5
done

log "starting (poll every ${INTERVAL}s, state=$STATE_FILE, qbit=$QBIT)"

last_pushed=""

while true; do
  port=$(cat "$STATE_FILE" 2>/dev/null || true)
  if [ -n "$port" ] && [ "$port" != "$last_pushed" ]; then
    if curl -fsS \
         --data-urlencode "json={\"listen_port\":$port,\"upnp\":false,\"random_port\":false}" \
         "$QBIT/api/v2/app/setPreferences" >/dev/null 2>&1; then
      log "pushed port $port to qBittorrent"
      last_pushed="$port"
    else
      log "WARN: failed to push port $port (is qBit localhost-bypass enabled? Options → Web UI → Authentication)"
    fi
  fi
  sleep "$INTERVAL"
done
