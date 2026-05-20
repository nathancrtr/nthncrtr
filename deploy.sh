#!/usr/bin/env bash
#
# deploy.sh — push this repo's services to their runtime locations on natto
# (and kvass for starmaya). Idempotent: re-running with no repo change is a
# no-op for everything except `docker compose up -d` (itself a no-op when
# nothing changed).
#
# Run on natto: ssh -t natto, then
#   cd /srv/nthncrtr-repo && git pull && sudo ./deploy.sh [opts] [services...]
#
# One-time bootstrap (not handled here): git clone this repo to
# /srv/nthncrtr-repo and put a read-only deploy key at /root/.ssh/.
#
# Services: caddy navidrome backup qbittorrent radarr sonarr prowlarr homepage nextcloud jellyfin immich cloudflared authelia pihole starmaya
# Default (no service args): caddy navidrome backup qbittorrent radarr sonarr prowlarr homepage nextcloud jellyfin immich cloudflared
#   — homepage is AFTER the *arrs/qBittorrent on purpose: its widgets reach
#     them over those compose projects' (external) docker networks, which
#     only exist once those projects have come up. Steady-state re-deploys
#     are fine in any order (the nets persist with the running containers).
#   — cloudflared exists only because Jellyfin is public (WORKLIST 6.6):
#     it's the Cloudflare Tunnel public path (GFiber can't port-forward —
#     see services/cloudflared/README.md). Default-on; harmless before the
#     tunnel is provisioned. (ddns and fail2ban were both removed in the
#     6.6 pivots — a tunnel needs no WAN-IP A record, and brute-force
#     protection is a Cloudflare dashboard Rate-Limiting rule, not a
#     container — see WORKLIST 6.6 / services/jellyfin/README.md.)
#   — pihole is gated behind --yes-pihole (DNS outage for ~30s).
#   — starmaya must be requested explicitly (deploys to kvass over ssh).
#   — authelia must be requested explicitly (the SSO gate; deploy it
#     BEFORE caddy on first stand-up or the gated sites 502 — see below).
#
# Options:
#   --dry-run         Show diffs and intended actions; change nothing.
#   --yes-pihole      Permit pihole deploy in this run.
#   -h, --help        Show usage.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
YES_PIHOLE=0

usage() {
  cat <<'EOF'
Usage: sudo ./deploy.sh [--dry-run] [--yes-pihole] [services...]

Services: caddy navidrome backup qbittorrent radarr sonarr prowlarr homepage nextcloud jellyfin immich cloudflared pihole starmaya
Default (no service args): caddy navidrome backup qbittorrent radarr sonarr prowlarr homepage nextcloud jellyfin immich cloudflared
EOF
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)    DRY_RUN=1; shift ;;
    --yes-pihole) YES_PIHOLE=1; shift ;;
    -h|--help)    usage 0 ;;
    --)           shift; break ;;
    -*)           echo "unknown option: $1" >&2; usage 1 ;;
    *)            break ;;
  esac
done

