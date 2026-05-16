# Migrating natto to a replacement host

How to stand up a fresh host as `natto` and cut over from the old one. Assumes you have console + network access to both hosts, the 5TB USB drive that currently holds `/mnt/media`, and a recent backup tarball.

## Notes for the 2026-05 migration (Pi → Beelink Mini S12)

This is the first cross-arch run. The old natto is a Raspberry Pi (arm64); the new natto is a Beelink Mini S12 (Intel N100, x86_64). Two things follow:

- The Caddy binary in the backup tarball is arm64. Do **not** restore it on top of the freshly-built x86_64 Caddy — the runbook handles this explicitly with a `--exclude` in § 5.
- Docker images for everything we run (`lscr.io/linuxserver/*`, `pihole/pihole`, `deluan/navidrome`, `ghcr.io/gethomepage/homepage`, `qmcgaw/gluetun`, `curlimages/curl`) are multi-arch. They re-pull the amd64 variant on first compose-up. No image-side work needed.

The 5TB USB drive is exfat (cross-platform, cross-arch). It physically unplugs from the Pi and replugs into the Beelink. Its UUID stays the same so the existing `/etc/fstab` line is portable.

## Prerequisites

- **New hardware:** Beelink Mini S12 (or any x86_64 box with ≥4GB RAM, ≥120GB internal storage, USB 3.x for the 5TB drive). The old host was a Pi 4 — match arch to the binaries in the backup tarball only if you want to skip the cross-arch caveats in § 5.
- **OS image:** Debian 13 (trixie) amd64 net-installer, or Ubuntu Server LTS 24.04 amd64. Set hostname to `natto`, enable SSH, create the `nthncrtr` user (UID 1000) before first boot.
- **Network:** wired ethernet to the same LAN as the old natto. Tailscale auth key from <https://login.tailscale.com/admin/settings/keys> (one-off, reusable for the cutover).
- **Backup tarball:** the most recent `/mnt/media/backups/natto-YYYY-MM-DD.tgz` from old natto (the daily `natto-backup.timer` produces these). Two ways to get it onto the new host:
  - *(preferred, no copy)* Leave it on the 5TB drive. After the drive is unplugged from old natto and plugged into the Beelink, the tarball is at `/mnt/media/backups/natto-YYYY-MM-DD.tgz` directly.
  - *(if you can't move the drive yet)* `scp old-natto:/mnt/media/backups/natto-latest.tgz new-natto:/tmp/`.
- **Secrets in hand:** Cloudflare API token (for `caddy.env`), Pi-hole admin password (or accept the auto-generated one and reset via web UI), Proton VPN WireGuard private key + forwarded-port-enabled config (for qBittorrent), Orpheus API key if you're mid-restore, Nextcloud `secrets.env` values (MariaDB + admin creds — reuse the old host's `/srv/nextcloud/secrets.env`; it is *not* in the backup tarball).

## Migration order

The order matters: capture final state on old → bootstrap new → restore data → move the drive → start services in dependency order → verify → cut DNS.

### 0. Pre-migration tasks on old natto

Right before you start on the new host, on old natto:

```sh
ssh natto

# Force a fresh backup tarball, so /srv reflects current state (including
# qBittorrent's BT_backup with up-to-the-minute .fastresume progress).
sudo systemctl start natto-backup.service
sudo journalctl -u natto-backup -n 20 --no-pager   # confirm it wrote the .tgz

ls -lh /mnt/media/backups/natto-*.tgz | tail -3
```

Don't shut anything down yet — old natto keeps serving DNS + the rest of the household traffic right up until the DNS cutover in § 7. The Pi-hole outage gate doesn't apply here because Pi-hole keeps running on old natto until then.

### 1. Clone the repo on the new host

The repo lives at `/srv/nthncrtr-repo` on natto (created by the UID-1000 user, used by `deploy.sh`).

```sh
sudo install -d -o nthncrtr -g nthncrtr /srv
sudo -u nthncrtr git clone https://github.com/<owner>/nthncrtr.git /srv/nthncrtr-repo
cd /srv/nthncrtr-repo
```

The deploy-key flow (passphrase-less ed25519 → `Host github.com` SSH config entry) is set up by `bootstrap/natto.sh step_deploy_key`. For the initial clone you can use HTTPS; switch the remote to SSH after bootstrap if you want `git pull` from cron to work without a password.

