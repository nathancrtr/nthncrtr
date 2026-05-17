#!/usr/bin/env bash
# Idempotent bootstrap for natto — the hub that runs Caddy (native) plus Pi-hole,
# Navidrome, Homepage, and (eventually) qBittorrent in Docker.
#
# Run on a fresh Debian/Ubuntu/Raspberry Pi OS host (arm64 expected) as root.
# The script is idempotent — safe to re-run after a partial run, and a clean
# second run should be a no-op.
#
# What it does:
#   1. Preflight checks (root, supported distro, arch).
#   2. Install Docker engine + compose plugin (via get.docker.com).
#   3. Install Tailscale (via tailscale.com/install.sh).
#   4. Install Go + xcaddy, build Caddy via services/caddy/build.sh, install to
#      /usr/local/bin/caddy. Create caddy system user. Install
#      /etc/caddy/Caddyfile and the systemd unit. Does NOT create caddy.env;
#      that file holds the Cloudflare API token and must be installed manually.
#   5. Create /srv/{pihole,navidrome,homepage}/ owned appropriately and copy
#      services/<svc>/docker-compose.yml into each.
#   6. Install /usr/local/sbin/natto-backup + natto-backup.{service,timer},
#      enable the daily timer.
#   7. Print next steps (provide secrets, restore data, start services).
#
# What it does NOT do (intentionally):
#   - Authenticate Tailscale (operator runs `tailscale up` with their auth key).
#   - Provide secrets (Cloudflare token, Pi-hole admin password). Operator
#     populates /etc/caddy/caddy.env and any other secret files manually.
#   - Start docker services (operator runs `docker compose up -d` per service
#     after verifying mounts and restoring data from backup).
#
# Usage:
#   sudo bootstrap/natto.sh
#
# Optionally: TS_AUTHKEY=tskey-... sudo -E bootstrap/natto.sh   (still leaves
#   the actual `tailscale up` to you — TS_AUTHKEY is just printed in the
#   next-steps block so you don't have to look it up.)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
log()  { printf '[bootstrap] %s\n' "$*"; }
skip() { printf '[bootstrap] %s — already in place, skipping\n' "$*"; }

# ---------------------------------------------------------------------------
# Step 1: preflight
# ---------------------------------------------------------------------------
preflight() {
  if [[ $EUID -ne 0 ]]; then
    echo "Run as root or via sudo." >&2
    exit 1
  fi

  if [[ ! -f /etc/os-release ]]; then
    echo "Cannot determine OS (no /etc/os-release)." >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    debian|ubuntu|raspbian) ;;
    *) echo "Unsupported OS: ${ID:-unknown}. Expected debian/ubuntu/raspbian." >&2; exit 1 ;;
  esac

  local arch
  arch=$(dpkg --print-architecture 2>/dev/null || uname -m)
  if [[ "$arch" != "arm64" && "$arch" != "aarch64" ]]; then
    log "WARNING: arch is $arch; natto is expected to be arm64. Continuing anyway."
  fi

  log "preflight OK (${ID} ${VERSION_ID:-?} on $arch)"
}

# ---------------------------------------------------------------------------
# Step 2: Docker
# ---------------------------------------------------------------------------
step_docker() {
  if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    skip "docker engine + compose plugin"
    return
  fi

  log "installing Docker via get.docker.com"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker.service

  # Add the operator's primary user (UID 1000) to the docker group if present.
  local user1000
  user1000=$(getent passwd 1000 | cut -d: -f1 || true)
  if [[ -n "$user1000" ]] && ! id -nG "$user1000" | grep -qw docker; then
    usermod -aG docker "$user1000"
    log "added $user1000 to docker group (re-login required)"
  fi
}

# ---------------------------------------------------------------------------
# Step 3: Tailscale
# ---------------------------------------------------------------------------
step_tailscale() {
  if command -v tailscale >/dev/null; then
    skip "tailscale"
    return
  fi
  log "installing Tailscale via tailscale.com/install.sh"
  curl -fsSL https://tailscale.com/install.sh | sh
  systemctl enable --now tailscaled.service
}

