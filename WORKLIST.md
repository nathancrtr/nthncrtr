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

### 2.2 VM dry-run of bootstrap  [DONE — real cold-start executed 2026-05-16 (Pi→Beelink); 9 gaps recorded in runbook]

> The deferred cold-start happened for real, not in a VM: the live
> Pi→Beelink migration on 2026-05-16. It succeeded; nine gaps were found
> and either fixed in-repo (`3ba3869`, `a684f1c`, `e13723b`, `5cffc0a`) or
> recorded — see `runbooks/migrate-natto.md` Gaps §"2026-05-16". One open
> follow-up: host-wide MagicDNS vs Pi-hole-owns-`:53` (see Phase 4).

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

### 2.5 deploy.sh — push repo changes to natto without ad-hoc scp/rsync

Replace per-change `scp services/<svc>/* natto:/srv/<svc>/` + manual `docker compose up -d` with a single in-repo script that knows each service's apply step and honors the safety rules. Motivated by the upcoming natto migration: ad-hoc deploys don't carry across hosts, but a checked-in script does.

**Preconditions:**
- Repo clean.
- `/srv/nthncrtr-repo` exists on natto and is a clone of this repo via SSH (passphrase-less Deploy key generated by `bootstrap/natto.sh` `step_deploy_key`; SSH config maps `github.com` → that key).
- `git -C /srv/nthncrtr-repo pull` succeeds as the UID-1000 user.