### 2. Run the bootstrap

```sh
sudo bootstrap/natto.sh
```

Expected on first run on x86_64: `WARNING: arch is amd64; natto is expected to be arm64. Continuing anyway.` That's fine — the script just notes it and proceeds; `caddy` rebuilds for the local arch. Should exit 0. If it fails, see § Gaps below — and add to it. Re-running after a fix is safe (the script is idempotent).

### 3. Authenticate Tailscale

```sh
sudo tailscale up --authkey=tskey-... --hostname=natto
tailscale status   # confirm new natto appears in the tailnet
```

Tailscale treats the new host as a separate machine — even with `--hostname=natto` it'll get a new tailnet IP. Note that IP; you'll need it for the DNS cutover in § 7. Don't `tailscale logout` the old natto yet; it's still serving traffic until step 7. If Tailscale auto-suffixes the new host (e.g., `natto-1.tailaf7ea6.ts.net`) because the name is already taken, that's expected — the old host's identity will be released in § 9.

### 4. Provide Caddy's secret

```sh
sudo install -o caddy -g caddy -m 0600 /dev/stdin /etc/caddy/caddy.env <<< 'CF_API_TOKEN=<your-token>'
```

### 5. Restore service data + move the 5TB drive

This is where the bulk of state arrives on the new host. Two sub-steps:

**5a. Move the 5TB drive.** On old natto, stop services that hold open files on `/mnt/media` (Navidrome, qBittorrent, the *arrs):

```sh
ssh natto
cd /srv/qbittorrent && sudo docker compose down
cd /srv/navidrome   && sudo docker compose down
cd /srv/radarr      && sudo docker compose down
cd /srv/sonarr      && sudo docker compose down
cd /srv/prowlarr    && sudo docker compose down 2>/dev/null || true
sudo umount /mnt/media
ls /mnt/media   # should be empty
```

Physically unplug the USB drive from the Pi and plug it into the Beelink. On the new host:

```sh
# /etc/fstab on the new host needs the same line as old natto:
#   UUID=84B5-47F4 /mnt/media exfat defaults,uid=1000,gid=1000,nofail 0 0
# (UUID is identical because it's a property of the filesystem, not the host.)
sudo install -d -o nthncrtr -g nthncrtr -m 0755 /mnt/media
sudo blkid /dev/sd*   # confirm the exfat UUID matches 84B5-47F4
echo 'UUID=84B5-47F4 /mnt/media exfat defaults,uid=1000,gid=1000,nofail 0 0' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount /mnt/media
ls /mnt/media          # music/ video/ backups/ _unsorted/ should be there
df -h /mnt/media       # exfat, ~5T
```

If `mount` fails with "wrong fs type": `sudo apt-get install -y exfatprogs` and retry. Debian 13 ships with the kernel exfat driver but `mount` still needs the userspace tools on some installs.

**5b. Restore `/srv/` and Caddy from the tarball.** The latest backup is now at `/mnt/media/backups/natto-*.tgz`. Extract it back to absolute paths, **excluding the arm64 Caddy binary**:

```sh
LATEST=$(ls -t /mnt/media/backups/natto-*.tgz | head -1)
echo "Restoring from: $LATEST"

# --exclude='/usr/local/bin/caddy' keeps the freshly-built x86_64 binary in
# place. The tarball was written with tar -P (absolute paths preserved).
sudo tar --exclude='/usr/local/bin/caddy' -xzPf "$LATEST" -C /

# Sanity check: caddy is still the local-arch one, and /srv has our state.
file /usr/local/bin/caddy            # should report "ELF 64-bit ... x86-64"
ls /srv/pihole /srv/navidrome /srv/homepage /srv/qbittorrent /srv/radarr /srv/sonarr
```

If the backup includes `/etc/caddy/caddy.env`, the manual install in § 4 was redundant; harmless either way.

Same-arch migration (e.g., Pi → Pi)? Drop the `--exclude='/usr/local/bin/caddy'` — the arm64 Caddy binary from the tarball will simply overwrite the matching one bootstrap built. It still works.

**Nextcloud is only partially in the tarball — restore it deliberately.** The
nightly backup excludes `/srv/nextcloud/{data,db}` (see `services/backup/README.md`).
After the tar extract you have `/srv/nextcloud/html/` and the logical dump
`/srv/nextcloud/db-dump.sql.gz`, but an *empty* `data/` and `db/`. Restore order:

```sh
# 1. Bring up just the DB on a fresh datadir, let it initialize, then load
#    the dump (provide /srv/nextcloud/secrets.env first — see Prerequisites).
cd /srv/nextcloud && docker compose up -d nextcloud-db
sleep 20
zcat /srv/nextcloud/db-dump.sql.gz | \
  docker exec -i nextcloud-db sh -c 'mariadb -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"'

# 2. Refill user files from the weekly mirror on the 5TB drive.
sudo rsync -aH --numeric-ids \
  /mnt/media/backups/nextcloud-data/ /srv/nextcloud/data/

# 3. Bring up the rest (covered by § 8 below).
```

If this is the *initial* Drive migration (no prior natto Nextcloud existed),
skip the above entirely — there's nothing to restore; follow
`runbooks/migrate-off-gdrive.md` once the stack is up.

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

In Cloudflare DNS, find the A records for the `*.nthncrtr.com` apex/wildcard and per-subdomain entries (`home`, `natto`, `torrent`, `starmaya`, etc.). Change the IP from the old natto's tailnet IP to the new one (from § 3). Cloudflare → Tailscale → new natto → Caddy.

Wait for the change to propagate (Cloudflare's TTL is usually 1 min). Then:

```sh
dig +short home.nthncrtr.com    # should resolve to the new tailnet IP
```

### 8. Start docker services and smoke-test

In dependency order. Pi-hole first (DNS), then the rest. The compose files were restored to `/srv/<svc>/docker-compose.yml` in § 5; data dirs are intact under each.

```sh
cd /srv/pihole && sudo docker compose up -d
sleep 5 && dig @127.0.0.1 example.com    # should answer

cd /srv/navidrome && sudo docker compose up -d
sleep 5 && curl -fsSL -o /dev/null -w '%{http_code}\n' https://natto.nthncrtr.com/ping    # 200

cd /srv/homepage && sudo docker compose up -d
sleep 15 && curl -fsSL -o /dev/null -w '%{http_code}\n' https://home.nthncrtr.com    # 200

cd /srv/qbittorrent && sudo docker compose up -d
sleep 30   # gluetun's WireGuard handshake takes 5–20s; qBit blocks until tun0 is up
docker logs gluetun     2>&1 | tail -20 | grep -iE 'healthy|tunnel|VPN'
docker logs qbittorrent 2>&1 | tail -10
curl -fsSL -o /dev/null -w '%{http_code}\n' https://torrent.nthncrtr.com/    # 200 or 401

cd /srv/radarr   && sudo docker compose up -d
cd /srv/sonarr   && sudo docker compose up -d
cd /srv/prowlarr && sudo docker compose up -d

# Nextcloud — Tailscale-only, so it has NO Cloudflare A record and was NOT
# part of the § 7 DNS cutover. Provide /srv/nextcloud/secrets.env first
# (Prerequisites), and if restoring an existing instance do the DB+data
# restore in § 5b before this. status.php answers 200 once it's healthy.
cd /srv/nextcloud && sudo docker compose up -d
sleep 20
curl -fsSL -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/status.php   # 200
docker exec -u www-data nextcloud php occ status                               # installed: true
```

### 8.5. End-to-end smoke check via deploy.sh

`deploy.sh` with no args runs the full default set (`caddy navidrome homepage backup qbittorrent radarr sonarr prowlarr nextcloud`) and is also a useful smoke check: re-running it after § 8 should be effectively a no-op if everything came up correctly.

```sh
cd /srv/nthncrtr-repo
sudo ./deploy.sh --dry-run        # preview — should show no diffs
sudo ./deploy.sh                  # apply (no-op if dry-run was clean)
```

### 8.6. Verify qBittorrent resumed its in-flight torrents

The whole point of moving `/srv/qbittorrent/config/` + `/mnt/media/` together is that qBit reads its `BT_backup/` directory on startup, finds a `.fastresume` for every torrent, and resumes from where it left off — both downloads in progress and torrents already complete (which switch to seeding). Verification:

```sh
# Count of torrents qBit knows about — should match what was on old natto.
ssh natto 'ls /srv/qbittorrent/config/qBittorrent/BT_backup/*.fastresume 2>/dev/null | wc -l'

# In the WebUI at https://torrent.nthncrtr.com:
#   - Total count under "All" matches the BT_backup count.
#   - Torrents transition from "Checking" → "Downloading" or "Seeding" within
#     a few minutes (qBit re-hashes data on first start to verify integrity).
#   - Pick one torrent that was mid-download on old natto and confirm its
#     "Progress" column matches the value you saw before shutdown.

# Port forwarding picked up by the sidecar:
docker logs qbit-port-updater | tail -10
docker exec gluetun cat /tmp/gluetun/forwarded_port
```

If torrents are stuck in "Stalled" with 0 peers, gluetun's tunnel may be up but Proton's port forwarding hasn't been assigned yet. Restart gluetun (`docker compose restart gluetun`) and wait another minute; the sidecar pushes the new port to qBit automatically.

If you were mid-flight on the Orpheus restore procedure (see `services/qbittorrent/README.md` § Disaster recovery), the `.torrent` files in `/srv/qbittorrent/restore/` were captured in the tarball, so you can resume that workflow on the new host with the same `qbit-bulk-add.sh --dir ./restore` command. qBit dedupes by infohash, so re-running adds nothing for torrents already loaded.

### 9. Decommission the old host

Only after all smoke tests pass on the new natto:

```sh
# On old natto
sudo systemctl stop caddy.service
cd /srv/pihole       && sudo docker compose down
cd /srv/navidrome    && sudo docker compose down
cd /srv/homepage     && sudo docker compose down
cd /srv/qbittorrent  && sudo docker compose down   # may already be down from § 5a
cd /srv/radarr       && sudo docker compose down
cd /srv/sonarr       && sudo docker compose down
cd /srv/prowlarr     && sudo docker compose down 2>/dev/null || true
sudo tailscale logout
sudo poweroff
```

Keep the old SD card around for a week before re-flashing — it's your last-resort rollback. Once Tailscale's old identity is released (it logs out cleanly above), the new host can be renamed from `natto-1` back to `natto` in the Tailscale admin console if needed.

## Alternative: in-place rsync (if you can't move the drive yet)

If the 5TB drive has to stay on old natto temporarily (e.g., you want both hosts online for a soak period), do this instead of § 5a:

```sh
# From new natto:
sudo systemctl start tailscaled                # already done in § 3
sudo rsync -aHAX --delete --info=progress2 \
  nthncrtr@old-natto.tailaf7ea6.ts.net:/srv/ /srv/
# Run a second pass after stopping services on old natto, to catch deltas:
ssh old-natto 'cd /srv/qbittorrent && sudo docker compose down && cd /srv/navidrome && sudo docker compose down'
sudo rsync -aHAX --delete --info=progress2 \
  nthncrtr@old-natto.tailaf7ea6.ts.net:/srv/ /srv/
```

`/mnt/media` would need an equivalent rsync — but that's ~2.5TB of media over the LAN and likely not worth it. If the drive can't move, the realistic plan is "keep `/mnt/media` mounted on old natto, re-export it to new natto over NFS or SMB, and accept the latency hit." That's out of scope for this runbook.

## Rollback

If the new host doesn't work and DNS is already cut over:

1. Revert the Cloudflare A records to the old natto's tailnet IP.
2. Re-power the old natto. Tailscale should reconnect and pick up its old IP. (If the IP changed, update the Cloudflare records again to the *new* old-natto IP.)
3. Old containers should auto-start (`restart: unless-stopped`). If not: `cd /srv/<svc> && sudo docker compose up -d` for each.
4. If you already physically moved the 5TB drive: move it back. Old natto's `/etc/fstab` line uses the same UUID and will pick it up on the next `mount /mnt/media`.

If the new host is fine but you want to roll *backups* back: extract an older `/mnt/media/backups/natto-*.tgz` tarball with the same `tar --exclude='/usr/local/bin/caddy' -xzPf ... -C /` pattern.

## Gaps found during dry-run

Each entry: date, what failed/needed manual intervention, and the fix.

- *(empty — full cold-start dry-run hasn't happened yet; idempotency-only dry-run on natto on 2026-05-09 surfaced one bug, fixed in commit `a497e30`: the Caddy rebuild check was mtime-based and would falsely fire on any fresh repo clone.)*