SERVICES=("$@")
if [[ ${#SERVICES[@]} -eq 0 ]]; then
  SERVICES=(caddy navidrome backup qbittorrent radarr sonarr prowlarr homepage nextcloud jellyfin immich cloudflared)
  (( YES_PIHOLE )) && SERVICES+=(pihole)
fi

if (( ! DRY_RUN )) && [[ $EUID -ne 0 ]]; then
  echo "deploy.sh must run as root (try: sudo $0 $*)" >&2
  exit 1
fi

# ---- helpers --------------------------------------------------------------

log()  { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*" >&2; }
err()  { printf '\033[1;31mxx  %s\033[0m\n' "$*" >&2; }

# install_file <src> <dst> [<mode>=0644] [<owner:group>=root:root]
# Skips the install if files are byte-identical. Sets CHANGED=1 in the
# caller's scope (via bash dynamic scoping) if the file differs.
install_file() {
  local src=$1 dst=$2 mode=${3:-0644} owner=${4:-root:root}
  [[ -f $src ]] || { err "missing source: $src"; return 1; }
  if [[ -f $dst ]] && cmp -s "$src" "$dst"; then
    note "unchanged: $dst"
    return 0
  fi
  CHANGED=1
  if (( DRY_RUN )); then
    if [[ ! -f $dst ]]; then
      note "would create: $dst (mode=$mode owner=$owner, $(wc -c <"$src") bytes)"
    else
      note "would update: $dst (mode=$mode owner=$owner)"
      diff -u "$dst" "$src" 2>/dev/null | sed 's/^/      /' | head -40 || true
    fi
    return 0
  fi
  install -m "$mode" -o "${owner%:*}" -g "${owner#*:}" "$src" "$dst"
  note "installed: $dst"
}

compose_up() {
  local svc=$1
  (( DRY_RUN )) && { note "would: (cd /srv/$svc && docker compose up -d)"; return 0; }
  (cd "/srv/$svc" && docker compose up -d)
}

# arrnet is the shared *arr-stack docker network (Prowlarr ↔ Sonarr/Radarr).
# It's external (declared `external: true` in the prowlarr/sonarr/radarr
# compose files), so the compose `up` calls expect it to exist. Subnet is
# pinned to keep it deterministic and out of the way of the other compose
# defaults already in use (172.17–172.28). No-op when present.
ensure_arrnet() {
  (( DRY_RUN )) && {
    docker network inspect arrnet >/dev/null 2>&1 \
      || note "would: docker network create arrnet --subnet 172.29.0.0/16"
    return 0
  }
  docker network inspect arrnet >/dev/null 2>&1 && return 0
  docker network create arrnet --subnet 172.29.0.0/16 --label managed-by=nthncrtr-repo >/dev/null
  note "created docker network: arrnet (172.29.0.0/16)"
}

verify_url() {
  local url=$1 want=${2:-200} code
  code=$(curl -sSL -o /dev/null -m 10 -w '%{http_code}' "$url" 2>/dev/null || echo "000")
  if [[ $code == "$want" ]]; then
    note "$url → HTTP $code"
  else
    warn "$url → HTTP $code (wanted $want)"
    return 1
  fi
}

check_repo_state() {
  command -v git >/dev/null || return 0
  [[ -d $REPO_ROOT/.git ]] || return 0
  if ! git -C "$REPO_ROOT" diff --quiet HEAD 2>/dev/null; then
    warn "repo has uncommitted changes; deploying current working tree as-is"
    git -C "$REPO_ROOT" status -s 2>/dev/null | sed 's/^/      /' >&2
  fi
}

# ---- services -------------------------------------------------------------

deploy_caddy() {
  log "caddy"
  # Syntax check first. `caddy adapt` won't touch live state and won't try to
  # provision TLS (which would require CF_API_TOKEN). `caddy validate` would.
  local caddy_err_file
  caddy_err_file=$(mktemp)
  if ! caddy adapt --adapter caddyfile --config "$REPO_ROOT/services/caddy/Caddyfile" >/dev/null 2>"$caddy_err_file"; then
    err "caddy adapt failed; aborting caddy deploy (live config untouched)"
    sed 's/^/      /' "$caddy_err_file" >&2
    rm -f "$caddy_err_file"
    return 1
  fi
  rm -f "$caddy_err_file"
  local CHANGED=0
  install_file "$REPO_ROOT/services/caddy/Caddyfile" /etc/caddy/Caddyfile 0644 caddy:caddy
  local caddyfile_changed=$CHANGED
  CHANGED=0
  install_file "$REPO_ROOT/services/caddy/caddy.service" /etc/systemd/system/caddy.service
  local unit_changed=$CHANGED
  (( DRY_RUN )) && return 0
  if (( unit_changed )); then
    systemctl daemon-reload
    note "systemctl daemon-reload"
  fi
  if (( caddyfile_changed || unit_changed )); then
    systemctl reload caddy
    note "systemctl reload caddy"
  fi
  systemctl is-active --quiet caddy || { err "caddy not active after reload"; return 1; }
  note "caddy active"
}

deploy_navidrome() {
  log "navidrome"
  local CHANGED=0
  install_file "$REPO_ROOT/services/navidrome/docker-compose.yml" /srv/navidrome/docker-compose.yml
  compose_up navidrome
  (( DRY_RUN )) && return 0
  sleep 3
  verify_url https://music.nthncrtr.com 200 || true
}

deploy_homepage() {
  log "homepage"
  local CHANGED=0
  install_file "$REPO_ROOT/services/homepage/docker-compose.yml" /srv/homepage/docker-compose.yml
  # rsync config/ WITHOUT --delete: runtime state (logs/) and homepage's
  # auto-generated stub yamls (kubernetes.yaml, proxmox.yaml) live there and
  # are not in the repo. Clobbering them would surprise the operator.
  # --checksum so identical-content-but-different-mtime doesn't false-positive
  # (the repo's mtimes don't match what was deployed; content usually does).
  local rsync_flags=(-a --checksum --no-perms --no-owner --no-group)
  (( DRY_RUN )) && rsync_flags+=(-n -v)
  rsync "${rsync_flags[@]}" "$REPO_ROOT/services/homepage/config/" /srv/homepage/config/ | sed 's/^/    /'
  compose_up homepage
  (( DRY_RUN )) && return 0
  sleep 3
  verify_url https://home.nthncrtr.com 200 || true
}

deploy_qbittorrent() {
  log "qbittorrent"
  local CHANGED=0
  install_file "$REPO_ROOT/services/qbittorrent/docker-compose.yml" /srv/qbittorrent/docker-compose.yml
  install_file "$REPO_ROOT/services/qbittorrent/port-updater.sh"    /srv/qbittorrent/port-updater.sh 0755
  install_file "$REPO_ROOT/services/qbittorrent/orpheus-restore.py"  /srv/qbittorrent/orpheus-restore.py 0755
  install_file "$REPO_ROOT/services/qbittorrent/orpheus-plan.py"     /srv/qbittorrent/orpheus-plan.py 0755
  install_file "$REPO_ROOT/services/qbittorrent/qbit-bulk-add.sh"    /srv/qbittorrent/qbit-bulk-add.sh 0755
  install_file "$REPO_ROOT/services/qbittorrent/apply-tuning.sh"     /srv/qbittorrent/apply-tuning.sh 0755
  (( DRY_RUN )) && return 0
  # qBit config lives at /srv/qbittorrent/config; downloads land under /mnt/media
  # (mounted directly so Radarr/Sonarr can hardlink final files instead of copying).
  # gluetun-state is a bind mount shared between gluetun (writer) and the
  # qbit-port-updater sidecar (reader). Pre-create with nthncrtr ownership
  # so gluetun (UID 1000 in-container) can write forwarded_port to it.
  [[ -d /srv/qbittorrent/config ]] || {
    install -d -o nthncrtr -g nthncrtr -m 0755 /srv/qbittorrent/config
    note "created /srv/qbittorrent/config"
  }
  [[ -d /srv/qbittorrent/gluetun-state ]] || {
    install -d -o nthncrtr -g nthncrtr -m 0755 /srv/qbittorrent/gluetun-state
    note "created /srv/qbittorrent/gluetun-state"
  }
  [[ -d /mnt/media/_unsorted/torrents ]] || {
    install -d -o nthncrtr -g nthncrtr -m 0755 /mnt/media/_unsorted/torrents
    note "created /mnt/media/_unsorted/torrents"
  }
  # Fast staging area for qBit's in-progress pieces, bind-mounted into the
  # container at /incomplete. Lives on the root SATA SSD, decoupling small
  # random torrent writes (which cap exfat-on-USB-HDD at ~10 MB/s) from the
  # archival /mnt/media. apply-tuning.sh sets temp_path_enabled/temp_path
  # to match.
  [[ -d /srv/qbit-incomplete ]] || {
    install -d -o nthncrtr -g nthncrtr -m 0755 /srv/qbit-incomplete
    note "created /srv/qbit-incomplete"
  }
  # Warn if secrets.env is missing — gluetun will start without it but won't
  # establish a tunnel, and qbittorrent (network_mode: service:gluetun) will
  # have no network at all.
  if [[ ! -f /srv/qbittorrent/secrets.env ]]; then
    warn "/srv/qbittorrent/secrets.env not found — see services/qbittorrent/README.md"
    warn "qBittorrent will have no network until Proton VPN credentials are provisioned."
  fi
  compose_up qbittorrent
  # qBit writes its container hostname+PID into config/qBittorrent/lockfile
  # and leaves a config/qBittorrent/ipc-socket. A compose recreate (this
  # happens on any gluetun/qbit change) gives the new container a fresh
  # hostname; if the prior qBit didn't remove its lockfile on shutdown
  # (unclean stop, OOM, host reboot, or being killed inside gluetun's netns),
  # the new qBit can't verify the recorded PID across a *different* hostname,
  # assumes another instance is already running, forwards its CLI args over
  # the stale ipc-socket and exits 0 — a silent ~1s crash-loop where the
  # WebUI never binds and the log shows only start/terminate (root cause of
  # the 2026-05-18 multi-hour qBit outage). qBit removes these on a clean
  # exit, so their presence after the WebUI fails to come up == stale.
  # Self-heal: gate on real WebUI readiness (also fixes the old blind
  # `sleep 5` that made apply-tuning race a not-yet-bound qBit), and if it
  # never binds, clear the stale pair and restart once.
  qbit_ready() { curl -sf -o /dev/null -m 4 http://127.0.0.1:8080/api/v2/app/version; }
  for _ in $(seq 1 12); do qbit_ready && break; sleep 3; done
  if ! qbit_ready; then
    warn "qBit WebUI did not bind — clearing stale lockfile/ipc-socket, restarting"
    rm -f /srv/qbittorrent/config/qBittorrent/ipc-socket \
          /srv/qbittorrent/config/qBittorrent/lockfile
    docker restart qbittorrent >/dev/null 2>&1 || true
    for _ in $(seq 1 12); do qbit_ready && break; sleep 3; done
  fi
  # Verify the local WebUI (the true qBit-up signal). NOT the public URL:
  # torrent.nthncrtr.com is Authelia-gated, so `curl -L` there just follows
  # the 302 to the auth portal and returns 200 even when qBit is dead — a
  # useless health check post-Authelia-cutover.
  if qbit_ready; then
    note "qBit WebUI up (127.0.0.1:8080)"
  else
    warn "qBit WebUI STILL down — investigate; see services/qbittorrent/README.md § stale-lock"
  fi
  # Re-assert seedbox tuning (queueing/rate/scheduler/conn limits). qBit owns
  # qBittorrent.conf, so this API-driven script is the version-controlled
  # source of truth; idempotent, safe to run every deploy. Only meaningful
  # once the WebUI is up (gated above).
  if /srv/qbittorrent/apply-tuning.sh; then
    note "seedbox tuning asserted"
  else
    warn "apply-tuning.sh failed — qBit prefs may be unmanaged (check localhost-bypass)"
  fi
  # A compose change to gluetun OR qbittorrent recreates the whole VPN+qBit
  # stack (shared netns). An *arr grab issued just before this — not yet
  # past metadata on a private tracker (no DHT/PEX) — has no resume data,
  # does not survive the recreate, and Sonarr/Radarr silently drop it back
  # to "missing" (no queue item, no retry). They have no auto-recovery.
  # See services/qbittorrent/README.md § "*arr grabs vs stack restarts".
  warn "qBit stack (re)deployed — in-flight *arr grabs may have been orphaned."
  warn "  Re-run Wanted→Missing search in Sonarr/Radarr to recover them."
}

deploy_radarr() {
  log "radarr"
  local CHANGED=0
  (( DRY_RUN )) || {
    # Ensure /srv/radarr/ and /srv/radarr/config exist before install_file
    # tries to write into them. /srv/radarr/ is not created by bootstrap until
    # it is re-run; on first deploy on an existing natto the dir may be absent.
    [[ -d /srv/radarr ]]        || { install -d -o nthncrtr -g nthncrtr -m 0755 /srv/radarr;        note "created /srv/radarr"; }
    [[ -d /srv/radarr/config ]] || { install -d -o nthncrtr -g nthncrtr -m 0755 /srv/radarr/config; note "created /srv/radarr/config"; }
  }
  install_file "$REPO_ROOT/services/radarr/docker-compose.yml" /srv/radarr/docker-compose.yml
  (( DRY_RUN )) && return 0
  ensure_arrnet
  compose_up radarr
  sleep 3
  verify_url https://radarr.nthncrtr.com 200 || true
}

deploy_sonarr() {
  log "sonarr"
  local CHANGED=0
  (( DRY_RUN )) || {
    [[ -d /srv/sonarr ]]        || { install -d -o nthncrtr -g nthncrtr -m 0755 /srv/sonarr;        note "created /srv/sonarr"; }
    [[ -d /srv/sonarr/config ]] || { install -d -o nthncrtr -g nthncrtr -m 0755 /srv/sonarr/config; note "created /srv/sonarr/config"; }
  }
  install_file "$REPO_ROOT/services/sonarr/docker-compose.yml" /srv/sonarr/docker-compose.yml
  (( DRY_RUN )) && return 0
  ensure_arrnet
  compose_up sonarr
  sleep 3
  verify_url https://sonarr.nthncrtr.com 200 || true
}

deploy_prowlarr() {
  log "prowlarr"
  local CHANGED=0
  (( DRY_RUN )) || {
    [[ -d /srv/prowlarr ]]        || { install -d -o nthncrtr -g nthncrtr -m 0755 /srv/prowlarr;        note "created /srv/prowlarr"; }
    [[ -d /srv/prowlarr/config ]] || { install -d -o nthncrtr -g nthncrtr -m 0755 /srv/prowlarr/config; note "created /srv/prowlarr/config"; }
  }
  install_file "$REPO_ROOT/services/prowlarr/docker-compose.yml" /srv/prowlarr/docker-compose.yml
  (( DRY_RUN )) && return 0
  ensure_arrnet
  compose_up prowlarr
  sleep 3
  verify_url https://prowlarr.nthncrtr.com 200 || true
}

deploy_nextcloud() {
  log "nextcloud"
  local CHANGED=0
  (( DRY_RUN )) || {
    # Parent + the three bind targets. Created root-owned and empty; the
    # nextcloud (www-data) and mariadb (mysql) images chown their own
    # subtrees on first init. Tailscale-only — no Caddyfile route to deploy.
    [[ -d /srv/nextcloud ]]      || { install -d -o root -g root -m 0755 /srv/nextcloud;      note "created /srv/nextcloud"; }
    [[ -d /srv/nextcloud/html ]] || { install -d -o root -g root -m 0755 /srv/nextcloud/html; note "created /srv/nextcloud/html"; }
    [[ -d /srv/nextcloud/data ]] || { install -d -o root -g root -m 0755 /srv/nextcloud/data; note "created /srv/nextcloud/data"; }
    [[ -d /srv/nextcloud/db ]]   || { install -d -o root -g root -m 0755 /srv/nextcloud/db;   note "created /srv/nextcloud/db"; }
  }
  install_file "$REPO_ROOT/services/nextcloud/docker-compose.yml" /srv/nextcloud/docker-compose.yml
  (( DRY_RUN )) && return 0
  # Without secrets.env the DB has no root password and Nextcloud can't run
  # its first-run install — surface it rather than letting the stack flap.
  if [[ ! -f /srv/nextcloud/secrets.env ]]; then
    warn "/srv/nextcloud/secrets.env not found — see services/nextcloud/secrets.env.example"
    warn "Nextcloud + MariaDB will not initialize until credentials are provisioned."
  fi
  compose_up nextcloud
  sleep 5
  # Tailscale-only: no public URL. status.php answers 200 (with JSON) even
  # pre-install, so it's a fair liveness probe.
  verify_url http://127.0.0.1:8081/status.php 200 || true
}

deploy_jellyfin() {
  log "jellyfin"
  local CHANGED=0
  (( DRY_RUN )) || {
    # config/cache on internal ext4, owned by the UID-1000 user (the
    # container runs as PUID/PGID 1000). The media tree is bind-mounted
    # read-only from /mnt/media/video and is not created here.
    # Tailscale-only — no Caddyfile route to deploy.
    [[ -d /srv/jellyfin ]]        || { install -d -o nthncrtr -g nthncrtr -m 0755 /srv/jellyfin;        note "created /srv/jellyfin"; }
    [[ -d /srv/jellyfin/config ]] || { install -d -o nthncrtr -g nthncrtr -m 0755 /srv/jellyfin/config; note "created /srv/jellyfin/config"; }
    [[ -d /srv/jellyfin/cache ]]  || { install -d -o nthncrtr -g nthncrtr -m 0755 /srv/jellyfin/cache;  note "created /srv/jellyfin/cache"; }
  }
  install_file "$REPO_ROOT/services/jellyfin/docker-compose.yml" /srv/jellyfin/docker-compose.yml
  (( DRY_RUN )) && return 0
  # /dev/dri must exist for the HW-transcode passthrough; warn (don't fail)
  # if it's absent — Jellyfin still runs software-only.
  if [[ ! -e /dev/dri/renderD128 ]]; then
    warn "/dev/dri/renderD128 not found — Jellyfin will start but HW transcode is unavailable"
  fi
  compose_up jellyfin
  sleep 5
  # /health answers 200 "Healthy" once up (works on 127.0.0.1 regardless of
  # the public route). The public path is verified out-of-band — see
  # services/jellyfin/README.md (it depends on the router port-forward +
  # Cloudflare record, which deploy.sh cannot assert).
  verify_url http://127.0.0.1:8096/health 200 || true
}

deploy_immich() {
  log "immich"
  local CHANGED=0
  (( DRY_RUN )) || {
    # Parent + the two bind targets, on the internal ext4 (NOT exfat — see
    # the compose header / README: postgres + the upload library both need
    # POSIX semantics). Created root-owned and empty; the immich-server and
    # postgres images chown their own subtrees on first init. Tailnet-only;
    # the photos.nthncrtr.com Caddyfile vhost is deployed via `deploy.sh
    # caddy` (run it after this on first stand-up).
    [[ -d /srv/immich ]]         || { install -d -o root -g root -m 0755 /srv/immich;         note "created /srv/immich"; }
    [[ -d /srv/immich/library ]] || { install -d -o root -g root -m 0755 /srv/immich/library; note "created /srv/immich/library"; }
    [[ -d /srv/immich/db ]]      || { install -d -o root -g root -m 0755 /srv/immich/db;      note "created /srv/immich/db"; }
  }
  install_file "$REPO_ROOT/services/immich/docker-compose.yml" /srv/immich/docker-compose.yml
  (( DRY_RUN )) && return 0
  # Without secrets.env postgres has no superuser password and immich-server
  # can't connect — surface it rather than letting the stack flap.
  if [[ ! -f /srv/immich/secrets.env ]]; then
    warn "/srv/immich/secrets.env not found — see services/immich/secrets.env.example"
    warn "Immich + postgres will not initialize until DB_PASSWORD/POSTGRES_PASSWORD are set (same value)."
  fi
  # Capacity guard: the library lives on / (the 238G SSD). A full Google
  # Photos import can be large; a full / silently breaks more than Immich
  # (see CLAUDE.md § disk space). Warn under ~20G free.
  local avail_g
  avail_g=$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')
  if [[ -n $avail_g && $avail_g -lt 20 ]]; then
    warn "only ${avail_g}G free on / — Immich library is on the SSD; watch capacity (README § caveat)"
  fi
  compose_up immich
  sleep 5
  # First boot runs DB init + migrations (can take a minute); ping answers
  # 200 once the server is up. Tailnet-only — no public URL to verify here.
  verify_url http://127.0.0.1:2283/api/server/ping 200 || \
    warn "immich not answering yet — first boot runs migrations; check 'docker logs immich_server'"
}

deploy_cloudflared() {
  log "cloudflared"
  local CHANGED=0
  (( DRY_RUN )) || {
    [[ -d /srv/cloudflared ]] || { install -d -o root -g root -m 0755 /srv/cloudflared; note "created /srv/cloudflared"; }
  }
  install_file "$REPO_ROOT/services/cloudflared/docker-compose.yml" /srv/cloudflared/docker-compose.yml
  # config.yml carries the per-tunnel UUID, filled in on natto by the
  # operator. NEVER clobber a filled-in deployed copy with the repo
  # placeholder — only install if absent or still the placeholder.
  if [[ ! -f /srv/cloudflared/config.yml ]] || grep -q REPLACE_WITH_TUNNEL_UUID /srv/cloudflared/config.yml 2>/dev/null; then
    install_file "$REPO_ROOT/services/cloudflared/config.yml" /srv/cloudflared/config.yml
  else
    note "config.yml has a real tunnel UUID — left as-is (repo copy is a placeholder)"
  fi
  (( DRY_RUN )) && return 0
  if [[ ! -f /srv/cloudflared/credentials.json ]]; then
    warn "/srv/cloudflared/credentials.json missing — see services/cloudflared/README.md"
    warn "cloudflared cannot connect the tunnel until the credentials file is provided."
  fi
  if grep -q REPLACE_WITH_TUNNEL_UUID /srv/cloudflared/config.yml 2>/dev/null; then
    warn "/srv/cloudflared/config.yml still has the placeholder tunnel UUID — fill it (README step 4)."
  fi
  compose_up cloudflared
  sleep 3
  # No health endpoint; '4x Registered tunnel connection' in the log = up.
  docker logs --tail 4 cloudflared 2>&1 | sed 's/^/    /' || \
    warn "no cloudflared logs yet — check 'docker logs cloudflared'"
}

deploy_authelia() {
  log "authelia"
  local CHANGED=0
  (( DRY_RUN )) || {
    # configuration.yml is deployed (not a secret). users.yml + secrets.env
    # + data/ are provisioned out-of-band on natto and are NOT touched here.
    [[ -d /srv/authelia ]]      || { install -d -o root -g root -m 0755 /srv/authelia;      note "created /srv/authelia"; }
    [[ -d /srv/authelia/data ]] || { install -d -o root -g root -m 0755 /srv/authelia/data; note "created /srv/authelia/data"; }
  }
  install_file "$REPO_ROOT/services/authelia/docker-compose.yml" /srv/authelia/docker-compose.yml
  install_file "$REPO_ROOT/services/authelia/configuration.yml"  /srv/authelia/configuration.yml
  (( DRY_RUN )) && return 0
  # Without users.yml or secrets.env Authelia exits non-zero on boot, which
  # would leave every site that `import authelia`s returning 502. Surface it
  # loudly rather than letting the gate fail closed silently.
  if [[ ! -f /srv/authelia/users.yml ]]; then
    warn "/srv/authelia/users.yml not found — see services/authelia/users.yml.example"
    warn "Authelia will not start; gated sites (home/torrent/radarr/sonarr/prowlarr) will 502."
  fi
  if [[ ! -f /srv/authelia/secrets.env ]]; then
    warn "/srv/authelia/secrets.env not found — see services/authelia/secrets.env.example"
    warn "Authelia will not start; gated sites will 502."
  fi
  compose_up authelia
  sleep 5
  # Portal is 127.0.0.1-only (no Caddyfile-independent public URL). The
  # forward-auth endpoint returns 401 for an unauthenticated probe, which
  # proves the service is up and the authz handler is wired.
  verify_url http://127.0.0.1:9091/api/authz/forward-auth 401 || \
    warn "authelia authz endpoint not answering 401 — check 'docker logs authelia'"
  warn "reminder: 'deploy.sh caddy' must run AFTER this for the gate to take effect;"
  warn "and set the *arrs' auth to 'Disabled for Local Addresses' + qBit's subnet"
  warn "bypass (see services/authelia/README.md) to avoid a double login."
}

deploy_pihole() {
  log "pihole"
  if (( ! YES_PIHOLE )); then
    warn "pihole skipped — use --yes-pihole to apply (~30s DNS outage for everyone)"
    return 0
  fi
  local CHANGED=0
  install_file "$REPO_ROOT/services/pihole/docker-compose.yml" /srv/pihole/docker-compose.yml
  (( DRY_RUN )) && return 0
  if (( ! CHANGED )); then
    note "pihole compose unchanged — not recreating container"
    return 0
  fi
  compose_up pihole
}

deploy_backup() {
  log "backup"
  local CHANGED=0
  # Unit file calls /usr/local/sbin/natto-backup (no .sh suffix), so rename on install.
  install_file "$REPO_ROOT/services/backup/backup.sh" /usr/local/sbin/natto-backup 0755
  install_file "$REPO_ROOT/services/backup/natto-backup.service" /etc/systemd/system/natto-backup.service
  install_file "$REPO_ROOT/services/backup/natto-backup.timer" /etc/systemd/system/natto-backup.timer
  (( DRY_RUN )) && return 0
  if (( CHANGED )); then
    systemctl daemon-reload
    systemctl enable --now natto-backup.timer
    note "natto-backup.timer enabled+active"
  fi
}

deploy_starmaya() {
  log "starmaya (over ssh from natto to kvass)"
  local src_dir=$REPO_ROOT/services/starmaya
  if (( DRY_RUN )); then
    rsync -nav -e ssh \
      "$src_dir/roaster-daemon.service" \
      "$src_dir/roaster-web.service" \
      "$src_dir/99-behmor-arduino.rules" \
      kvass:/tmp/starmaya-deploy/ | sed 's/^/    /'
    return 0
  fi
  ssh kvass 'mkdir -p /tmp/starmaya-deploy'
  rsync -av -e ssh \
    "$src_dir/roaster-daemon.service" \
    "$src_dir/roaster-web.service" \
    "$src_dir/99-behmor-arduino.rules" \
    kvass:/tmp/starmaya-deploy/ | sed 's/^/    /'
  # ssh -t for an interactive TTY so kvass-side sudo can prompt.
  ssh -t kvass '
    set -e
    sudo install -m 0644 -o root -g root /tmp/starmaya-deploy/roaster-daemon.service /etc/systemd/system/roaster-daemon.service
    sudo install -m 0644 -o root -g root /tmp/starmaya-deploy/roaster-web.service   /etc/systemd/system/roaster-web.service
    sudo install -m 0644 -o root -g root /tmp/starmaya-deploy/99-behmor-arduino.rules /etc/udev/rules.d/99-behmor-arduino.rules
    sudo systemctl daemon-reload
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=tty
    sudo systemctl restart roaster-daemon roaster-web
    sudo systemctl is-active --quiet roaster-daemon roaster-web && echo "    starmaya units active"
    rm -rf /tmp/starmaya-deploy
  '
}

# ---- dispatcher -----------------------------------------------------------

(( DRY_RUN )) && log "DRY RUN — no changes will be applied"
check_repo_state

failures=()
for svc in "${SERVICES[@]}"; do
  if ! declare -F "deploy_$svc" >/dev/null; then
    err "unknown service: $svc"
    failures+=("$svc")
    continue
  fi
  "deploy_$svc" || failures+=("$svc")
done

if (( ${#failures[@]} )); then
  err "failures: ${failures[*]}"
  exit 1
fi
log "done"
