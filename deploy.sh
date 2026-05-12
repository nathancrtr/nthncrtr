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
# Services: caddy navidrome homepage backup qbittorrent pihole starmaya
# Default (no service args): caddy navidrome homepage backup qbittorrent
#   — pihole is gated behind --yes-pihole (DNS outage for ~30s).
#   — starmaya must be requested explicitly (deploys to kvass over ssh).
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

Services: caddy navidrome homepage backup qbittorrent pihole starmaya
Default (no service args): caddy navidrome homepage backup qbittorrent
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
  SERVICES=(caddy navidrome homepage backup qbittorrent)
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
  if ! caddy adapt --adapter caddyfile --config "$REPO_ROOT/services/caddy/Caddyfile" >/dev/null 2>/tmp/caddy-adapt.err; then
    err "caddy adapt failed; aborting caddy deploy (live config untouched)"
    sed 's/^/      /' /tmp/caddy-adapt.err >&2
    return 1
  fi
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
  verify_url https://natto.nthncrtr.com 200 || true
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
  (( DRY_RUN )) && return 0
  # Ensure data dirs exist (idempotent; first-time setup may have already done this).
  if [[ ! -d /srv/qbittorrent/config || ! -d /srv/qbittorrent/downloads ]]; then
    install -d -o nthncrtr -g nthncrtr -m 0755 /srv/qbittorrent/config /srv/qbittorrent/downloads
    note "created /srv/qbittorrent/{config,downloads}"
  fi
  compose_up qbittorrent
  sleep 3
  verify_url https://torrent.nthncrtr.com 200 || true
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
