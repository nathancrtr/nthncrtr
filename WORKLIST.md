# Worklist

Each entry is a self-contained mission with three parts:
- **Preconditions** — what must be true before starting.
- **Success criteria** — explicit, testable assertions.
- **Rollback** — what to do if success criteria fail.

Anything that stops Pi-hole or reloads Caddy must be announced and confirmed before execution (per CLAUDE.md). Treat `/mnt/media` (the 5TB drive; music lives in `/mnt/media/music`) as read-mostly: backups OK, destructive ops not.

---

## Phase 1 — Capture what exists today

### 1.1 Caddyfile + caddy.service + build.sh  [DONE — 78498e5]

Snapshot of the running Caddyfile, systemd unit, and an `xcaddy` build script reproducing the binary (caddy v2.11.2 + caddy-dns/cloudflare v0.2.4, linux/arm64). No changes on natto.

### 1.2 Pi-hole compose file  [DONE — acebc60]

`services/pihole/docker-compose.yml` committed verbatim from natto's existing `/home/nthncrtr/docker/pihole-compose.yml`. Cross-checked against `docker inspect`: image, network mode, port bindings, env, mounts, restart policy all match.

### 1.3 Pi-hole cutover  [DONE (no-op) — 9d8223c]

Closed as a no-op: the running container was already created from the same compose file. DNS baseline verified (`dig @natto.local example.com` returned answers). User chose no force-recreate to avoid the ~30s outage with no functional change.

### 1.4 Navidrome compose + cutover

Capture `services/navidrome/docker-compose.yml` and ensure it reproduces the running container.