**Success criteria:**
- `deploy.sh` committed at repo root; `bash -n` clean; mode 0755.
- Per-service apply logic for: caddy (validate via `caddy adapt` before any file touches `/etc/caddy/`; reload only if files changed; `daemon-reload` only if unit changed), navidrome/homepage (compose up -d; homepage rsyncs `config/` *without* `--delete` so runtime state like `logs/` and homepage's auto-generated stub yamls survive), pihole (gated behind `--yes-pihole`; even with the flag, skip recreate if compose unchanged), backup (install script + units + enable timer), starmaya (over `ssh -t kvass` for interactive sudo at the kvass end).
- Pi-hole and starmaya default-skipped — must be requested by name and (for pihole) by `--yes-pihole`.
- `--dry-run` shows unified diff for each file that would change, prefixes rsync with `-n`, prints "would: …" for compose actions, and applies nothing.
- `install_file` helper does cmp-then-install (no-op when bytes identical) and sets a CHANGED flag the caller uses to decide whether to reload. Re-running with no repo change is effectively a no-op (compose `up -d` runs but is itself a no-op when nothing changed).
- Dirty-tree warning: deploy continues but emits a `warn` with `git status -s` output, so the operator notices.
- `bootstrap/natto.sh` extended with `step_deploy_key` (between `step_tailscale` and `step_caddy`): generates a passphrase-less ed25519 keypair as the UID-1000 user, appends a `Host github.com` block to that user's `~/.ssh/config` (idempotent — only appends if not present), and prints the pubkey when `/srv/nthncrtr-repo` isn't yet cloned. Operator action remains: add pubkey to GitHub as a Deploy key, then `git clone` to `/srv/nthncrtr-repo`. Banner updated with step 6 describing this.
- `CLAUDE.md` gains a "Deploying repo changes to natto" subsection at the top of Workflow patterns; "Sudo on natto from workhorse" gets a one-liner pointing at `deploy.sh` for non-one-off changes.
- End-to-end dry-run from natto: `cd /srv/nthncrtr-repo && git pull && sudo ./deploy.sh --dry-run` exits 0, shows expected diffs (or none if natto is already in sync), and the services it would touch remain healthy.

**Outcome:**
- (to be filled in after the dry-run lands)

**Rollback:**
- `deploy.sh` is additive — removing the file affects no service. Per-service rollback follows the existing cutover pattern (snapshot pre-state, `docker compose down`, restore).
- If a deploy lands bad config: `git -C /srv/nthncrtr-repo checkout <prev-commit> -- services/<svc>/...` then re-run `deploy.sh <svc>`.

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

### 4.1 Coffee app Caddyfile route  [DONE]

External access to the coffee roasting app at `https://starmaya.nthncrtr.com`.

**Preconditions (all met):**
- Decision made: yes, expose the roasting app externally.
- kvass is on natto's tailnet as `kvass.tailaf7ea6.ts.net` (joined between sessions; visible in `ssh natto 'tailscale status'`).
- The app responds via tailnet: `ssh natto 'curl -fsSL -o /dev/null -w "%{http_code}\n" http://kvass.tailaf7ea6.ts.net:8080'` returns 200.

**Success criteria (all met):**
- `services/caddy/Caddyfile` block for `starmaya.nthncrtr.com` is uncommented and reverse-proxies to `kvass.tailaf7ea6.ts.net:8080`.
- `caddy adapt` passes against the new Caddyfile.
- After deploying to natto and `systemctl reload caddy`: cert provisioned via DNS-01 in ~9s, no errors in `journalctl -u caddy`.
- `curl -fsSL https://starmaya.nthncrtr.com` returns the React app HTML (`<title>Starmaya — Roast Logger</title>`).

**Outcome:**
- Cloudflare A record for `starmaya.nthncrtr.com` set to natto's tailnet IP (`100.110.225.55`), matching the `*.nthncrtr.com → natto → Caddy → backend` pattern used by every other route.
- Caddyfile block uncommented in `services/caddy/Caddyfile` and deployed to `/etc/caddy/Caddyfile` on natto via the sudo-clipboard pattern; `systemctl reload caddy` triggered automatic cert provisioning via Cloudflare DNS-01.
- The same deploy also picked up two prior unpushed repo changes: removal of the old `roast.nthncrtr.com` stub and removal of the Jellyfin stub from mission 4.2 (which had only been done in the repo, never on natto).

**Rollback:**
- Re-comment the block in `services/caddy/Caddyfile`. `caddy adapt` to validate. Push to natto. `systemctl reload caddy`.

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

### 4.3 Media directory layout decision  [DONE]

Right now Navidrome serves from `/mnt/media/music`. If Jellyfin is on the table, decide the directory structure now while the media tree is small enough to reorganize. Note: `/mnt/media/music` currently contains a mix of music plus stray files (logs, bin/, config/) that should also be sorted out as part of this mission.

**Preconditions:**
- Phase 1 done (Navidrome compose pins its mount path explicitly).
- Current `/mnt/media` size is small enough to move comfortably: `ssh natto 'du -sh /mnt/media'` returns a number you're willing to copy.

**Success criteria:**
- `runbooks/media-layout.md` committed describing the chosen layout (e.g., `/mnt/media/{music,video,audiobooks,...}`) and the rationale (why this split, what's reserved for future media types).
- Navidrome's bind mount in `services/navidrome/docker-compose.yml` reflects the chosen subdirectory (e.g., `/mnt/media/music:/music:ro`).
- A future `services/jellyfin/docker-compose.yml` would mount the corresponding video subdirectory; the runbook spells out exactly what mount line to use.
- After any migration: music currently served by Navidrome is still accessible (no broken paths in Navidrome's library scan).

**Outcome:**
- `runbooks/media-layout.md` committed describing target layout `/mnt/media/{music,video,backups,_unsorted/}` with rationale, mount details, migration steps, and rollback.
- Reorganization executed on natto: created `video/`, `backups/`, `_unsorted/{from-mnt-media-root,from-mnt-media-music}/`. Moved 9 root-level junk entries into `_unsorted/from-mnt-media-root/` (Autorun.inf, Seagate, Start_Here_*, Warranty.pdf, .VolumeIcon.{icns,ico}, ._). Collapsed the music nesting: previous /mnt/media/music/ contained ~62 mixed installer artifacts plus a /music subdir with the actual library; rotated to /mnt/media/music_old, lifted the real music up, swept the rest into `_unsorted/from-mnt-media-music/`. `System Volume Information` left in place (Windows artifact; recreates if touched).
- 778 album dirs in /mnt/media/music after the move, matching pre-move count.
- Navidrome compose mount unchanged (/mnt/media/music:/music:ro stays the same path). Navidrome restarted; rescan in progress (live verified via container logs — albums importing at ~1-10s each, expected total 30 min – 2 h for 257 GB).

**Rollback:**
- See `runbooks/media-layout.md` § Rollback for the reverse-direction script. All moves stayed within the exfat fs so they're truly reversible without re-copying.

### 4.4 Host-wide MagicDNS vs Pi-hole-owns-:53  [DONE 2026-05-16 — nsswitch `resolve`]

Pi-hole binds `0.0.0.0:53`, so systemd-resolved's stub listener is disabled.
Diagnosis showed resolved itself was **already correct**: it had the
Tailscale split-DNS route (`tailscale0` → `100.100.100.100` for
`tailaf7ea6.ts.net`) *and* the public path (`enp1s0` → upstreams), and
`resolvectl query kvass.tailaf7ea6.ts.net` worked. The only break was
`/etc/nsswitch.conf` = `hosts: files dns` — no `resolve` entry, so glibc
bypassed resolved entirely.

**Fix applied:** `hosts: files resolve [!UNAVAIL=return] dns` (+ ensure
`libnss-resolve` installed). Host-only change; Pi-hole/`:53`/household
untouched; no host→container dependency. Rejected alternatives: resolv.conf
→ `100.100.100.100` (Tailscale resolver returns nothing for public names in
this tailnet — would break public DNS); Pi-hole conditional-forward (needs
the host repointed at the Pi-hole container — fragile, no gain).

**Verified:** `getent hosts kvass.tailaf7ea6.ts.net` + public both resolve;
`dig @192.168.1.50 example.com` still answers; `*.nthncrtr.com` smoke set
unchanged. Caddyfile keeps the pinned kvass IP **on purpose** — Go's pure
resolver bypasses nsswitch/resolved (reads resolv.conf directly).

**Open sub-item (lower priority):** fold the host-DNS prep (resolved-stub
drop-in + nsswitch `resolve`) into `bootstrap/natto.sh` so a future
Ubuntu/systemd-resolved cold-start doesn't re-hit migration Gaps 4 & 6
manually. The runbook documents the manual steps in the meantime.

**Success criteria:**
- `getent hosts kvass.tailaf7ea6.ts.net` resolves on natto.
- Pi-hole still owns `:53` for the household; `dig @192.168.1.50 example.com` still answers.
- No regression to the `*.nthncrtr.com` smoke set.

---

## Phase 5 — Self-hosted Google Drive replacement (Nextcloud)

Goal: a one-time migration *off* Google Drive onto self-hosted Nextcloud,
Tailscale-only. Decided with the operator: model = one-time data liberation
(not ongoing sync); reach = Tailscale-only; storage = Beelink internal ext4
(Drive is < 50 GB); sequencing = repo scaffolding now, activates at the
Pi → Beelink cutover (nothing deploys to the current Pi).

### 5.1 Storage decision + Nextcloud service scaffolding  [DONE — scaffolding; activation pending Beelink cutover]

**Preconditions:**
- Operator decisions captured (model / reach / storage / sequencing — see above).
- Repo `git status` clean before starting.

**Success criteria:**
- Storage resolved: Nextcloud data + DB on the Beelink's internal ext4 at
  `/srv/nextcloud/{html,data,db}` — *not* the exfat 5TB (exfat can't give the
  DB/data POSIX semantics, and the 5TB must stay exfat for the migration
  design + safety rule #3). < 50 GB Drive fits internal with headroom.
- `services/nextcloud/` committed: `docker-compose.yml` (nextcloud:stable
  apache + mariadb:lts + redis:alpine + cron sidecar), `README.md`,
  `secrets.env.example`, `.gitignore` (excludes `secrets.env`). Tailscale-only
  — deliberately **no** Caddyfile/Cloudflare route.
- `bootstrap/natto.sh` `step_srv` creates `/srv/nextcloud` + installs the
  compose; banner lists the secrets file + bring-up.
- `deploy.sh` gains `deploy_nextcloud` (creates the bind dirs, warns on
  missing `secrets.env`, `compose up`, verifies `127.0.0.1:8081/status.php`);
  `nextcloud` added to the default service set + usage.
- Nothing deploys to the live Pi.

**Outcome:**
- All of the above committed. Activation deferred to mission 5.4 (the
  Pi → Beelink cutover) — the Pi can't host this well and a throwaway
  deploy there would be wasted effort.

**Rollback:**
- Additive only — `git revert` the scaffolding commit; no running service is
  affected (nothing deployed yet).

### 5.2 Backup integration  [DONE]

**Preconditions:** Mission 5.1 committed.

**Success criteria:**
- `backup.sh` excludes `/srv/nextcloud/{data,db}` from the nightly tar (a
  hot InnoDB datadir tar is unrestorable; user data too large to duplicate
  nightly), and writes a guarded logical `mariadb-dump` →
  `/srv/nextcloud/db-dump.sql.gz` (small, *is* tarred). Dump is skipped
  silently when `nextcloud-db` is absent (current Pi) and a dump failure
  warns but does not fail the whole backup. Free-space `du` honors the same
  excludes so the estimate isn't inflated.
- New weekly `nextcloud-data-sync.{sh,service,timer}` rsyncs
  `/srv/nextcloud/data` → `/mnt/media/backups/nextcloud-data/` as a single
  `--delete` mirror (one copy, not 7 daily dupes — operator's choice). No-op
  exit 0 when Nextcloud isn't deployed yet.
- `bootstrap` `step_backup` + `deploy.sh` install all six backup files and
  enable both timers. `services/backup/README.md` documents the split and
  the restore procedure.

**Outcome:** Committed. First real exercise happens post-cutover (5.4).

**Rollback:** `git revert`; backup is non-destructive. Old `backup.sh`
behavior (tar all of `/srv`) is restored by the revert.

### 5.3 Google Drive migration runbook  [DONE — execution pending operator]

**Preconditions:** Missions 5.1–5.2 committed.

**Success criteria:**
- `runbooks/migrate-off-gdrive.md` committed: rclone remote setup (with the
  own-client-id rate-limit warning), the Google-native export-format decision
  (MS Office recommended; ODF / PDF alternatives spelled out), dry-run sizing
  gate against the < 50 GB assumption, copy → chown → `occ files:scan`,
  verification checklist, and an explicit "deleting from Google is manual and
  out of scope" boundary.
- `runbooks/migrate-natto.md` threaded: Nextcloud secrets in Prerequisites;
  § 5b restore (DB dump + weekly data mirror, since `data`/`db` aren't in the
  tarball); § 8 bring-up + smoke; note that it's Tailscale-only so absent
  from the § 7 DNS cutover.
- `CLAUDE.md` architecture table + repo-layout tree updated (drift fix).

**Outcome:** Committed. The actual Drive pull is an operator action, run once
after 5.4, following the runbook.

**Rollback:** Documentation only — `git revert`. The runbook's own rollback
section covers a botched copy (additive; Google untouched until verified).

### 5.4 Cutover activation  [SUPERSEDED by 6.1 — narrowed to deploy+verify; Drive pull split out]

> The Pi→Beelink migration completed 2026-05-16, so this is no longer
> *gated* — just not yet done (operator chose to defer Nextcloud during the
> cutover). Old natto never ran Nextcloud, so this is the **initial** Drive
> migration (`runbooks/migrate-off-gdrive.md`), not a restore. Note: the
> tailnet-name success check below is reached *from another tailnet node*,
> not from natto itself (no MagicDNS on the host post-migration).

**Preconditions:**
- Missions 5.1–5.3 committed.
- Pi → Beelink migration underway per `runbooks/migrate-natto.md` (Beelink
  bootstrapped, `/srv` restored, 5TB moved).
- `/srv/nextcloud/secrets.env` provisioned (mode 0600).

**Success criteria:**
- `deploy.sh nextcloud` (or the § 8 manual bring-up) starts all four
  containers; `127.0.0.1:8081/status.php` → 200 and
  `occ status` → `installed: true`.
- Reachable on the tailnet at `http://natto.tailaf7ea6.ts.net:8081`; admin
  login works; no Administration → Overview warnings.
- Nightly backup produces a fresh `/srv/nextcloud/db-dump.sql.gz` inside the
  tarball; `nextcloud-data-sync.timer` produces
  `/mnt/media/backups/nextcloud-data/` on its first weekly run (or a manual
  `systemctl start nextcloud-data-sync.service`).
- Then, once: execute `runbooks/migrate-off-gdrive.md`.

**Outcome:** (to be filled in at cutover.)

**Rollback:**
- Nextcloud is independent of every other service — `cd /srv/nextcloud &&
  docker compose down` removes it with zero impact on DNS/Caddy/the rest.
  Re-run when ready.

---

## Phase 6 — Post-migration services on the more capable hardware

The Pi → Beelink migration (Phase 5.4 gate lifted 2026-05-16) unlocked
services the Pi couldn't host well. Operator decisions captured for this
phase: Jellyfin + Nextcloud are **Tailscale-only / LAN** (no Caddy route, no
Cloudflare DNS — local streaming + personal data only); Jellyfin uses the
Beelink's Intel QuickSync via `/dev/dri`; SMB/Samba is dropped as an
unsupported feature (docs cleaned, not just unimplemented).

### 6.1 Nextcloud activation  [supersedes 5.4 — deploy + verify only]

Scope deliberately narrowed with the operator: bring the stack up and
verify it. The one-time Google Drive pull (`runbooks/migrate-off-gdrive.md`)
remains a separate operator action, not part of this mission.

**Preconditions:**
- Missions 5.1–5.3 committed (scaffolding, backup, runbook — all DONE).
- `/srv/nextcloud/secrets.env` provisioned on natto (mode 0600, root:root).

**Success criteria:**
- `deploy.sh nextcloud` starts all four containers; `127.0.0.1:8081/status.php`
  → 200 and `occ status` → `installed: true`.
- Reachable on the tailnet at `http://natto.tailaf7ea6.ts.net:8081`; admin
  login works; no Administration → Overview blocking warnings.
- No Caddyfile/Cloudflare change (Tailscale-only by design).

**Outcome:** Done 2026-05-16. `deploy.sh nextcloud` brought up all four
containers; `secrets.env` provisioned (root:root 0600, generated creds,
admin `nthncrtr`). `occ status` → `installed: true`, v33.0.3,
`maintenance: false`, `needsDbUpgrade: false`. `status.php` → 200 on both
`127.0.0.1:8081` and `http://natto.tailaf7ea6.ts.net:8081`. Caddyfile
untouched. Google Drive pull (`runbooks/migrate-off-gdrive.md`) remains
outstanding as a separate operator action.

**Rollback:** `cd /srv/nextcloud && docker compose down` — independent of
every other service, zero DNS/Caddy impact.

### 6.2 Jellyfin standup  [NEW]

**Preconditions:**
- `/mnt/media/video/{movies,tv}` populated on natto (verified — already is).
- `/dev/dri/renderD128` present on natto (verified — Intel QuickSync).
- Repo `git status` clean before the deploy.

**Success criteria:**
- `services/jellyfin/` committed: `docker-compose.yml` (lscr.io/linuxserver
  image, PUID/PGID 1000, `/mnt/media/video:/media/video:ro`, `/dev/dri`
  passthrough with host `render`/`video` gids, `:8096` + `:7359/udp`) and
  `README.md`. Tailscale-only — no Caddyfile/Cloudflare route.
- `deploy.sh` gains `deploy_jellyfin` (dirs, compose up, verify
  `127.0.0.1:8096/health`); `jellyfin` in the default service set + usage.
- `bootstrap/natto.sh` `step_srv` creates `/srv/jellyfin` (1000:1000) +
  installs the compose; banner lists the bring-up.
- `deploy.sh jellyfin` brings the container up; `127.0.0.1:8096/health` → 200.
- Reachable at `http://natto:8096` / `http://natto.tailaf7ea6.ts.net:8096`;
  Movies + Shows libraries import from `/media/video/{movies,tv}`.
- HW transcode: `/dev/dri/renderD128` visible in-container; QSV selectable
  under Dashboard → Playback.

**Outcome:** Done 2026-05-16. `deploy.sh jellyfin` created
`/srv/jellyfin/{config,cache}` (1000:1000) and brought the container up.
Web UI → 200 on `127.0.0.1:8096` and `http://natto.tailaf7ea6.ts.net:8096`.
`/dev/dri/{card0,renderD128}` present in-container (renderD128 shows the
linuxserver synthetic group name for host gid 991 — numeric supplementary
membership intact). Library import + QSV enablement are the operator's
first-run UI steps (services/jellyfin/README.md). Caddyfile untouched.

**Rollback:** `cd /srv/jellyfin && docker compose down`; revert the repo
commit. Config/cache under `/srv/jellyfin` is disposable; media untouched
(read-only mount).

### 6.3 Drop SMB/Samba as an unsupported feature  [DONE]

**Preconditions:** none (Samba already absent on the Beelink — never
reproduced at the migration).

**Success criteria:**
- `runbooks/migrate-natto.md`: smbd-stop step removed from §5a; the Samba
  clause removed from Gap §7; the standalone Samba Gap §8 deleted (items
  renumbered, cross-refs fixed); the NFS/SMB fallback line reworded to NFS.
  The generic "`fuser -vm` is the authority before `umount`" lesson kept.
- `CLAUDE.md`: the "Samba decommissioned" line replaced with an explicit
  "SMB/Samba is not a supported feature" statement.

**Outcome:** Committed. Documentation-only — there was no Samba config in
the repo to remove; the change makes the *unsupported* status explicit
rather than reading as a transient migration casualty.

**Rollback:** `git revert` — docs only, no running service affected.

### 6.4 SSO for the web-admin tier (Authelia + Caddy forward_auth)  [NEW — scaffolding committed; activation pending operator]

Collapse the per-app password-manager entries for the *arrs, qBittorrent
and Homepage to one Authelia login. Scope (operator decision): web-admin
tier only, optimised for convenience (one_factor) over a hard security
boundary — services already sit behind Tailscale/Caddy. Plex (plex.tv,
structurally impossible), Jellyfin (breaks non-browser clients), Navidrome,
Nextcloud and Pi-hole are explicitly out of scope.

**Preconditions:**
- Repo clean; `services/authelia/` scaffolding committed.
- Cloudflare DNS `auth.nthncrtr.com` record added (one-record-per-host —
  no wildcard) pointing as the other `*.nthncrtr.com` records do.
- `/srv/authelia/{secrets.env,users.yml}` provisioned on natto (0600,
  root:root) per services/authelia/README.md.

**Success criteria:**
- `deploy.sh authelia` (opt-in) brings the container up;
  `127.0.0.1:9091/api/authz/forward-auth` → 401.
- `deploy.sh caddy` (run *after*) reloads cleanly (`caddy adapt` passes).
- `https://radarr.nthncrtr.com` in a fresh browser → 302 to
  `auth.nthncrtr.com` → login → back to Radarr; `sonarr.nthncrtr.com`
  then does NOT re-prompt (shared `.nthncrtr.com` cookie).
- Homepage *arr/qBit widgets still render (API-key path unaffected:
  *arrs set to "Disabled for Local Addresses", qBit subnet-bypass kept).
- Navidrome / Jellyfin / Nextcloud / Pi-hole logins unchanged.

**Rollback:** revert the Caddyfile `import authelia` + portal/snippet,
`deploy.sh caddy` (validates, gate comes off), `docker compose down` in
/srv/authelia. Apps' own auth only local-disabled, never deleted —
re-enable per service. `/srv/authelia/data` disposable.

### 6.5 *arr "slow/non-functional" debug — doc drift fix + restart-orphan hardening  [PARTIAL — #1 shipped; #2/#3 designed, not built]

**Trigger:** operator reported Radarr/Sonarr downloads extremely slow
and/or non-functional while a hand-added qBit torrent worked fine.

**Diagnosis (2026-05-17, no live-service changes made):** Radarr is healthy
and fast (one grab, *Oddity 2160p*, 28.7 GB in 22 min ≈ 21 MB/s,
auto-imported). The fault is **not** speed: Sonarr's only grab (*Planet
Earth III S01E01*, sent to qBit 20:00) was orphaned when the seedbox commit
`9c25f5c` deployed at ~20:36 recreated the gluetun+qBit stack; the
no-metadata private-tracker torrent had no resume data, didn't survive, and
Sonarr silently reverted it to "missing" (no queue/blocklist/retry).
Indexers, VPN, forwarded port, disk all verified healthy; the earlier
"0 active indexers" was the (resolved) Prowlarr tag-sync gotcha.

**Success criteria:**
- Radarr/Sonarr READMEs no longer claim "stub — not yet deployed"; document
  the real wiring (categories, qBit global save path
  `/mnt/media/_unsorted/torrents`, exfat/no-hardlink copy-on-import, the
  ghost `tv-sonarr` category, the restart-orphan gotcha).
- Compose-file header comments corrected (no SD-card / 2 GB-Pi /
  `/mnt/media/downloads/complete/` drift).
- `deploy.sh qbittorrent` warns the operator to re-run Wanted→Missing after
  a stack (re)deploy (mitigation #1).
- `services/qbittorrent/README.md` carries the design for mitigations
  #2 (skip recreate on tuning-only change) and #3 (post-deploy *arr search
  hook) as the planned path.

**Outcome:** Repo-only change, committed. Live services untouched (operator
explicitly scoped to repo fixes). Mitigation #1 shipped; #2/#3 documented,
deferred. The operator still needs to: re-search the lost Planet Earth III
episodes (or Manual Import the hand-grabbed season pack already in
`/mnt/media/video/tv`), and delete the ghost `tv-sonarr` qBit category.

**Rollback:** `git revert` — documentation + one benign `warn` in
`deploy.sh`; no service config or state changed.

### 6.6 Jellyfin → public for trusted users  [DONE — activated + live-verified 2026-05-17; brute-force rule operator-attested]

Make Jellyfin reachable from a trusted friend's own home, their own
per-user account, one clean URL `https://play.nthncrtr.com` (no port).
Stable decisions: **per-user accounts**; **no Authelia on Jellyfin**
(forward_auth breaks native TV/phone clients — consistent with 6.4's
exclusion); hostname **`play`, deliberately not `jellyfin.*`** (`jellyfin.*`
is exactly what automated Jellyfin-CVE scanners enumerate — obscurity as
real attack-surface reduction). Content is the operator's responsibility;
Cloudflare's video-proxying clause is a known caveat (see below).

**Ingress: Cloudflare Tunnel (after router-forward proved impossible).**
The mission first tried **router port-forward + `services/ddns` + a
dedicated Caddy `:8443`**. That is **dead on GFiber**, proven the hard way —
keep this list, it's the whole reason for the architecture:

1. GFiber **reserves inbound WAN 443** for its own management UI (answers
   with a self-signed cert, never forwards — symptom: external cert error /
   HTTP 408, *zero* connections in `journalctl -u caddy`).
2. Moving to WAN 8443 → natto:8443 still failed: GFiber's port-forward/DMZ
   target a **phantom device** (MAC `e4:5f:01:3a:e1:02`, *neither* of
   natto's NICs — `enp1s0 78:55:36:09:3e:b1`, `wlp2s0 50:31:23:b0:3c:92`);
   GFiber never DHCP-learned natto because natto is static-IP, so its
   reserved-IP system can't manage it (resets to `.100`).
3. GFiber's only working "expose" is **DMZ = all ports**, which (no host
   firewall on natto) would put SSH, the Caddy admin API `:2019`, Pi-hole,
   Nextcloud and every *arr on the open internet. Unacceptable.

So ingress is a **Cloudflare Tunnel** (`services/cloudflared`): `cloudflared`
dials *out*, GFiber is irrelevant, and the tunnel ingress maps **exactly
`play.nthncrtr.com → Jellyfin` and nothing else** — strictly better scoping
than the `:8443`/DMZ ideas. Inside clients use Pi-hole split-horizon →
Caddy `:443` → Jellyfin (no Cloudflare round-trip for local 4k). One
`PublishedServerUrl` (no port) serves both. `services/ddns` is **removed**
(a tunnel needs no WAN-IP A record).

**Brute-force (two pivots — keep this history):** through a tunnel,
attackers hit Cloudflare not natto, so host-firewall fail2ban is useless.
*Pivot 2a:* reworked `services/fail2ban` to the `cloudflare-token` ban
action (ban at Cloudflare's edge via API). *Dead end:* the shipped action
calls Cloudflare's **deprecated zone IP-Access-Rules endpoint**
(`zones/<z>/firewall/access_rules/rules`); scoped API tokens get
`10000 Authentication error` there *regardless of permissions* (verified
live: token could read the zone but every firewall/account/lists endpoint
returned 10000; two token iterations incl. correct Firewall-Services:Edit
scope did not help). *Pivot 2b (final):* **`services/fail2ban` retired
entirely**; brute-force protection is a **Cloudflare WAF Rate-Limiting
rule** on the login path — dashboard state (zone `nthncrtr.com` → Security
→ WAF → Rate limiting), not in the repo, like the Pi-hole split-horizon
record. No API token, no container, no deprecated surface. Jellyfin still
logs failed auths itself if forensics are needed.

**Preconditions:**
- Repo clean; 6.2 (Jellyfin standup) DONE.
- Operator has a browser for `cloudflared tunnel login` + Cloudflare zone
  access for `nthncrtr.com`.

**Success criteria (repo side — committed scope):**
- `services/cloudflared/` scaffolded (cloudflare/cloudflared; compose +
  version-controlled `config.yml` ingress + `.gitignore` + README), wired
  into `deploy.sh` (default set, placeholder-safe config install) +
  `bootstrap/natto.sh`.
- `services/ddns/` **removed** (repo + deploy.sh + bootstrap de-wired).
- `services/fail2ban/` **removed** (repo + deploy.sh + bootstrap de-wired)
  — see the two-pivot brute-force note above; protection is now the
  Cloudflare WAF Rate-Limiting rule (operator dashboard step, not in repo).
- `services/caddy/Caddyfile`: `play.nthncrtr.com` back to a plain
  `:443`-implicit inside-only block (no `:8443`, no `import authelia`);
  `caddy adapt` passes.
- `services/jellyfin/docker-compose.yml`: `PublishedServerUrl =
  https://play.nthncrtr.com` (no port); `network_mode: host` retained
  (DNS-rebinding-guard fix); header rewritten.
- Doc drift fixed: `services/jellyfin/README.md`, CLAUDE.md arch table +
  layout + safety rule 8.

**Operator steps (outside the repo — NOT done by this mission):**
1. GFiber cleanup: **turn the `natto` DMZ toggle OFF**, delete the phantom
   `natto` port-forward rule + its reservation (it targets a non-natto
   MAC). No inbound rule is needed at all under a tunnel.
2. Cloudflare DNS: delete any stale `play` / `jellyfin` A records left by
   the decommissioned ddns (the tunnel route in step 4 creates the correct
   proxied CNAME; a leftover grey-cloud A record would shadow it).
3. cloudflared (interactive, services/cloudflared/README.md): `cloudflared
   tunnel login` → `tunnel create play` → put `credentials.json` at
   `/srv/cloudflared/` (0600) → fill the tunnel UUID into
   `/srv/cloudflared/config.yml` → `cloudflared tunnel route dns play
   play.nthncrtr.com` (creates the proxied CNAME).
4. **Cloudflare WAF Rate-Limiting rule** (brute-force layer): Cloudflare
   dashboard → zone `nthncrtr.com` → Security → WAF → Rate limiting rules
   → create: match URI path contains `/Users/AuthenticateByName`, ~5
   req/min per IP, action block/managed-challenge ~10 min. Free plan = one
   rule. (This replaces the retired fail2ban; see the brute-force note.)
5. fail2ban teardown (the retired service): `cd /srv/fail2ban && sudo
   docker compose down`, then `sudo rm -rf /srv/fail2ban`. **Revoke** the
   unused Cloudflare API token that was created for it (dashboard → API
   Tokens → delete) — it's dead weight + attack surface.
6. Pi-hole (v6) split-horizon: local A record `play.nthncrtr.com →
   192.168.1.240` via admin UI **Settings → Local DNS Records** (writes
   `/etc/pihole/hosts/custom.list`, hot-reloads FTL — not a container
   restart, no DNS outage, so safety rule 1's stop/restart gate does not
   apply). Without it, LAN streams pointlessly hairpin through Cloudflare.
   Verify `dig +short play.nthncrtr.com @127.0.0.1` → natto LAN IP.
7. Jellyfin UI: **Known proxies = `127.0.0.1`** (cloudflared connects from
   localhost — correct proxied-client behaviour + real IPs in Jellyfin's
   own log), disable UPnP, create the friend's non-admin per-user account,
   strong passwords on all; set Playback → **Internet streaming bitrate
   limit ~10–15 Mbps** (uplink + limits Cloudflare video exposure).
8. `deploy.sh caddy jellyfin cloudflared` on natto (caddy `adapt`-gated).
9. Verify: inside `https://play.nthncrtr.com` (loads, login); outside
   (cellular, WiFi off) friend streams a title; **QSV engages for a remote
   4k transcode** (services/jellyfin/README.md); `docker logs cloudflared`
   shows 4× `Registered tunnel connection`; **negative test** —
   `pi-hole.nthncrtr.com` / `natto.nthncrtr.com` must FAIL from outside;
   `df -h /`.

**Follow-up (tracked, not in scope):** none outstanding for brute-force —
the Cloudflare WAF Rate-Limiting rule (operator step 4) is the layer.
Optional later: a stricter custom WAF expression (geo/ASN) if abuse shows.

**Cloudflare ToS caveat:** Cloudflare restricts proxying large amounts of
video. Low-volume, few trusted users is pragmatically fine and widely done
but is a documented gray area; if the zone is ever throttled the fallback
is the VPS-relay option. Keep usage modest (the bitrate cap in step 7
helps). Content licensing is the operator's responsibility.

**Outcome:** Pivoted twice. (1) Router-forward → Cloudflare Tunnel after
GFiber proved port-forward impossible (three independent dead ends, above).
(2) fail2ban edge-ban → retired, replaced by a Cloudflare WAF Rate-Limiting
rule, after the shipped action's deprecated Cloudflare endpoint proved
unusable with scoped tokens. Repo end state committed (services/cloudflared
added; services/{ddns,fail2ban} removed; Caddy/jellyfin reverted to clean
no-port URL; deploy.sh/bootstrap/docs consistent). **Activated &
live-verified 2026-05-17:** `https://play.nthncrtr.com` serves Jellyfin
from outside the network via the tunnel (WebFetch); DNS = Cloudflare
proxied; cloudflared healthy (4× edge conns); Pi-hole split-horizon →
natto LAN IP for the inside path; Known-proxies=127.0.0.1; QSV transcode
confirmed; fail2ban container + `/srv/fail2ban` torn down. The Cloudflare
WAF Rate-Limiting rule + the friend's per-user account + bitrate cap are
**operator-attested** (dashboard/UI state not inspectable from here).
`caddy adapt` + `docker compose config` validated against natto throughout.

**Rollback:** revert the Caddyfile `play` block + `deploy.sh caddy`
(adapt-gated; inside path drops, Jellyfin tailnet-only). `cloudflared
tunnel delete play` + remove the Cloudflare DNS route to de-expose
immediately (faster than a repo revert). `docker compose down` in
`/srv/cloudflared` — independent, zero impact on other services. Delete
the Cloudflare Rate-Limiting rule in the dashboard if backing out the
brute-force layer. Jellyfin's own data/accounts untouched. Restoring
`services/ddns` or `services/fail2ban` is **not**
part of rollback (it's obsolete regardless — the router path is dead).

## Phase 7 — Housekeeping

### 7.1 Rename Navidrome `natto.nthncrtr.com` → `music.nthncrtr.com`  [DONE — repo; operator Pi-hole step pending]

`natto.nthncrtr.com` conflated the hub *host* (natto, the Beelink) with
the music *service* — confusing. Operator decision: name it for the
function, **`music`** (Navidrome's own web UI is the actual listening
client, not just a Subsonic backend), consistent with `home.`/`torrent.`/
`play.` and survives a future server swap.

**Preconditions:** `*.nthncrtr.com` is a Cloudflare wildcard → no
per-host public DNS change; old `natto.nthncrtr.com` falls through to
Caddy's `:443 { abort }` once its vhost block is renamed.

**Repo changes (done):** Caddyfile vhost `natto.` → `music.` (+ rationale
comment); navidrome/caddy/jellyfin/pihole READMEs; `deploy.sh`
`verify_url`; `bootstrap/natto.sh` + `runbooks/migrate-natto.md` curl
checks. Historical `[DONE]` mission records left untouched (point-in-time
record; `natto.` failing from outside is still true). `caddy adapt`
validated.

**Success criteria:** `https://music.nthncrtr.com/ping` → 200 after
`deploy.sh caddy navidrome`; `https://natto.nthncrtr.com` → connection
abort; LAN-only devices still reach it (see operator step).

**Operator step (NOT a repo change — runtime Pi-hole state):** the
split-horizon record `192.168.1.50 natto.nthncrtr.com` is in
`pihole.toml`/`custom.list`, runtime-managed, not in the repo. Via Pi-hole
**Settings → Local DNS Records**: add `192.168.1.50 music.nthncrtr.com`,
remove the old `natto.` row. Until this is done, LAN-only music clients
(smart TV, Chromecast) break — the Caddyfile rename alone does not migrate
split-horizon.

**Rollback:** revert this commit + `deploy.sh caddy navidrome`
(adapt-gated); restore the old Pi-hole local record. Navidrome data
untouched throughout.

### 7.2 Encrypt Navidrome passwords at rest (ND_PASSWORDENCRYPTIONKEY)  [DONE — encrypted at rest + live-verified 2026-05-18]

Surfaced while recovering a locked-out login (the `natto.→music.` rename
broke password-manager autofill; the stored credential was correct all
along — rename/deploy exonerated). Recovery revealed Navidrome had **no
encryption key**, so passwords sat in **plaintext** in `navidrome.db` —
and thus in every nightly `/srv` backup tarball.

**Key thing learned (assumption was WRONG):** setting
`ND_PASSWORDENCRYPTIONKEY` does **not** auto-encrypt pre-existing
plaintext. Navidrome's boot routine is *key-rotation only* — it decrypts
each stored value with the **previous** key and re-encrypts with the new
one. Fed raw plaintext it logs `cipher: message authentication failed`,
skips the migration, leaves the value plaintext, and (silently) login
still works via a plaintext-compare fallback while the user-update API
500s. The "already encrypted with current key" sentinel is the `property`
row **`PasswordsEncryptedKey`**; its absence makes Navidrome re-fail the
migration every boot.

**What actually worked (the real method, now in README § Recovery):**
spin a throwaway Navidrome on a scratch DB with the *same*
`--env-file secrets.env`, `POST /auth/createAdmin` the user+password there
(Navidrome stores it correctly key-encrypted and writes
`PasswordsEncryptedKey`), stop it, then with the real container stopped
transplant scratch's `user.password` **and** the `PasswordsEncryptedKey`
property into the real DB via host `python3` stdlib `sqlite3`. Real
Navidrome then sees a healthy keyed DB, skips migration, decrypts fine.
No hand-rolled crypto — Navidrome's own code produced the ciphertext.

**Repo changes:** compose `env_file` block (mirrors homepage pattern,
`required: false`); `secrets.env.example`; `.gitignore`; README
§ Password encryption rewritten to the correct (key-rotation-only) model
+ a two-case recovery procedure (plaintext-write for keyless; scratch
transplant for keyed). Initial commit `6531017` carried the wrong
"auto-encrypts in place" claim; corrected same session.

**Outcome (verified 2026-05-18):** secret installed
`/srv/navidrome/secrets.env` (0600, `nthncrtr`, 64-hex); deployed;
auto-migration failed as above; scratch-transplant applied. `user.password`
now a 72-char key-encrypted blob (was 24-char plaintext `eittrza9e…`);
`PasswordsEncryptedKey` present; **no encrypt/decrypt errors** in fresh
logs; login 200 both locally and end-to-end via
`https://music.nthncrtr.com`; scratch instance + dir fully removed.

**Caveat (documented, accepted):** encrypted DB + key live in the *same*
nightly tarball — defends against a DB-only leak, not loss of the whole
backup. Losing the key locks out all users (recovery = README § Recovery,
keyed case).

**Rollback:** remove `ND_PASSWORDENCRYPTIONKEY` from `secrets.env` +
redeploy; Navidrome can't decrypt → run README § Recovery. Pre-encryption
DB snapshots `/srv/navidrome/_pwreset_bak_2026051800{2552,3613}` are clean
fallbacks (they predate encryption; they also contain old credentials —
prune once satisfied).
