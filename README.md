# nthncrtr

Version-controlled config and operational runbooks for the home network at `nthncrtr.com`.

External `*.nthncrtr.com` traffic flows through Cloudflare DNS → the Tailscale IP of `natto` → Caddy on natto → the local service. Internally, everything is small enough that one Pi (natto) hosts the hub services, with a workshop appliance (`starmaya`) running the coffee roasting profiler.

## Hosts

| Host | Hostname | Role | Services |
|---|---|---|---|
| natto | `natto` | Hub | Caddy, Pi-hole, Navidrome, Homepage (qBittorrent stub) |
| starmaya | `kvass` (today) | Workshop | `roaster-daemon` + `roaster-web` |
| workhorse | `workhorse` | Mac client | Tailscale only |

`starmaya` is the canonical service name and intended hostname; the machine is currently named `kvass`.

## Repo layout

```
services/<name>/   per-service config (compose, systemd units, etc.) + a README
bootstrap/         idempotent host setup scripts (run as root on a fresh Pi)
runbooks/          operational docs for non-routine procedures
WORKLIST.md        mission tracker — what's done, what's planned
CLAUDE.md          context for AI-assisted edits
```

## Usage

To bring up a replacement natto from scratch, follow [`runbooks/migrate-natto.md`](runbooks/migrate-natto.md). To understand `/mnt/media`'s layout, see [`runbooks/media-layout.md`](runbooks/media-layout.md). Per-service operational notes (ports, secrets, container names) are in each `services/<name>/README.md`.

## Conventions

- Compose files use relative paths (`./data`, `./config`) and live alongside their data at `/srv/<svc>/` on natto.
- Secrets are never committed: `services/<svc>/secrets.env.example` documents the variables; the populated `secrets.env` lives at `/srv/<svc>/` mode `0600` and is `.gitignore`d.
- The 5TB drive is mounted at `/mnt/media` (exfat, owner uid=1000); music in `/mnt/media/music`, backups in `/mnt/media/backups`.
- Pi-hole stops require explicit confirmation (DNS outage for the household). Caddy reloads require `caddy validate` first.