# ---------------------------------------------------------------------------
# Step 3.5: Deploy key + ssh config for /srv/nthncrtr-repo (CD pipeline)
# ---------------------------------------------------------------------------
# Generates a passphrase-less ed25519 keypair for the UID-1000 user to use as
# a GitHub Deploy key on this repo, and writes an ~/.ssh/config entry so SSH
# offers it to github.com. Cloning the repo to /srv/nthncrtr-repo remains a
# manual step (the operator has to add the pubkey to GitHub first).
#
# Passphrase-less is deliberate: server-resident keys used non-interactively
# can't prompt for a passphrase, and the threat model is "if the host is
# compromised, the read-only single-repo deploy key is the least concern."
step_deploy_key() {
  local user1000 home_dir
  user1000=$(getent passwd 1000 | cut -d: -f1)
  if [[ -z "$user1000" ]]; then
    log "WARNING: no UID 1000 user; skipping deploy-key setup"
    return
  fi
  home_dir=$(getent passwd 1000 | cut -d: -f6)

  local key_path="$home_dir/.ssh/id_ed25519_deploy"
  local config_path="$home_dir/.ssh/config"

  install -d -o "$user1000" -g "$user1000" -m 0700 "$home_dir/.ssh"

  if [[ -f "$key_path" ]]; then
    skip "deploy keypair at $key_path"
  else
    log "generating passphrase-less deploy keypair at $key_path"
    sudo -u "$user1000" ssh-keygen -t ed25519 -N "" -C "deploy@$(hostname -s)" -f "$key_path" >/dev/null
  fi

  if [[ -f "$config_path" ]] && grep -q '^Host github.com$' "$config_path"; then
    skip "ssh config Host github.com block"
  else
    log "adding Host github.com block to $config_path"
    sudo -u "$user1000" tee -a "$config_path" >/dev/null <<EOF

Host github.com
    HostName github.com
    User git
    IdentityFile $key_path
    IdentitiesOnly yes
EOF
    chmod 600 "$config_path"
    chown "$user1000:$user1000" "$config_path"
  fi

  if [[ ! -d /srv/nthncrtr-repo/.git ]]; then
    log "deploy-key pubkey (add to GitHub repo → Settings → Deploy keys before cloning):"
    sed 's/^/      /' "${key_path}.pub"
  fi
}

# ---------------------------------------------------------------------------
# Step 4: Caddy (build, user, unit, Caddyfile)
# ---------------------------------------------------------------------------
step_caddy() {
  # caddy user/group
  if ! getent group caddy >/dev/null; then
    groupadd --system caddy
  fi
  if ! id -u caddy >/dev/null 2>&1; then
    useradd --system --gid caddy --home-dir /var/lib/caddy --create-home \
      --shell /usr/sbin/nologin caddy
  fi

  # Build only if /usr/local/bin/caddy is missing or its version differs from
  # CADDY_VERSION declared in build.sh. (mtime checks would falsely trigger
  # whenever the repo is freshly cloned, so we compare versions instead.)
  local expected_version installed_version need_build=0
  expected_version=$(sed -nE 's/^CADDY_VERSION="(v[^"]+)".*/\1/p' \
    "$REPO_ROOT/services/caddy/build.sh")
  if [[ -z "$expected_version" ]]; then
    echo "could not parse CADDY_VERSION from services/caddy/build.sh" >&2
    exit 1
  fi
  if [[ ! -x /usr/local/bin/caddy ]]; then
    need_build=1
  else
    installed_version=$(/usr/local/bin/caddy version | awk '{print $1}')
    if [[ "$installed_version" != "$expected_version" ]]; then
      log "caddy installed=$installed_version expected=$expected_version — rebuilding"
      need_build=1
    fi
  fi

  if (( need_build )); then
    # Toolchain: Go + xcaddy
    if ! command -v go >/dev/null; then
      log "installing golang-go via apt"
      DEBIAN_FRONTEND=noninteractive apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y golang-go
    fi
    local go_major
    go_major=$(go version | sed -nE 's/.*go([0-9]+)\.[0-9]+.*/\1/p')
    if (( go_major < 1 )); then
      echo "go version unparseable" >&2; exit 1
    fi

    if ! command -v xcaddy >/dev/null && [[ ! -x /root/go/bin/xcaddy ]]; then
      log "installing xcaddy"
      go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
    fi
    export PATH="$PATH:/root/go/bin"

    log "building caddy via services/caddy/build.sh"
    local tmp
    tmp=$(mktemp -d)
    pushd "$tmp" >/dev/null
    bash "$REPO_ROOT/services/caddy/build.sh"
    install -o root -g root -m 0755 ./caddy /usr/local/bin/caddy
    popd >/dev/null
    rm -rf "$tmp"
    log "caddy installed: $(/usr/local/bin/caddy version)"
  else
    skip "caddy binary"
  fi

  # /etc/caddy + Caddyfile (Caddyfile only; caddy.env stays operator-provided)
  install -d -o root -g root -m 0755 /etc/caddy
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/caddy/Caddyfile" \
    /etc/caddy/Caddyfile

  # systemd unit
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/caddy/caddy.service" \
    /etc/systemd/system/caddy.service
  systemctl daemon-reload

  if [[ ! -f /etc/caddy/caddy.env ]]; then
    log "WARNING: /etc/caddy/caddy.env missing — Caddy will not start until"
    log "         you create it with CF_API_TOKEN=<token>, mode 0600, owner caddy:caddy."
  fi
}

