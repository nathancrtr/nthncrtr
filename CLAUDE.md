# Project context for Claude

You are working in the version-controlled config + operational runbook for a small home network at `nthncrtr.com`. Read this whole file before acting; it's short, but every section reflects a real lesson learned.

## Architecture

| Host | Hostname | Role | OS / Arch | Services |
|---|---|---|---|---|
| **natto** | `natto` | Hub | Beelink Mini S12, x86_64, Ubuntu Server 26.04 LTS (migrated from Raspberry Pi/arm64/Debian on 2026-05-16 — see `runbooks/migrate-natto.md` Gaps §"2026-05-16") | Caddy (native, systemd), Pi-hole, Navidrome, Homepage, qBittorrent (behind Gluetun + Proton VPN), the *arrs, Nextcloud (Tailscale-only), Jellyfin + Seerr (**the two internet-exposed services** — public for trusted users via a single **Cloudflare Tunnel** (`services/cloudflared`, outbound; GFiber can't port-forward) whose `ingress:` list is the exposure allowlist; brute-force handled by Cloudflare WAF Rate-Limiting rules (dashboard, not in repo); inside clients use Caddy + Pi-hole split-horizon for each. Jellyfin (`play.nthncrtr.com`) is the media server, Seerr (`requests.nthncrtr.com`) is the Jellyfin-SSO request manager that forwards into Sonarr/Radarr — see `services/jellyfin/README.md`, `services/seerr/README.md`, and WORKLIST 6.6/6.7), Authelia (SSO gate for the web-admin tier via Caddy `forward_auth` — fronts the *arrs/qBittorrent/Homepage only; **not** Jellyfin or Seerr, which would break their native clients), Immich (self-hosted Google Photos replacement at `photos.nthncrtr.com` — tailnet-only like Nextcloud, **not** internet-exposed and deliberately **not** behind Authelia: its native mobile backup app breaks behind `forward_auth`, same as Jellyfin/Seerr; library + postgres on `/srv` ext4, not exfat — see `services/immich/README.md`), Memos (lightweight note-taking / quick-capture at `notes.nthncrtr.com` — single container + embedded SQLite on `/srv` ext4; tailnet-only like Nextcloud/Immich, **not** internet-exposed and deliberately **not** behind Authelia, same native-mobile-app reasoning — see `services/memos/README.md`) — all docker-managed compose projects. SMB/Samba is **not a supported feature** here (the old `\\natto\Music` share was dropped at the migration and is intentionally not reproduced). |
| **starmaya** | `kvass` (machine), `starmaya` (canonical) | Workshop appliance | Raspberry Pi, arm64, Debian 13 | `roaster-daemon` + `roaster-web` (Node.js, native systemd). On natto's tailnet as `kvass.tailaf7ea6.ts.net`. |
| **workhorse** | `workhorse` | Client + dev | Intel Mac | Tailscale only — hosts no services. This is where you typically run from. |

External access flow: `<svc>.nthncrtr.com` → Cloudflare DNS (DNS-01 challenge token in `caddy.env`) → Tailscale IP of natto → Caddy on natto → local service.

**There is NO `*.nthncrtr.com` wildcard record.** Each subdomain is its own explicit Cloudflare **A record → natto's Tailscale IP `100.122.71.33`, proxy status "DNS only" (grey cloud)** — the proxy cannot route to a `100.x` Tailscale/CGNAT address, which is also what keeps these tailnet-only. A brand-new subdomain therefore needs this record added in the Cloudflare dashboard (dashboard state, not in repo — same class as the Jellyfin/Seerr WAF rules); Caddy serves the vhost and auto-provisions TLS via DNS-01, but until the A record exists the name does not resolve at all. (The two exceptions are `play.nthncrtr.com` and `requests.nthncrtr.com`, which are Cloudflare-**proxied** CNAMEs to the Cloudflare Tunnel — see safety rule 8.)

**natto's LAN IP is `192.168.1.240`** (primary on `enp1s0`). A `192.168.1.50` secondary alias is also configured on the same NIC — a carryover from the Pi-era natto, which lived at `.50`; kept on the Beelink so anything that cached `.50` (DHCP leases, `known_hosts`, hardcoded LAN refs) continued to work post-migration. Caddy, Pi-hole, and everything else on natto answer on **both** IPs because they bind to the host network. `.240` is canonical for new records; `.50` works equivalently and can be cleaned up later (drop the alias from `enp1s0`, retire any `.50` references).

**New-subdomain gotcha — Pi-hole negative cache.** Inside clients resolve via Pi-hole, which *forwards* `*.nthncrtr.com` upstream (so they get the Tailscale IP) — **except** `music.nthncrtr.com`, `play.nthncrtr.com`, and `requests.nthncrtr.com`, which have Pi-hole local-DNS overrides → natto's LAN IP `192.168.1.240` (`/etc/pihole/hosts/custom.list`, mirrored in `pihole.toml` `hosts[]`; LAN-direct, no Tailscale hop — the two `cloudflared`-tunneled names also need it so inside clients don't hairpin out to Cloudflare). Some grandfathered records (e.g. `music.`, `requests.` as of 2026-05-27) still point at the `.50` alias instead — both work; consolidate to `.240` opportunistically. If a new name was queried *before* its Cloudflare record existed (diagnosing, a browser, etc.), Pi-hole negative-caches the `NXDOMAIN` for the zone's SOA-minimum TTL (**1800s / 30 min**). Symptom: the record is correct at `1.1.1.1`/`8.8.8.8`/Cloudflare's NS but Pi-hole still returns `NXDOMAIN`. Fix: **wait it out** (the SOA TTL counts down — `dig +noall +authority <name> @192.168.1.240`); do **not** restart Pi-hole to force it (safety rule 1 — a household DNS outage to dodge a ≤30-min wait is a bad trade). Clients also negative-cache locally (macOS: `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder`; browsers cache separately). Add a Pi-hole local override only if you specifically want LAN-direct like `music`/`play`/`requests`.

## Repo layout

```
.
├── CLAUDE.md                    # this file
├── README.md                    # human-facing intro
├── WORKLIST.md                  # mission tracker (current + planned + done with [DONE]/[PARTIAL])
├── bootstrap/                   # one-shot host setup scripts (idempotent, run as root)
│   ├── natto.sh
│   └── starmaya.sh
├── runbooks/                    # operational docs for non-routine procedures
│   ├── migrate-natto.md         # cold migration to a replacement host
│   ├── migrate-off-gdrive.md    # one-time Google Drive → Nextcloud data move
│   └── media-layout.md          # /mnt/media organization
└── services/                    # per-service config, one dir each
    ├── caddy/                   # Caddyfile + caddy.service + build.sh
    ├── pihole/                  # docker-compose.yml
    ├── navidrome/               # docker-compose.yml
    ├── homepage/                # docker-compose.yml + config/ + secrets.env.example + .gitignore
    ├── qbittorrent/             # qBit + Gluetun (Proton VPN) sidecar
    ├── sonarr/                  # docker-compose.yml (the *arrs are repo-managed via deploy.sh)
    ├── radarr/                  # docker-compose.yml
    ├── lidarr/                  # docker-compose.yml (music *arr; /mnt/media mounted :ro — search/grab only, qBit writes, NOT import — after 2026-06-16 data-loss; Orpheus via Prowlarr)
    ├── prowlarr/                # docker-compose.yml
    ├── nextcloud/               # NC + MariaDB + Redis + cron (Tailscale-only) + secrets.env.example
    ├── jellyfin/                # docker-compose.yml (host-net; /dev/dri HW transcode; public via cloudflared)
    ├── immich/                  # photo/video backup (Google Photos replacement): server + valkey + vectorchord-pg; tailnet-only, no Authelia
    ├── memos/                   # lightweight note-taking / quick-capture (single container, SQLite); tailnet-only, no Authelia
    ├── cloudflared/             # Cloudflare Tunnel — the public path for Jellyfin (config.yml + gitignored creds)
    ├── authelia/                # SSO IdP: compose + configuration.yml + secrets/users (gitignored) — opt-in deploy
    ├── starmaya/                # systemd units + udev rule (deploys to kvass)
    └── backup/                  # backup.sh + nextcloud-data-sync.sh + their {service,timer}s
```

On natto, deployed config lives at `/srv/<svc>/` with the compose file co-located beside its data (so relative `./data` paths in compose files work). The bootstrap script is what syncs `services/<svc>/docker-compose.yml` into place there.

## Naming conventions you must know

- **starmaya vs kvass**: the docs and repo paths always use `starmaya`. The actual machine you SSH to right now is named `kvass`. Treat `starmaya` as the canonical service name and intended future hostname. ([memory](../../.claude/projects/-Users-nathancarter-repos-nthncrtr/memory/project_starmaya_kvass.md))
- Container names on natto: `pihole`, `navidrome-navidrome-1` (compose v2 default with project=navidrome), `homepage`, `qbittorrent`, `gluetun` (Proton VPN sidecar for `qbittorrent`).
- Service data on natto lives under `/srv/<svc>/`. `/home/nthncrtr/{navidrome,homepage,docker}/` are the **previous** locations and are now empty parents — the move to `/srv/` happened in mission 1.7.
- The 5TB drive is at `/mnt/media` (**ext4**, mounted `rw,noatime,nodiratime` with default options — *not* exfat with `uid=` like the old Pi-era setup; the exfat → ext4 reformat happened on 2026-05-20, *after* the 2026-05-16 host migration — see `runbooks/reformat-mnt-media-to-ext4.sh`). The mount root `/mnt/media` itself is `root:root`; the top-level service subdirs (`music/`, `seed-only/`, `video/`, `backups/`, `_unsorted/`) are each `chown`ed to `nthncrtr:nthncrtr` at creation. Adding a new top-level subdir therefore needs sudo: `sudo mkdir -p /mnt/media/<new> && sudo chown nthncrtr:nthncrtr /mnt/media/<new>`. Music in `/mnt/media/music` (Navidrome), video in `/mnt/media/video` (`movies/` + `tv/`, served read-only by Jellyfin), backups in `/mnt/media/backups`, **seed-only MP3 copies of OPS uploads in `/mnt/media/seed-only/`** (qBit seeds them; Navidrome doesn't see them, so the same album in three formats doesn't triple in your library), junk in `/mnt/media/_unsorted/`. Do NOT call it "/mnt/music" — that path doesn't exist.

## Safety rules

These exist because skipping them once would be expensive. Each has a reason:

1. **Pi-hole stop = household DNS outage.** Always announce + get an explicit y/n confirm (use `AskUserQuestion`) before any operation that stops or restarts the `pihole` container. ~30s of dropped DNS for everyone in the house. Other services don't need this gate.
2. **Caddy reload only after `caddy validate` (or `caddy adapt`) passes.** If validation fails, leave the running config alone. A broken Caddyfile takes down every external URL.
3. **/mnt/media is read-mostly.** No `partition`, `mkfs`, `rm -rf`, or anything destructive against `/mnt/media` or `/dev/sd*`. Backup operations are fine. Reorganization within the fs (mv) is fine.
4. **`docker compose down && up` is fine for non-Pi-hole services**, but verify the public URL after — see § Workflow patterns.
5. **Never `--no-verify` git commits.** Never amend published commits. Never force-push.
6. **Never add `Co-Authored-By: Claude` trailers to commit messages.** Operator preference, applies forever. ([memory](../../.claude/projects/-Users-nathancarter-repos-nthncrtr/memory/feedback_commit_attribution.md))
7. **Always commit before and after a session.** A clean `git status` at session end means a future session can pick up cleanly.
8. **Jellyfin and Seerr are the internet-exposed services; the cloudflared `ingress:` list is the allowlist.** The public path is a single Cloudflare Tunnel (`services/cloudflared`), whose `ingress:` in `config.yml` maps exactly `play.nthncrtr.com → Jellyfin` and `requests.nthncrtr.com → Seerr` — everything else 404s. Adding a third hostname is an **explicit operator decision** that must update this rule in the same change; don't quietly extend the ingress list. GFiber router port-forwarding and DMZ are proven dead ends (don't retry; full reasoning in `services/jellyfin/README.md`). Never put Jellyfin or Seerr behind `import authelia` (breaks Jellyfin's native TV/phone clients and Seerr's native mobile app — same reasoning as Immich). The barrier on each is per-user accounts (Jellyfin's own; Seerr's Jellyfin-SSO inherits them) + a Cloudflare WAF Rate-Limiting rule on the respective login path (dashboard state, not in repo — fail2ban was tried and retired: Cloudflare deprecated the zone IP-Access-Rules API its action used). Don't weaken any of these without saying so explicitly. See WORKLIST 6.6 (Jellyfin-public) and 6.7 (Seerr).

