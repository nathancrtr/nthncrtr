# Project context for Claude

You are working in the version-controlled config + operational runbook for a small home network at `nthncrtr.com`. Read this whole file before acting; it's short, but every section reflects a real lesson learned.

## Architecture

| Host | Hostname | Role | OS / Arch | Services |
|---|---|---|---|---|
| **natto** | `natto` | Hub | Beelink Mini S12, x86_64, Ubuntu Server 26.04 LTS (migrated from Raspberry Pi/arm64/Debian on 2026-05-16 — see `runbooks/migrate-natto.md` Gaps §"2026-05-16") | Caddy (native, systemd), Pi-hole, Navidrome, Homepage, qBittorrent (behind Gluetun + Proton VPN), the *arrs, Nextcloud (Tailscale-only), Jellyfin (**the one internet-exposed service** — public for trusted users via a **Cloudflare Tunnel** (`services/cloudflared`, outbound; GFiber can't port-forward); `fail2ban` bans abuse at Cloudflare's edge; inside clients use Caddy + Pi-hole split-horizon — see `services/jellyfin/README.md` and WORKLIST 6.6), Authelia (SSO gate for the web-admin tier via Caddy `forward_auth` — fronts the *arrs/qBittorrent/Homepage only; **not** Jellyfin, which would break its native clients) — all docker-managed compose projects. SMB/Samba is **not a supported feature** here (the old `\\natto\Music` share was dropped at the migration and is intentionally not reproduced). |
| **starmaya** | `kvass` (machine), `starmaya` (canonical) | Workshop appliance | Raspberry Pi, arm64, Debian 13 | `roaster-daemon` + `roaster-web` (Node.js, native systemd). On natto's tailnet as `kvass.tailaf7ea6.ts.net`. |
| **workhorse** | `workhorse` | Client + dev | Intel Mac | Tailscale only — hosts no services. This is where you typically run from. |

External access flow: `*.nthncrtr.com` → Cloudflare DNS (DNS-01 challenge token in `caddy.env`) → Tailscale IP of natto → Caddy on natto → local service.

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
    ├── nextcloud/               # NC + MariaDB + Redis + cron (Tailscale-only) + secrets.env.example
    ├── jellyfin/                # docker-compose.yml (host-net; /dev/dri HW transcode; public via cloudflared)
    ├── cloudflared/             # Cloudflare Tunnel — the public path for Jellyfin (config.yml + gitignored creds)
    ├── fail2ban/                # Jellyfin brute-force jail; bans at Cloudflare edge (gitignored token)
    ├── authelia/                # SSO IdP: compose + configuration.yml + secrets/users (gitignored) — opt-in deploy
    ├── starmaya/                # systemd units + udev rule (deploys to kvass)
    └── backup/                  # backup.sh + nextcloud-data-sync.sh + their {service,timer}s
```

On natto, deployed config lives at `/srv/<svc>/` with the compose file co-located beside its data (so relative `./data` paths in compose files work). The bootstrap script is what syncs `services/<svc>/docker-compose.yml` into place there.

## Naming conventions you must know

- **starmaya vs kvass**: the docs and repo paths always use `starmaya`. The actual machine you SSH to right now is named `kvass`. Treat `starmaya` as the canonical service name and intended future hostname. ([memory](../../.claude/projects/-Users-nathancarter-repos-nthncrtr/memory/project_starmaya_kvass.md))
- Container names on natto: `pihole`, `navidrome-navidrome-1` (compose v2 default with project=navidrome), `homepage`, `qbittorrent`, `gluetun` (Proton VPN sidecar for `qbittorrent`).
- Service data on natto lives under `/srv/<svc>/`. `/home/nthncrtr/{navidrome,homepage,docker}/` are the **previous** locations and are now empty parents — the move to `/srv/` happened in mission 1.7.
- The 5TB drive is at `/mnt/media` (exfat, uid=1000:gid=1000). Music in `/mnt/media/music` (Navidrome), video in `/mnt/media/video` (`movies/` + `tv/`, served read-only by Jellyfin), backups in `/mnt/media/backups`, junk in `/mnt/media/_unsorted/`. Do NOT call it "/mnt/music" — that path doesn't exist.

## Safety rules

These exist because skipping them once would be expensive. Each has a reason:

1. **Pi-hole stop = household DNS outage.** Always announce + get an explicit y/n confirm (use `AskUserQuestion`) before any operation that stops or restarts the `pihole` container. ~30s of dropped DNS for everyone in the house. Other services don't need this gate.
2. **Caddy reload only after `caddy validate` (or `caddy adapt`) passes.** If validation fails, leave the running config alone. A broken Caddyfile takes down every external URL.
3. **/mnt/media is read-mostly.** No `partition`, `mkfs`, `rm -rf`, or anything destructive against `/mnt/media` or `/dev/sd*`. Backup operations are fine. Reorganization within the fs (mv) is fine.
4. **`docker compose down && up` is fine for non-Pi-hole services**, but verify the public URL after — see § Workflow patterns.
5. **Never `--no-verify` git commits.** Never amend published commits. Never force-push.
6. **Never add `Co-Authored-By: Claude` trailers to commit messages.** Operator preference, applies forever. ([memory](../../.claude/projects/-Users-nathancarter-repos-nthncrtr/memory/feedback_commit_attribution.md))
7. **Always commit before and after a session.** A clean `git status` at session end means a future session can pick up cleanly.
8. **Jellyfin is the only internet-exposed service; keep it that way.** The public path is the Cloudflare Tunnel (`services/cloudflared`), whose ingress maps exactly `play.nthncrtr.com → Jellyfin` and nothing else — never add other hostnames/services to that tunnel config. GFiber router port-forwarding and DMZ are proven dead ends (don't retry; full reasoning in `services/jellyfin/README.md`). Never put Jellyfin behind `import authelia` (breaks its native clients). The barrier is Jellyfin's per-user accounts + `services/fail2ban` (Cloudflare-edge bans); don't weaken either without saying so explicitly. See WORKLIST 6.6.

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