# ---------------------------------------------------------------------------
# Step 5: /srv/<svc>/ + compose files
# ---------------------------------------------------------------------------
step_srv() {
  # Pi-hole stays root-owned because the data inside is root-owned (the
  # pihole/pihole image creates files as the in-container pihole UID, which
  # surfaces as root on the host).
  install -d -o root     -g root     -m 0755 /srv/pihole
  install -d -o "$(getent passwd 1000 | cut -d: -f1)" \
              -g "$(getent group  1000 | cut -d: -f1)" -m 0755 /srv/navidrome
  install -d -o "$(getent passwd 1000 | cut -d: -f1)" \
              -g "$(getent group  1000 | cut -d: -f1)" -m 0755 /srv/homepage
  install -d -o "$(getent passwd 1000 | cut -d: -f1)" \
              -g "$(getent group  1000 | cut -d: -f1)" -m 0755 /srv/radarr
  install -d -o "$(getent passwd 1000 | cut -d: -f1)" \
              -g "$(getent group  1000 | cut -d: -f1)" -m 0755 /srv/sonarr
  install -d -o "$(getent passwd 1000 | cut -d: -f1)" \
              -g "$(getent group  1000 | cut -d: -f1)" -m 0755 /srv/prowlarr
  install -d -o "$(getent passwd 1000 | cut -d: -f1)" \
              -g "$(getent group  1000 | cut -d: -f1)" -m 0755 /srv/qbittorrent
  # Nextcloud: parent dir only. The html/data/db subdirs are created by
  # deploy.sh and their contents are owned by the in-container UIDs
  # (www-data, mysql) — encoding those host-side here would be wrong.
  install -d -o root -g root -m 0755 /srv/nextcloud
  # Jellyfin: config/cache owned by the UID-1000 user (container runs as
  # PUID/PGID 1000). The media tree stays on /mnt/media/video (bind-mounted
  # read-only) and is not created here.
  install -d -o "$(getent passwd 1000 | cut -d: -f1)" \
              -g "$(getent group  1000 | cut -d: -f1)" -m 0755 /srv/jellyfin
  # cloudflared exists only because Jellyfin is public (WORKLIST 6.6) — the
  # Cloudflare Tunnel public path. Root-owned: needs compose + config.yml +
  # credentials.json (operator-provided). (Brute-force protection is a
  # Cloudflare dashboard Rate-Limiting rule, not a service here.)
  install -d -o root -g root -m 0755 /srv/cloudflared

  # Copy compose files (root-owned, world-readable so the docker group user
  # can `docker compose ...` against them).
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/pihole/docker-compose.yml"   /srv/pihole/docker-compose.yml
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/navidrome/docker-compose.yml" /srv/navidrome/docker-compose.yml
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/homepage/docker-compose.yml"  /srv/homepage/docker-compose.yml
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/radarr/docker-compose.yml"    /srv/radarr/docker-compose.yml
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/sonarr/docker-compose.yml"    /srv/sonarr/docker-compose.yml
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/prowlarr/docker-compose.yml"  /srv/prowlarr/docker-compose.yml
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/qbittorrent/docker-compose.yml" /srv/qbittorrent/docker-compose.yml
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/nextcloud/docker-compose.yml"   /srv/nextcloud/docker-compose.yml
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/jellyfin/docker-compose.yml"    /srv/jellyfin/docker-compose.yml
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/cloudflared/docker-compose.yml" /srv/cloudflared/docker-compose.yml
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/cloudflared/config.yml"         /srv/cloudflared/config.yml
}

# ---------------------------------------------------------------------------
# Step 6: backup script + daily timer
# ---------------------------------------------------------------------------
step_backup() {
  install -o root -g root -m 0755 \
    "$REPO_ROOT/services/backup/backup.sh" \
    /usr/local/sbin/natto-backup
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/backup/natto-backup.service" \
    /etc/systemd/system/natto-backup.service
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/backup/natto-backup.timer" \
    /etc/systemd/system/natto-backup.timer
  # Weekly Nextcloud data mirror (no-op on hosts without Nextcloud deployed).
  install -o root -g root -m 0755 \
    "$REPO_ROOT/services/backup/nextcloud-data-sync.sh" \
    /usr/local/sbin/nextcloud-data-sync
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/backup/nextcloud-data-sync.service" \
    /etc/systemd/system/nextcloud-data-sync.service
  install -o root -g root -m 0644 \
    "$REPO_ROOT/services/backup/nextcloud-data-sync.timer" \
    /etc/systemd/system/nextcloud-data-sync.timer
  systemctl daemon-reload
  systemctl enable --now natto-backup.timer
  systemctl enable --now nextcloud-data-sync.timer

  # Create the backup target if the 5TB drive is mounted; warn otherwise.
  if mountpoint -q /mnt/media; then
    install -d -o root -g root -m 0755 /mnt/media/backups
  else
    log "WARNING: /mnt/media is not mounted — backups will fail until you mount"
    log "         the 5TB drive there and create /mnt/media/backups."
  fi
}