9. **The *arrs and qBittorrent auth model is two coupled halves — never change one without the other.** Each *arr's `config.xml` carries `<AuthenticationMethod>External</AuthenticationMethod>` (set 2026-05-18; runtime state on natto, *not* in the repo): the app renders no login page and trusts the Authelia-fronted proxy, while its API key still guards `/api`. qBittorrent has no `External` equivalent, so it instead whitelists the docker bridge in `qBittorrent.conf` (`WebUI\AuthSubnetWhitelist`, also runtime state). Because `External`/whitelist mean "no app-level login," the compose port publishes are deliberately bound to `127.0.0.1` (not `0.0.0.0`) so the *only* path is `*.nthncrtr.com → Caddy → Authelia` — a `0.0.0.0` publish would be an unauthenticated LAN-direct open door. Don't "fix" a port back to `0.0.0.0`, and don't revert `External`/whitelist, without restoring the matching half. Reverting both (back to in-app `Forms` login + `0.0.0.0`) is the clean way to unwind Authelia if ever wanted.

   **Consequence — inter-service docker traffic does NOT use `host.docker.internal`.** Because the host ports are 127.0.0.1-only, a container reaching `host.docker.internal:<port>` (which resolves to the docker bridge gateway, not loopback) gets *connection refused*. This bit Prowlarr↔*arrs and *arrs→qBit on 2026-05-19, after the 2026-05-18 rebinding. The fix is shared docker networks, declared `external: true` in the compose files: **arrnet** (172.29.0.0/16, created idempotently by `deploy.sh ensure_arrnet`) carries Prowlarr↔Sonarr↔Radarr; **qbittorrent_default** (172.23.0.0/16, created by the qbittorrent stack and already in qBit's `AuthSubnetWhitelist`) carries Sonarr/Radarr→`gluetun:8080`. Inter-container traffic stays private (no LAN exposure, host 127.0.0.1 bindings unchanged — safety rule 9 holds). When adding another service that needs to talk to an *arr or qBit, put it on the matching net by container name; don't reach for `host.docker.internal`. Same pattern as Homepage's widgets (`services/homepage/docker-compose.yml`).

## Workflow patterns (the things that took a while to figure out)

### Deploying repo changes to natto

For routine config updates use `deploy.sh` at the repo root, not ad-hoc scp/rsync. The repo is checked out at `/srv/nthncrtr-repo` on natto; deploy from there:

```sh
ssh -t natto
cd /srv/nthncrtr-repo
git pull
sudo ./deploy.sh                          # default: caddy + navidrome + homepage + backup
sudo ./deploy.sh --dry-run                # preview diffs and intended actions
sudo ./deploy.sh navidrome homepage       # specific services
sudo ./deploy.sh --yes-pihole pihole      # required for pihole (DNS outage gate)
sudo ./deploy.sh starmaya                 # opt-in; deploys to kvass via ssh -t
```

The script honors the safety rules: Caddy gets `caddy adapt` validation before any file touches `/etc/caddy/`; Pi-hole requires `--yes-pihole`; starmaya is opt-in. It also warns (does not fail) if the working tree is dirty. Per-service logic and reload conditions live inside `deploy.sh` itself.

Repo lives at `/srv/nthncrtr-repo`, cloned via a per-host GitHub Deploy key (passphrase-less ed25519). GitHub disallows the same key being both a personal SSH key and a deploy key, so the host gets its own. `bootstrap/natto.sh` (`step_deploy_key`) generates the key and writes the `Host github.com` SSH config entry; the operator still has to add the pubkey to GitHub once and clone the repo once. Passphrase-less is deliberate — non-interactive automation can't prompt, and a server-resident read-only single-repo key is the least valuable thing on a compromised host.

### Sudo on natto from workhorse

For one-off changes outside `deploy.sh`'s scope: `ssh natto sudo …` will fail because sudo wants a TTY. The pattern that works:

1. Compose the full command (`set -e` + the sudo'd ops chained with `&&`).
2. Copy it to clipboard via `printf … | pbcopy`.
3. Tell the operator to paste it in their terminal — sudo prompts there work.
4. After they say "done", verify the resulting state by SSHing in non-sudo for read-only checks.

For non-sudo work, `nthncrtr` is in the `docker` group, so `docker compose …` over plain SSH is fine.

### Validating the Caddyfile

`caddy validate` actually tries to *provision* TLS, which fails without `CF_API_TOKEN` set. For syntax-only checks, use:

```sh
cat services/caddy/Caddyfile | ssh natto 'caddy adapt --adapter caddyfile --config /dev/stdin'
```

A successful adapt returns the JSON config. A failed one prints an error to stderr.

### Capturing existing config

When asked to "capture" what's running on natto for a service, check whether it's already managed by docker compose first:

```sh
ssh natto 'docker inspect <container> --format "{{index .Config.Labels \"com.docker.compose.project.config_files\"}}"'
```

If a path comes back, that file IS the source of truth — fetch it verbatim. The cutover from "running container" to "container managed by your shiny new compose file" is then a **no-op**, because the running container is already that compose file. Don't restart things to "test the cutover" — verify config equivalence and move on.

### Service "cutover" pattern (when something IS needed)

For Navidrome / Homepage / qBittorrent (anything not Pi-hole):
1. Snapshot pre-state: `docker inspect <container> > /tmp/<svc>.pre.json`.
2. `cd /srv/<svc> && docker compose down`.
3. Make the change (path move, compose edit, etc.).
4. `docker compose up -d` from the new location.
5. Verify: `curl -fsSL -o /dev/null -w '%{http_code}\n' https://<url>`.
6. Diff `docker inspect` if you want full assurance.

For Pi-hole, add the AskUserQuestion gate before step 2.

### Secrets

Pattern in this repo:

- Repo: commit `services/<svc>/secrets.env.example` with variable names + empty values, and `services/<svc>/.gitignore` excluding `secrets.env`.
- On natto: `/srv/<svc>/secrets.env` mode 0600, populated with real values.
- Compose: `env_file: [{ path: secrets.env, required: false }]` so `docker compose config` works on workhorse where the file is intentionally absent.
- For Homepage: services.yaml references secrets as `{{HOMEPAGE_VAR_*}}` substitutions; the env file populates those vars.

Caddy's secret (`CF_API_TOKEN`) lives at `/etc/caddy/caddy.env` (mode 0600, owner `caddy:caddy`), which the systemd unit `EnvironmentFile=`s. Operator installs it manually after bootstrap; it's deliberately NOT in the repo and NOT auto-restored.

### Bash scripts

- Idempotent. Re-running a script with no state change should be a no-op.
- `set -euo pipefail` at the top of every script.
- For "is the binary installed at the right version" checks, prefer parsing the binary's `--version` over comparing `mtime` (mtimes don't survive `git clone` or `tar`-based copies).

### Reaching kvass

kvass is on natto's tailnet as `kvass.tailaf7ea6.ts.net` (IP `100.65.46.92`). From workhorse, `ssh kvass` works over the LAN. **From natto, tailnet names resolve** — `curl http://kvass.tailaf7ea6.ts.net:8080` is the roaster-web endpoint behind `starmaya.nthncrtr.com`. The mechanism is non-obvious: natto runs Pi-hole on `:53`, so systemd-resolved's stub listener is disabled — but resolved itself still has the Tailscale split-DNS route (`tailscale0` → `100.100.100.100` for `tailaf7ea6.ts.net`), and `/etc/nsswitch.conf` carries `resolve [!UNAVAIL=return]` so glibc lookups go through resolved (fixed 2026-05-16; runbook Gaps item 6). So: `getent`/`curl`/`apt` resolve tailnet names, but Go's *pure* resolver (Caddy) reads `/etc/resolv.conf` directly and bypasses this — which is why the Caddyfile still pins kvass's IP defensively (don't "fix" it back to the name). `tailscale ping kvass` works regardless (no DNS).

### When debugging weird state, check disk space first

natto's root fs (since 2026-05-16: a 238G ext4 SSD on the Beelink, no longer a 15G SD card — disk-full is far less likely but the *failure mode* below is the same) has hit 100% before. A full disk causes *silent* failures, not loud ones: **pihole-FTL** writes to `pihole.toml` truncate to zero (so it boots from "default config" and wipes upstream DNS), and **Navidrome** SQLite checkpoints can't drain the WAL (multi-GB `navidrome.db-wal` builds up). Both look like a healthy running service that's just behaving wrong. Run `ssh natto 'df -h /'` early in any session that involves degraded state — it'll save you hours of guessing.

## Where to look for what

- **What was decided** — `WORKLIST.md`. Each mission has Preconditions / Success criteria / Rollback / Outcome. `[DONE]` and `[PARTIAL]` markers are kept up to date.
- **How to migrate natto** — `runbooks/migrate-natto.md`. Cold-start steps, in order, including the Cloudflare DNS cutover.
- **/mnt/media layout** — `runbooks/media-layout.md`. What's where and why; rollback for the reorganization.
- **Per-service operational notes** — `services/<svc>/README.md`. Ports, secrets, container names, where data lives.
- **Project memory** — `~/.claude/projects/-Users-nathancarter-repos-nthncrtr/memory/`. The kvass/starmaya distinction and the "no Co-Authored-By" rule live here. Update when you learn something durable.

## Things NOT to do

- Don't try `sudo` over SSH non-interactively to natto. Use the clipboard pattern.
- Don't `caddy validate` for syntax checks — use `caddy adapt`.
- Don't commit `secrets.env` (it's gitignored, but always double-check `git status` before committing in `services/homepage/`).
- Don't restart Pi-hole without operator confirmation.
- Don't reload Caddy without first validating the new config.
- Don't add `Co-Authored-By: Claude` to commits.
- Don't blindly trust this file — if you find drift between what's documented here and reality, fix this file as part of your work and call it out in the commit message.

## When in doubt

Default to capture-then-confirm: read state, propose the change, ask the operator before doing anything that reaches outside the repo. This codebase is small enough that over-asking costs less than under-asking.
