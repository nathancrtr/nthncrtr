# Immich

Self-hosted photo/video backup — the destination for the Google Photos
export. Three containers from one compose file: the merged Immich server
(API + web + microservices), a Valkey (Redis) for the job queue, and a
PostgreSQL with the VectorChord vector extension (Immich pins this exact
image; a plain `postgres` will not work). The machine-learning container
(face/object recognition, CLIP smart search) is deliberately **not**
deployed — see § Machine learning.

## Where things live

| | Path |
|---|---|
| Compose | `/srv/immich/docker-compose.yml` |
| Photo/video library (`UPLOAD_LOCATION`) | `/srv/immich/library/` |
| PostgreSQL datadir | `/srv/immich/db/` |
| Secrets | `/srv/immich/secrets.env` (root:root, 0600, **not** in repo) |
| Containers | `immich_server`, `immich_redis`, `immich_postgres` |
| Images | `ghcr.io/immich-app/immich-server:release`, `valkey/valkey:9`, `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0` |
| Host port | `2283` → container `2283` (published on `0.0.0.0`) |
| Reachability | **Tailnet-only** — `https://photos.nthncrtr.com` (Caddy), `http://natto.tailaf7ea6.ts.net:2283`, `http://natto:2283` |

### Why internal disk, not the 5TB

Immich's PostgreSQL datadir **and** its upload library need POSIX
ownership, locking and atomic renames. The 5TB drive is exfat by design
(it moves between hosts UUID-stable — see `runbooks/migrate-natto.md`) and
gives none of that; Immich officially requires a POSIX filesystem for
`UPLOAD_LOCATION`. Same resolved reasoning as Nextcloud and Jellyfin.

> **Capacity caveat (read this).** `/srv` (the Beelink's 238 GB ext4 SSD)
> had ~130 GB free when Immich was set up. A full Google Photos archive can
> exceed that. Run `df -h /` on natto during and after the Takeout import.
> If the library outgrows the SSD it must move to **dedicated POSIX
> storage** — exfat (`/mnt/media`) is not a safe target for the Immich
> library (corruption / permission breakage), so do not "solve" a full
> disk by pointing `UPLOAD_LOCATION` there. Tracked in WORKLIST 7.1.

### Why no Authelia (and why it's still safe)

There is deliberately **no `import authelia`** on the `photos.nthncrtr.com`
Caddy vhost. Immich's native mobile app — the primary auto-backup
mechanism — breaks behind `forward_auth` for exactly the reason Jellyfin's
native clients do (WORKLIST 6.4/6.6). The barrier is instead:

1. **Tailnet-only.** `photos.nthncrtr.com` resolves (via Cloudflare DNS) to
   natto's *Tailscale* IP, which is not internet-routable. Only devices on
   the tailnet reach it. Jellyfin remains the one internet-exposed service
   (safety rule 8); Immich is not on the Cloudflare Tunnel.
2. **Immich's own per-user accounts.** Set up the admin account on first
   load, then add per-user accounts.

The port is published on `0.0.0.0:2283` (the Nextcloud model), *not*
`127.0.0.1` — that is correct here because Immich has app-level auth, so
this is not the unauthenticated-LAN-door scenario that makes the *arrs bind
to loopback (safety rule 9).

## Secrets

`secrets.env.example` lists the variables. On natto,
`/srv/immich/secrets.env` (root:root, 0600) is loaded by both
`immich-server` and `immich-db`. `DB_PASSWORD` (server's connection secret)
and `POSTGRES_PASSWORD` (postgres superuser init) **must be the same
value**; generate one and put it in both. `POSTGRES_PASSWORD` is read only
on the very first DB init.

## Deploy

```sh
ssh -t natto
cd /srv/nthncrtr-repo && git pull
sudo ./deploy.sh immich        # also in the default deploy set
sudo ./deploy.sh caddy         # picks up the photos.nthncrtr.com vhost
```

`deploy.sh` creates `/srv/immich/{library,db}` (root-owned, empty — the
containers chown their own subtrees on first init), installs the compose
file, warns if `secrets.env` is missing, brings the stack up, and probes
`http://127.0.0.1:2283/api/server/ping`. First boot takes a minute or two
while postgres initializes and Immich runs migrations.

**DNS (one-time, Cloudflare dashboard — not in repo).** There is no
`*.nthncrtr.com` wildcard; each subdomain is an explicit record. Add:
Type **A**, Name **photos**, IPv4 **100.122.71.33** (natto's Tailscale IP,
same as `music`/`radarr`), Proxy status **DNS only** (grey cloud — the
proxy can't reach a `100.x` address, and this is what keeps it
tailnet-only), TTL Auto. Until this exists `photos.nthncrtr.com` does not
resolve even though Caddy is already serving the vhost.

First use: open `https://photos.nthncrtr.com` (on the tailnet) → create the
admin account → Account Settings → API Keys → create one for the Homepage
widget (`HOMEPAGE_VAR_IMMICH_KEY` in `services/homepage/secrets.env`).

## Importing Google Photos

Export via Google Takeout (Photos only; pick a sensible archive size).
Recommended tool: **`immich-go`** (handles Takeout's split archives, JSON
sidecar metadata and album reconstruction far better than the web uploader).
Run it from a machine that can reach `http://natto.tailaf7ea6.ts.net:2283`
with an API key. The library lands in `/srv/immich/library` — watch
`df -h /` (see the capacity caveat above) before importing the full set.

## Machine learning

Disabled on purpose (CPU/RAM cost on the Beelink). The server runs with
`IMMICH_MACHINE_LEARNING_URL=false`, so browsing, albums, mobile auto-backup
and metadata search all work; **smart (semantic) search and face/object
recognition do not**. To enable later: in `docker-compose.yml` uncomment the
`immich-machine-learning` service and the `model-cache` volume, remove the
`IMMICH_MACHINE_LEARNING_URL=false` line, `sudo ./deploy.sh immich`, then in
Immich run the "Smart Search" and "Face Detection" jobs over the library.

## Backups

Not yet wired into `services/backup`. The nightly natto tarball excludes
large/DB data dirs by policy (see `services/backup/README.md`); like
Nextcloud, Immich needs a logical `pg_dump` of `immich_postgres` plus a
copy of `/srv/immich/library`. Adding that to `backup.sh` is follow-up
work noted in WORKLIST 7.1 — until then the library is **not** backed up.

## Rollback

`cd /srv/immich && docker compose down` stops the stack (data in
`/srv/immich/{library,db}` is preserved). To fully unwind: remove the
`photos.nthncrtr.com` block from the Caddyfile + redeploy caddy, remove the
Homepage entry, `docker compose down -v`, and `rm -rf /srv/immich`. Also
delete the `photos` A record in the Cloudflare dashboard (there is no
wildcard — it is a real per-subdomain record that must be removed by hand).