# ---------------------------------------------------------------------------
# Step 7: next-steps banner
# ---------------------------------------------------------------------------
banner() {
  cat <<EOF

Bootstrap complete.

Next steps (operator):
  1. Authenticate Tailscale and bring it up:
       tailscale up${TS_AUTHKEY:+ --authkey=\$TS_AUTHKEY}
     ${TS_AUTHKEY:+(TS_AUTHKEY is in your environment.)}

  2. Provide secrets:
       a. /etc/caddy/caddy.env — Cloudflare API token for the DNS-01 challenge.
            sudo install -o caddy -g caddy -m 0600 /dev/stdin /etc/caddy/caddy.env <<<'CF_API_TOKEN=<token>'
       b. Pi-hole admin password — set on first run via Pi-hole's web UI, or
          export WEBPASSWORD=... in the compose environment before bringing it up.
       c. /srv/nextcloud/secrets.env — MariaDB + Nextcloud admin creds, mode
          0600. See services/nextcloud/secrets.env.example.
       d. /srv/cloudflared/credentials.json (0600) + tunnel UUID in
          /srv/cloudflared/config.yml — see services/cloudflared/README.md
          (cloudflared tunnel login/create/route — interactive, operator).

  2b. Public Jellyfin (the ONE internet-exposed service — WORKLIST 6.6).
      GFiber cannot port-forward (reserved 443 / phantom-device / DMZ=all);
      the public path is a Cloudflare Tunnel:
       - cloudflared: tunnel login → create play → route dns
         (services/cloudflared/README.md). Exposes ONLY
         play.nthncrtr.com → Jellyfin; nothing else on natto.
       - Pi-hole: add local DNS play.nthncrtr.com → natto LAN IP
         (split-horizon — inside clients use Caddy :443 directly, no
         Cloudflare round-trip; one URL works in + out).
       - Cloudflare dashboard: a WAF Rate-Limiting rule on the Jellyfin
         login path is the brute-force layer (no fail2ban — see
         WORKLIST 6.6 for why the container approach was retired).
       - Jellyfin UI → Dashboard → Networking → Known proxies = 127.0.0.1
         (cloudflared connects from localhost; correct proxied-client
         behaviour + real IPs in Jellyfin's own log). Create per-user
         accounts; UPnP port-mapping OFF; set Playback → Internet
         streaming bitrate limit ~10–15 Mbps.

  3. Restore service data from the latest /mnt/media/backups/natto-*.tgz tarball
     into /srv/{pihole,navidrome,homepage}/. (See runbooks/migrate-natto.md.)

  4. Validate Caddy and start it:
       caddy validate --config /etc/caddy/Caddyfile
       systemctl enable --now caddy.service
       systemctl status caddy.service

  5. Start docker services one at a time and verify each via its public URL:
       cd /srv/pihole       && docker compose up -d   # then dig @127.0.0.1 example.com
       cd /srv/navidrome   && docker compose up -d   # then curl https://music.nthncrtr.com/ping
       cd /srv/homepage    && docker compose up -d   # then curl https://home.nthncrtr.com
       cd /srv/qbittorrent && docker compose up -d   # then curl https://torrent.nthncrtr.com
       cd /srv/nextcloud   && docker compose up -d   # Tailscale-only; curl http://127.0.0.1:8081/status.php
       cd /srv/jellyfin    && docker compose up -d   # Tailscale-only; curl http://127.0.0.1:8096/health

  6. Set up the CD pipeline (one-time, if /srv/nthncrtr-repo isn't already in place):
       a. Add the deploy-key pubkey (printed by step_deploy_key above) to the
          repo's Settings → Deploy keys (read access only is sufficient).
       b. As the UID-1000 user, clone the repo to /srv/nthncrtr-repo:
            git clone git@github.com:nathancrtr/nthncrtr.git /tmp/nthncrtr-repo
            sudo mv /tmp/nthncrtr-repo /srv/nthncrtr-repo
       c. Going forward, deploy with:
            cd /srv/nthncrtr-repo && git pull && sudo ./deploy.sh [--dry-run] [services...]

EOF
}

# ---------------------------------------------------------------------------
main() {
  preflight
  step_docker
  step_tailscale
  step_deploy_key
  step_caddy
  step_srv
  step_backup
  banner
}

main "$@"
