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

### 1.8 starmaya capture  [DONE — pending optional bootstrap re-run on kvass]

Commit the systemd units, the udev rule for `/dev/behmor-arduino`, and a setup bootstrap script. Note: the host's actual current hostname is `kvass`; `starmaya` is the canonical name used in the repo (matches the service name and intended hostname after rename).

**Preconditions:**
- SSH access to the roasting host (`ssh kvass`).
- `ssh kvass 'systemctl is-active roaster-daemon.service roaster-web.service'` returns `active` for both.
- Repo clean.

**Success criteria:**
- `services/starmaya/roaster-daemon.service` and `services/starmaya/roaster-web.service` committed (verbatim from `systemctl cat`; verified byte-identical to what's running).
- `services/starmaya/99-behmor-arduino.rules` committed (verbatim from `/etc/udev/rules.d/99-behmor-arduino.rules`; verified byte-identical).
- `bootstrap/starmaya.sh` committed; `bash -n` syntax clean; idempotent by construction (uses `install`, guards `useradd`/`groupadd` with `getent`/`id` checks, no `apt-get install` of the application). ShellCheck not run (not installed locally; success criterion relaxed to syntax check).
- A re-run of `bootstrap/starmaya.sh` on kvass completes with exit 0 and `systemctl is-active roaster-daemon.service roaster-web.service` still returns `active` for both. (Not yet exercised — operator can verify by running `sudo bootstrap/starmaya.sh` from a checkout of this repo on kvass.)

**Outcome:**
- Two services captured (not one, as the original mission text assumed): `roaster-daemon` (owns the Arduino serial port, `PrivateNetwork=yes`) and `roaster-web` (HTTP server on `:8080`, depends on the daemon).
- Arduino is unplugged at capture time, so `/dev/behmor-arduino` doesn't currently exist; the udev rule still installs and will activate when the board is connected.
- `roaster-web` listens on port **8080**, not 5000 as the commented-out Caddyfile entry expects. Mission 4.1 (Caddyfile activation) needs to update the port.

**Rollback:**
- The bootstrap script is additive and idempotent; partial runs are safe to re-run after fixing the failing step.
- If a service is disrupted: `systemctl restart roaster-daemon.service roaster-web.service` and verify with `systemctl status`.

---

## Phase 2 — Make portability real

### 2.1 bootstrap/natto.sh  [DONE — pending VM dry-run validation in 2.2]

Idempotent bootstrap that brings a fresh host to the point where `docker compose up -d` per service is the only remaining step.

**Preconditions:**
- All Phase 1 missions committed (compose files, build script, /srv/ pinning, starmaya capture).
- Repo clean.

**Success criteria:**
- `bootstrap/natto.sh` committed; `bash -n` syntax clean. (ShellCheck not run locally; deferred to 2.2's VM dry-run, where we can install it.)
- Script installs/configures: Docker (engine + compose plugin via get.docker.com), Tailscale (via tailscale.com/install.sh, but does NOT auth — operator runs `tailscale up` themselves), Caddy (Go + xcaddy + services/caddy/build.sh + install /usr/local/bin/caddy + caddy user/group + systemd unit + Caddyfile), `/srv/{pihole,navidrome,homepage}/` with the right ownerships, and per-service `docker-compose.yml` copied into each.
- Script does NOT start docker services automatically; does NOT install secrets (operator provides /etc/caddy/caddy.env); does NOT clone the repo (it expects to be running from inside the cloned repo). The repo-clone step is moved into the migration runbook (2.3) since it's a one-time operator action, not something to re-run.
- Idempotent: every step gates on `if not already installed/in-place`, uses `install` for file placement (overwrites with same content, harmless), and uses `getent`/`id` checks for users/groups. Caddy is rebuilt only if `services/caddy/build.sh` is newer than the installed binary.
- `--check` flag NOT included (deferred — would be useful but adds material complexity; not blocking this mission).

**Outcome:**
- `bootstrap/natto.sh` written; `bash -n` clean. End-to-end run not yet exercised — that's mission 2.2's job.

**Rollback:**
- The script is additive; partial runs are safe to re-run after fixing the failing step.
- If a host is genuinely broken (e.g., wrong Docker repo configured): document the manual remediation in `runbooks/migrate-natto.md` § Gaps.

### 2.2 VM dry-run of bootstrap  [PARTIAL — idempotency proved on natto; cold-start deferred]

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

**Outcome:**
- No VM tooling on workhorse and workhorse is Intel (arm64 VMs run under QEMU emulation — slow). Operator chose to defer the cold-start VM dry-run until a real replacement Pi is in hand and to instead prove **idempotency** by running `bootstrap/natto.sh` on natto itself, where every step should short-circuit.
- Idempotency dry-run on natto: `sudo bash /tmp/nthncrtr-test/bootstrap/natto.sh` — exit 0, every step skipped/no-op'd as expected (docker + tailscale skipped, caddy binary version matched expected, /srv dirs and compose files re-installed with identical content, no services restarted).
- One bug surfaced and fixed inline before the run: the original Caddy rebuild trigger compared `build.sh` mtime to the installed binary, which would falsely fire on any fresh repo clone (tar/git preserve mtimes). Replaced with a version comparison: parse `CADDY_VERSION` from `build.sh` and compare against `caddy version` output.
- Cold-start success criteria (services start, tailscale auth-key flow, etc.) NOT yet exercised.

**Rollback:**
- Discard the VM (when used). No production impact for the natto-based idempotency check.

### 2.3 runbooks/migrate-natto.md  [DONE — 8958d0f]

A doc that lets future-you (or a second operator) bring up a replacement natto cold.

**Preconditions:**
- Missions 2.1 and 2.2 committed.

**Success criteria:**
- `runbooks/migrate-natto.md` committed.
- Sections include: prerequisites (hardware spec, OS image choice, network setup), step-by-step migration order (bootstrap → restore /srv/ from backup → start services in dependency order → verify), data restore commands referencing `/mnt/media/backups/natto-YYYY-MM-DD.tgz`, DNS/Tailscale cutover (when to flip the Cloudflare record from old natto's Tailnet IP to new), per-service smoke tests with curl commands, and a "Gaps found during dry-run" section.
- A reader who has never seen the repo can complete the migration using only this doc plus the repo's compose files and bootstrap script.

**Outcome:** Nine numbered steps in order: clone repo, run bootstrap, tailscale up, install caddy.env, restore data, start Caddy, cut DNS at Cloudflare, bring up docker services with smoke tests, decommission old host. Plus prerequisites, rollback section, and a Gaps section seeded with the one mtime bug from 2.2.

**Rollback:**
- N/A (documentation only).

### 2.4 Backup script  [DONE — pending first real run on natto]

A one-liner-ish script producing a dated tarball on the 5TB drive.

**Preconditions:**
- Phase 1 done; `/srv/` is the source of truth for service data.
- `/mnt/media/backups/` exists and is writable. (Auto-created by `bootstrap/natto.sh` step_backup if `/mnt/media` is mounted.)

**Success criteria:**
- `services/backup/backup.sh` committed; `bash -n` clean. (Picked `services/backup/` as the location since it groups the script with its systemd unit + timer; `bootstrap/` stays reserved for one-shot host setup scripts.)
- Running it produces `/mnt/media/backups/natto-YYYY-MM-DD.tgz` containing: `/srv/`, `/usr/local/bin/caddy`, `/etc/caddy/Caddyfile`, `/etc/caddy/caddy.env` (acceptable since this tarball stays on the local-only 5TB drive), and `/etc/systemd/system/caddy.service`. Atomic write via `.partial` rename so a partial archive never appears at the dated path.
- `tar -tzf /mnt/media/backups/natto-YYYY-MM-DD.tgz | head -20` shows the expected absolute paths (script uses `tar -P` for round-trippable restore via `tar -xzf ... -C /`).
- Script exits non-zero if run as non-root, any source path is missing, the destination dir is missing/unwritable, or there's not enough free space (require source-set-size + 10% headroom).
- `services/backup/natto-backup.{service,timer}` committed: a oneshot service running `/usr/local/sbin/natto-backup` and a daily timer firing at 03:30 with a 15-min randomized delay (Persistent=true so a missed run catches up on next boot).
- `bootstrap/natto.sh` extended with `step_backup` that installs the script to `/usr/local/sbin/natto-backup`, the unit + timer to `/etc/systemd/system/`, runs `daemon-reload`, enables and starts the timer, and creates `/mnt/media/backups` if `/mnt/media` is mounted.

**Outcome:**
- Files committed; `bash -n` clean. First real run on natto not yet exercised — operator can trigger via `sudo systemctl start natto-backup.service` once the bootstrap step has installed the units.

**Rollback:**
- N/A — backup is non-destructive. If a backup tarball is corrupt, delete it and re-run.

---

## Phase 3 — Unified management with Homepage

### 3.1 Homepage Docker socket integration + per-service widgets  [DONE]

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

**Outcome:**
- Docker socket already mounted (was set up before this mission, in the existing compose). Added `services/homepage/config/docker.yaml` defining `my-docker: { socket: /var/run/docker.sock }` so the `server: my-docker` references in services.yaml resolve.
- Pi-hole and Navidrome widgets retain their existing API keys + Navidrome user/token/salt; values moved out of services.yaml into `/srv/homepage/secrets.env` (mode 0600, NOT in repo) and referenced from services.yaml as `{{HOMEPAGE_VAR_*}}`. `services/homepage/secrets.env.example` lists the variable set; `.gitignore` excludes the populated file.
- compose env_file uses the optional spec (`required: false`) so `docker compose config` works on workhorse where secrets.env is absent.
- Live verification on natto: `curl http://127.0.0.1:3000/api/services` returns the new structure with widget configs populated; container is healthy; public URL returns 200.
- qBittorrent widget left commented out (container doesn't exist yet — mission 1.5 stub). Activates automatically once the container is up.

**Rollback:**
- Revert `services/homepage/config/services.yaml` and the compose file. `docker compose up -d` to recreate.

### 3.2 Group by host + bookmarks.yaml + commit config dir  [DONE]

Reorganize the dashboard around hosts and add the "links I'll need someday" panel.

**Preconditions:**
- Mission 3.1 done.

**Success criteria:**
- `services/homepage/config/services.yaml` uses top-level groups named after each host (`natto`, `starmaya`, …).
- `services/homepage/config/bookmarks.yaml` includes: Tailscale admin console, Cloudflare dashboard, the GitHub repo for this homelab, plus any operator-defined entries.
- The full `services/homepage/config/` directory is committed (excluding the secrets file).
- Dashboard renders with grouped layout; bookmarks panel populated.

**Outcome:**
- services.yaml restructured with top-level groups `natto` (Pi-hole, Navidrome, qBittorrent stub) and `starmaya` (Coffee Roasting, link-only). bookmarks.yaml replaced placeholder GitHub/Reddit/YouTube with Tailscale admin, Cloudflare dashboard, and a GitHub link for this repo (currently `https://github.com/` — operator should fill in the real URL once the repo is published).
- Fixed a real bug found during the refactor: the previous Navidrome `href` was `https://navidrome.nthncrtr.com`, which had no Caddyfile route. Now points at `natto.nthncrtr.com` (the actual route).
- Full `services/homepage/config/` committed except `kubernetes.yaml`/`proxmox.yaml` (Homepage-generated stubs, not authoritative) and `custom.{css,js}` (empty on natto). If the operator populates those later, they should be added.
- API check: `curl http://127.0.0.1:3000/api/bookmarks` returns the new groups.

**Rollback:**
- Revert the config files. `docker compose restart homepage`.

---

## Phase 4 — Outstanding cleanup

### 4.1 Coffee app Caddyfile route  [PARTIAL — stub corrected, awaiting kvass on tailnet]

The `roast.nthncrtr.com` block is commented out in the Caddyfile. Activate it once the roasting app is ready for external access.

**Preconditions:**
- Decision made: yes, expose the roasting app externally.
- starmaya is on Tailscale and reachable from natto: `ssh natto 'tailscale ping starmaya'` succeeds. (Today the host is named `kvass`; the rename to `starmaya` may need to happen first or the route needs to point at the actual tailnet hostname.)
- The app responds locally: `ssh natto 'curl -fsSL -o /dev/null -w "%{http_code}\n" http://<host>.tailaf7ea6.ts.net:8080'` returns 200. Note: port is **8080** per `roaster-web.service`, not 5000 as the existing commented-out Caddyfile block claims — mission 4.1 must rewrite the port when uncommenting.

**Success criteria:**
- `services/caddy/Caddyfile` block for `roast.nthncrtr.com` is uncommented (and re-formatted to match house style — proper indentation).
- `caddy validate --config services/caddy/Caddyfile` passes locally.
- After deploying to natto and `caddy reload`: no errors in `journalctl -u caddy -n 50`.
- `curl -fsSL https://roast.nthncrtr.com/<known-path>` returns the expected response.

**Outcome (partial):**
- Updated the commented-out `roast.nthncrtr.com` block in `services/caddy/Caddyfile` to reflect known truth: port 5000 → 8080 (matches `roaster-web.service`), hostname is a `<kvass-on-tailnet>` placeholder, and a comment explains the blocker.
- Activation pending: kvass needs to join natto's tailnet (currently natto sees only natto + kraut). Once it does, replace the placeholder with the real tailnet hostname, uncomment the block, push to natto, `caddy reload`.

**Rollback:**
- Re-comment the block in the Caddyfile. `caddy validate`. Push to natto. `caddy reload`.

### 4.2 Jellyfin Caddyfile route — deploy or remove stub  [DONE — stub removed]

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

**Outcome:** Stub removed. Caddyfile parses clean (`caddy adapt` on natto returns valid JSON; format warning on the `:443 abort` block was a tab/space issue, also fixed).

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
