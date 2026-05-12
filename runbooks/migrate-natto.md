# Migrating natto to a replacement host

How to bring up a fresh Pi as `natto` and cut over from the old one. Assumes you have console + network access to both hosts and a recent backup tarball on the 5TB drive.

## Prerequisites

- **New hardware:** Raspberry Pi 4 or 5 (4GB+ recommended). Old natto is arm64 — match arch to keep the Caddy binary and Docker images portable.
- **OS image:** Raspberry Pi OS (Debian 13 trixie, 64-bit) or Ubuntu Server LTS 24.04 arm64. Flash the image, set hostname to `natto`, enable SSH, set up the `nthncrtr` user (UID 1000) before first boot.
- **Network:** wired ethernet to the same LAN as the old natto. Tailscale auth key from <https://login.tailscale.com/admin/settings/keys> (one-off, reusable for the cutover).
- **Backup tarball:** the most recent `/mnt/media/backups/natto-YYYY-MM-DD.tgz` from old natto (mission 2.4 produces these). Copy it to the new host before you start, e.g. via `scp old-natto:/mnt/media/backups/natto-latest.tgz new-natto:/tmp/`.
- **Secrets in hand:** Cloudflare API token (for `caddy.env`), Pi-hole admin password (or accept the auto-generated one and reset via web UI).

## Migration order

The order matters: bootstrap first → restore data → start services in dependency order → verify → cut DNS.

### 1. Clone the repo on the new host

```sh
sudo install -d -o nthncrtr -g nthncrtr /opt/nthncrtr
sudo -u nthncrtr git clone https://github.com/<owner>/nthncrtr.git /opt/nthncrtr/repo
cd /opt/nthncrtr/repo
```

### 2. Run the bootstrap

```sh
sudo bootstrap/natto.sh
```

Should exit 0. If it fails, see § Gaps below — and add to it. Re-running after a fix is safe (the script is idempotent).

### 3. Authenticate Tailscale

```sh
sudo tailscale up --authkey=tskey-...
tailscale status   # confirm new natto appears in the tailnet
```

Note the new natto's tailnet IP (`100.x.y.z`). You'll need it for the DNS cutover in § 7.

### 4. Provide Caddy's secret

```sh
sudo install -o caddy -g caddy -m 0600 /dev/stdin /etc/caddy/caddy.env <<< 'CF_API_TOKEN=<your-token>'
```

### 5. Restore service data

The backup is a tarball of `/srv/`, `/usr/local/bin/caddy`, `/etc/caddy/{Caddyfile,caddy.env}`, and `/etc/systemd/system/caddy.service` from the old natto. Extract it back to its original paths:

```sh
sudo tar -xzf /tmp/natto-latest.tgz -C /
```

Spot check: `ls /srv/pihole /srv/navidrome /srv/homepage` — each should contain a `docker-compose.yml` plus the data directories captured by mission 1.7.

If the backup includes `/etc/caddy/caddy.env`, the manual install in § 4 was redundant; harmless either way.

### 6. Start Caddy

```sh
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy.service
sudo systemctl status caddy.service
sudo journalctl -u caddy -n 50    # check for ACME / Cloudflare DNS errors
```

Caddy needs a working internet connection to renew certs (DNS-01 via Cloudflare). It does NOT need port 80 reachable from outside.

### 7. Cut DNS over to the new host

This is the externally-visible cutover. Until you do it, all `*.nthncrtr.com` traffic still flows to the old natto.

In Cloudflare DNS, find the A record for `*.nthncrtr.com` (or the individual subdomain records). Change the IP from the old natto's tailnet IP to the new one (from § 3). Cloudflare → Tailscale → new natto → Caddy.

Wait for the change to propagate (Cloudflare's TTL is usually 1 min). Then:

```sh
dig +short home.nthncrtr.com    # should resolve to the new tailnet IP
```

### 8. Start docker services and smoke-test

In dependency order. Pi-hole first (DNS), then Navidrome and Homepage (independent), then qBittorrent.

```sh
cd /srv/pihole && sudo docker compose up -d
sleep 5 && dig @127.0.0.1 example.com    # should answer

cd /srv/navidrome && sudo docker compose up -d
sleep 5 && curl -fsSL -o /dev/null -w '%{http_code}\n' https://natto.nthncrtr.com/ping    # 200

cd /srv/homepage && sudo docker compose up -d
sleep 15 && curl -fsSL -o /dev/null -w '%{http_code}\n' https://home.nthncrtr.com    # 200

cd /srv/qbittorrent && sudo docker compose up -d
sleep 5 && curl -fsSL -o /dev/null -w '%{http_code}\n' https://torrent.nthncrtr.com/    # 200 or 401
```

### 9. Decommission the old host

Only after all smoke tests pass on the new natto:

```sh
# On old natto
sudo systemctl stop caddy.service
cd /srv/pihole       && sudo docker compose down
cd /srv/navidrome    && sudo docker compose down
cd /srv/homepage     && sudo docker compose down
cd /srv/qbittorrent  && sudo docker compose down
sudo tailscale logout
sudo poweroff
```

Keep the old SD card around for a week before re-flashing — it's your last-resort rollback.

## Rollback

If the new host doesn't work and DNS is already cut over:

1. Revert the Cloudflare A record to the old natto's tailnet IP.
2. Re-power the old natto. Tailscale should reconnect and pick up its old IP. (If the IP changed, update the Cloudflare record again to the *new* old-natto IP.)
3. Old containers should auto-start (`restart: unless-stopped`). If not: `cd /srv/<svc> && sudo docker compose up -d` for each.

If the new host is fine but you want to roll *backups* back: extract an older `/mnt/media/backups/natto-*.tgz` tarball with the same `tar -xzf ... -C /` pattern.

## Gaps found during dry-run

Each entry: date, what failed/needed manual intervention, and the fix.

- *(empty — full cold-start dry-run hasn't happened yet; idempotency-only dry-run on natto on 2026-05-09 surfaced one bug, fixed in commit `a497e30`: the Caddy rebuild check was mtime-based and would falsely fire on any fresh repo clone.)*