**Preconditions:**
- Repo `git status` clean.
- `ssh natto 'docker inspect navidrome'` succeeds.
- `curl -fsSL -o /dev/null -w '%{http_code}\n' https://natto.nthncrtr.com/ping` returns `200` (Navidrome's health endpoint).
- Music library reachable: `ssh natto 'ls /mnt/media/music | head'` returns content.
- Snapshot pre-cutover state: `ssh natto 'docker inspect navidrome > /tmp/navidrome.pre.json'`.

**Success criteria:**
- `services/navidrome/docker-compose.yml` committed; `docker compose config` parses cleanly.
- If a cutover is needed (i.e., the running container is *not* already managed by an equivalent compose file), after `docker compose up -d`: `docker ps --filter name=navidrome --format '{{.Status}}'` shows `Up … (healthy)` within 60s.
- `curl -fsSL https://natto.nthncrtr.com/ping` returns 200 post-cutover.
- Library track count (visible in Navidrome UI or via `/api/...`) is unchanged from pre-cutover snapshot.
- Diff of `docker inspect` pre/post shows no drift in image, env, mounts (especially the `/mnt/media/music` bind), ports (4533), network mode.

**Rollback:**
- `docker compose -f /path/to/compose down && docker start navidrome_old` (rename the existing container to `navidrome_old` *before* `compose up`, so this works).
- If `navidrome_old` is gone: `docker run -d --name navidrome --restart unless-stopped -p 4533:4533 -v /mnt/media/music:/music:ro -v /srv/navidrome/data:/data deluan/navidrome:latest` (adjust to whatever the prior image+mounts were per the pre-cutover snapshot).
- If the Navidrome scan database is corrupted post-rollback: restore `/srv/navidrome/data` (or wherever the data dir lives) from `/mnt/media/backups/`.

### 1.5 Torrent client compose + cutover

Caddyfile routes `torrent.nthncrtr.com` → `:8080`. Discover the container name (qBittorrent or Transmission per Phase 3 hints) before starting.

**Preconditions:**
- Repo clean.
- `ssh natto 'docker ps --format "{{.Names}}\t{{.Image}}" | grep -iE "qbit|transmission|torrent"'` identifies the container.
- `curl -fsSL -o /dev/null -w '%{http_code}\n' https://torrent.nthncrtr.com/` returns 200 or 401 (auth gate is fine).
- No active downloads at risk (operator awareness — pause if necessary).
- Snapshot pre-cutover state: `docker inspect <container> > /tmp/torrent.pre.json`.

**Success criteria:**
- `services/<torrent-name>/docker-compose.yml` committed; `docker compose config` parses.
- Post-cutover: container running and healthy; web UI loads; existing torrents listed and resume (verify with `docker exec` or via UI).
- Save paths preserved: pick one torrent and verify its destination directory matches pre-cutover.
- `docker inspect` diff shows no meaningful drift.

**Rollback:**
- `docker compose down && docker start <torrent>_old`.
- If state is corrupted: restore `/srv/<torrent>/config` from backup. Re-add lost torrents from `.torrent` files in `/srv/<torrent>/watch/` if present.

### 1.6 Homepage compose + cutover

Caddyfile routes `home.nthncrtr.com` → `:3000`.

**Preconditions:**
- Repo clean.
- `ssh natto 'docker inspect homepage'` succeeds.
- `curl -fsSL -o /dev/null -w '%{http_code}\n' https://home.nthncrtr.com` returns 200.
- Snapshot: `docker inspect homepage > /tmp/homepage.pre.json` and `ls -la <homepage_config_dir>`.

**Success criteria:**
- `services/homepage/docker-compose.yml` committed; `docker compose config` parses.
- Post-cutover: dashboard at `home.nthncrtr.com` loads with the same links and widgets visible pre-cutover (visual check).
- Homepage's `config/` directory remains bind-mounted to its existing host path (don't lose the operator's customization).
- `docker inspect` diff shows no meaningful drift.

**Rollback:**
- `docker compose down && docker start homepage_old`.
- Restore `/srv/homepage/config` from backup if config files were moved.

### 1.7 Pin Docker data paths to /srv/<service>/  [DONE]

Discovery showed no anonymous Docker volumes existed (all services already used host bind paths under `/home/nthncrtr/<svc>/`). Decision: relocate to `/srv/<svc>/` for convention, with both compose file and data co-located per service so relative `./<dir>` binds keep working. Notable: `/srv` is on the same filesystem as `/home/nthncrtr/` (the SD card root, currently 90% full) so the move is `mv` within one fs, not a physical relocation. SD-card pressure is a separate problem worth tracking elsewhere.

**Preconditions:**
- Missions 1.4–1.6 committed.
- `/srv/{navidrome,homepage,pihole}` exist on natto (created by sudo mkdir; navidrome and homepage owned by nthncrtr, pihole left root-owned to match the data inside).
- Each service is healthy via its public URL.

**Success criteria:**
- For each service: data dir lives under `/srv/<svc>/`, compose file lives at `/srv/<svc>/docker-compose.yml`, container is up and bind-mounting from the new location (verify with `docker inspect <c> --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}'`).
- Public URL returns 200 (or healthy app-specific status) post-move.
- For Pi-hole: announced and confirmed before stopping; DNS resolves again within ~30s of `docker compose up -d`.
- Repo `services/<svc>/docker-compose.yml` matches what's deployed (Navidrome and Homepage need no change since their relative paths work at both old and new locations; Pi-hole compose changes from `./pihole/etc-*` to `./etc-*`).

**Rollback:**
- For each service: `cd /srv/<svc> && docker compose down && mv <dir> /home/nthncrtr/<svc>/<dir> && cd /home/nthncrtr/<svc> && docker compose up -d`.
- For Pi-hole specifically: announce the rollback (DNS blip), then `mv` the etc-pihole and etc-dnsmasq.d back into `/home/nthncrtr/docker/pihole/`, restore the original `pihole-compose.yml` path semantics, `docker compose up -d` from `/home/nthncrtr/docker/`.

**Outcome:**
- Navidrome moved to `/srv/navidrome/` — inspect confirms `/srv/navidrome/data->/data`; `https://natto.nthncrtr.com/ping` → 200.
- Homepage moved to `/srv/homepage/` — inspect confirms `/srv/homepage/config->/app/config`; `https://home.nthncrtr.com` → 200.
- Pi-hole moved to `/srv/pihole/{etc-pihole,etc-dnsmasq.d}` — inspect confirms new paths; container healthy; `dig @natto.local example.com` returns answers; admin UI returns 200. Repo `services/pihole/docker-compose.yml` updated from `./pihole/etc-*` to `./etc-*` to match the new co-located layout.
- Old paths under `/home/nthncrtr/{navidrome,homepage,docker/pihole}/` left in place pending optional cleanup; the moved subdirs are gone but the parent dirs and the original `pihole-compose.yml` remain as a quiet record.

### 1.8 coffee-host capture

Commit the systemd unit, the udev rule for `/dev/behmor-arduino`, and a setup bootstrap script.

**Preconditions:**
- SSH access to coffee-host.
- `ssh coffee-host 'systemctl is-active <roaster>.service'` returns `active`.
- `ssh coffee-host 'ls -l /dev/behmor-arduino'` shows the symlink.
- Repo clean.

**Success criteria:**
- `services/coffee-host/<roaster>.service` committed (verbatim from `systemctl cat <roaster>.service`).
- `services/coffee-host/99-behmor-arduino.rules` committed (verbatim from `/etc/udev/rules.d/99-behmor-arduino.rules` or wherever it lives — find with `grep -r behmor /etc/udev`).
- `bootstrap/coffee-host.sh` committed; ShellCheck clean; idempotent (running it twice produces no errors and no second-run side effects beyond `apt update`).
- A re-run of `bootstrap/coffee-host.sh` on the live coffee-host completes with exit 0 and the roaster service is still active afterward.

**Rollback:**
- The bootstrap script should be additive and idempotent; rollback for a partial run is to re-run after fixing.
- If the roaster service is disrupted: `systemctl restart <roaster>.service`. Verify with `systemctl status` and a manual reading from `/dev/behmor-arduino`.

---

## Phase 2 — Make portability real

### 2.1 bootstrap/natto.sh

Idempotent bootstrap that brings a fresh host to the point where `docker compose up -d` per service is the only remaining step.

**Preconditions:**
- All Phase 1 missions committed (compose files, build script, /srv/ pinning, coffee-host capture).
- Repo clean.

**Success criteria:**
- `bootstrap/natto.sh` committed; `shellcheck` clean.
- Script installs/configures: Docker (engine + compose plugin), Tailscale, the Caddy binary (built via `services/caddy/build.sh`), the Caddy systemd unit, `/srv/<service>/` directory tree, SSH `authorized_keys` for the operator, and clones/pulls this repo to a known path (e.g., `/opt/nthncrtr/repo`).
- Script does NOT start docker services automatically (operator runs `docker compose up -d` per service after verifying mounts).
- Idempotent: a second run on the same host produces zero state changes (verify by capturing `find / -newer /tmp/marker -type f` between runs, or just confirming exit 0 with no warnings).
- Includes a `--check` flag that asserts each step's expected state without making changes.

**Rollback:**
- The script is additive; partial runs are safe to re-run after fixing the failing step.
- If a host is genuinely broken (e.g., wrong Docker repo configured): document the manual remediation in `runbooks/migrate-natto.md` § Gaps.

### 2.2 VM dry-run of bootstrap

Prove the script runs end-to-end on a clean host without leaning on the live natto.

**Preconditions:**
- Mission 2.1 committed.
- Multipass installed on workhorse (`brew install multipass`) OR a UTM VM with a fresh arm64 Ubuntu/Raspbian image, OR a spare Pi.
- Target VM has SSH enabled and can reach the internet.

**Success criteria:**
- `bootstrap/natto.sh` runs to exit 0 on the VM.
- After completion: `caddy version` reports v2.11.2; `docker --version` and `docker compose version` work; `tailscale status` is functional (auth-key flow OK); `/srv/{pihole,navidrome,homepage,...}` directories exist; `/etc/systemd/system/caddy.service` is in place; the repo is checked out at the expected path.
- At least one service smoke test: `docker compose -f services/<svc>/docker-compose.yml up -d` on the VM brings up the container (data dirs will be empty — accept "service starts and responds" as the bar).
- Every manual fix-up the operator made during the dry-run is recorded in `runbooks/migrate-natto.md` § "Gaps found during dry-run" with the date.
- VM is destroyed after the run (`multipass delete --purge <name>`).

**Rollback:**
- Discard the VM. No production impact.

### 2.3 runbooks/migrate-natto.md

A doc that lets future-you (or a second operator) bring up a replacement natto cold.

**Preconditions:**
- Missions 2.1 and 2.2 committed.

**Success criteria:**
- `runbooks/migrate-natto.md` committed.
- Sections include: prerequisites (hardware spec, OS image choice, network setup), step-by-step migration order (bootstrap → restore /srv/ from backup → start services in dependency order → verify), data restore commands referencing `/mnt/media/backups/natto-YYYY-MM-DD.tgz`, DNS/Tailscale cutover (when to flip the Cloudflare record from old natto's Tailnet IP to new), per-service smoke tests with curl commands, and a "Gaps found during dry-run" section.
- A reader who has never seen the repo can complete the migration using only this doc plus the repo's compose files and bootstrap script.

**Rollback:**
- N/A (documentation only).

### 2.4 Backup script

A one-liner-ish script producing a dated tarball on the 5TB drive.

**Preconditions:**
- Phase 1 done; `/srv/` is the source of truth for service data.
- `/mnt/media/backups/` exists and is writable (`ssh natto 'touch /mnt/media/backups/.test && rm /mnt/media/backups/.test'`).

**Success criteria:**
- `bootstrap/backup.sh` committed (or `services/backup/backup.sh` — pick one and stay consistent).
- Running it produces `/mnt/media/backups/natto-YYYY-MM-DD.tgz` containing: `/srv/`, `/usr/local/bin/caddy`, `/etc/caddy/Caddyfile`, `/etc/caddy/caddy.env` (acceptable since this tarball stays on the local-only 5TB drive), and `/etc/systemd/system/caddy.service`.
- `tar -tzf /mnt/media/backups/natto-YYYY-MM-DD.tgz | head -20` shows expected paths.
- Script exits non-zero if any source path is missing or the destination drive is full.
- A systemd timer (`bootstrap/systemd/natto-backup.timer` + `.service`) committed for daily runs at a low-traffic hour, or an equivalent cron entry — choose one.

**Rollback:**
- N/A — backup is non-destructive. If a backup tarball is corrupt, delete it and re-run.

---

## Phase 3 — Unified management with Homepage

### 3.1 Homepage Docker socket integration + per-service widgets

Mount the Docker socket read-only and add widget configs so the dashboard shows live status.

**Preconditions:**
- Phase 1 complete (all services compose-managed; `/srv/` pinned).
- Homepage running and reachable at `https://home.nthncrtr.com`.
- API tokens available for: Pi-hole (admin password or app-password), Navidrome, qBittorrent/Transmission, Tailscale.
- Decide on a secrets strategy (NOT committed to git): env file referenced by Homepage, or `.env` in `services/homepage/` listed in `.gitignore`.

**Success criteria:**
- `services/homepage/docker-compose.yml` updated to mount `/var/run/docker.sock:/var/run/docker.sock:ro` and to load secrets from the chosen secrets file.
- `services/homepage/config/services.yaml` includes a `docker:` block per service that references the container by name; live status (running/stopped, CPU, memory) renders inline next to each link.
- Per-service widgets render real values: Pi-hole shows queries blocked in last 24h; Navidrome shows track count and now-playing; torrent client shows session DL/UL stats; Tailscale shows online peers count.
- Secrets file is `.gitignore`d; a `.env.example` with placeholder keys is committed.
- `https://home.nthncrtr.com` loads with all widgets populated within 5s.

**Rollback:**
- Revert `services/homepage/config/services.yaml` and the compose file. `docker compose up -d` to recreate.

### 3.2 Group by host + bookmarks.yaml + commit config dir

Reorganize the dashboard around hosts and add the "links I'll need someday" panel.

**Preconditions:**
- Mission 3.1 done.

**Success criteria:**
- `services/homepage/config/services.yaml` uses top-level groups named after each host (`natto`, `coffee-host`, …).
- `services/homepage/config/bookmarks.yaml` includes: Tailscale admin console, Cloudflare dashboard, the GitHub repo for this homelab, plus any operator-defined entries.
- The full `services/homepage/config/` directory is committed (excluding the secrets file).
- Dashboard renders with grouped layout; bookmarks panel populated.

**Rollback:**
- Revert the config files. `docker compose restart homepage`.

---

## Phase 4 — Outstanding cleanup

### 4.1 Coffee app Caddyfile route

The `roast.nthncrtr.com` block is commented out in the Caddyfile. Activate it once the roasting app is ready for external access.

**Preconditions:**
- Decision made: yes, expose the roasting app externally.
- coffee-host is on Tailscale and reachable from natto: `ssh natto 'tailscale ping coffee-host'` succeeds.
- The app responds locally: `ssh natto 'curl -fsSL -o /dev/null -w "%{http_code}\n" http://coffee-host.tailaf7ea6.ts.net:5000'` returns 200 (or whatever the app's healthy response is).

**Success criteria:**
- `services/caddy/Caddyfile` block for `roast.nthncrtr.com` is uncommented (and re-formatted to match house style — proper indentation).
- `caddy validate --config services/caddy/Caddyfile` passes locally.
- After deploying to natto and `caddy reload`: no errors in `journalctl -u caddy -n 50`.
- `curl -fsSL https://roast.nthncrtr.com/<known-path>` returns the expected response.

**Rollback:**
- Re-comment the block in the Caddyfile. `caddy validate`. Push to natto. `caddy reload`.

### 4.2 Jellyfin Caddyfile route — deploy or remove stub

The commented `jellyfin.nthncrtr.com` block in the Caddyfile is a smell. Either deploy Jellyfin or delete the stub.

**Preconditions:**
- Decision made: deploy Jellyfin OR remove the stub.
- (Deploy path) Mission 4.3 committed first, so the media directory layout is settled.

**Success criteria — deploy path:**
- `services/jellyfin/docker-compose.yml` committed; container running on the planned host (likely natto, possibly elsewhere).
- The `jellyfin.nthncrtr.com` Caddyfile block is uncommented and points at the right host:port.
- `caddy validate` passes; `caddy reload` succeeds.
- `https://jellyfin.nthncrtr.com` loads the Jellyfin web UI.

**Success criteria — remove path:**
- The commented `jellyfin.nthncrtr.com` block is deleted entirely from `services/caddy/Caddyfile`.
- `caddy validate` passes.

**Rollback:**
- Revert the Caddyfile change. If a Jellyfin container was started, `docker compose down && docker volume rm <vols>` if disposable; otherwise leave it stopped pending re-decision.

### 4.3 Media directory layout decision

Right now Navidrome serves from `/mnt/media/music`. If Jellyfin is on the table, decide the directory structure now while the media tree is small enough to reorganize. Note: `/mnt/media/music` currently contains a mix of music plus stray files (logs, bin/, config/) that should also be sorted out as part of this mission.

**Preconditions:**
- Phase 1 done (Navidrome compose pins its mount path explicitly).
- Current `/mnt/media` size is small enough to move comfortably: `ssh natto 'du -sh /mnt/media'` returns a number you're willing to copy.

**Success criteria:**
- `runbooks/media-layout.md` committed describing the chosen layout (e.g., `/mnt/media/{music,video,audiobooks,...}`) and the rationale (why this split, what's reserved for future media types).
- Navidrome's bind mount in `services/navidrome/docker-compose.yml` reflects the chosen subdirectory (e.g., `/mnt/media/music:/music:ro`).
- A future `services/jellyfin/docker-compose.yml` would mount the corresponding video subdirectory; the runbook spells out exactly what mount line to use.
- After any migration: music currently served by Navidrome is still accessible (no broken paths in Navidrome's library scan).

**Rollback:**
- Move data back to the prior layout. Revert compose file. Restart Navidrome. The directory move is reversible as long as the operation is a copy-then-verify-then-delete, not a rename in flight.
