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
| Photo/video library (`UPLOAD_LOCATION`) | `/mnt/media/immich/library/` (on `/srv/immich/library/` until 2026-07-21 — see below) |
| PostgreSQL datadir | `/srv/immich/db/` |
| Secrets | `/srv/immich/secrets.env` (root:root, 0600, **not** in repo) |
| Containers | `immich_server`, `immich_redis`, `immich_postgres` |
| Images | `ghcr.io/immich-app/immich-server:release`, `valkey/valkey:9`, `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0` |
| Host port | `2283` → container `2283` (published on `0.0.0.0`) |
| Reachability | **Tailnet-only** — `https://photos.nthncrtr.com` (Caddy), `http://natto.tailaf7ea6.ts.net:2283`, `http://natto:2283` |

### Storage split (changed 2026-07-21)

The PostgreSQL datadir is live service state and stays on natto's SSD
(`/srv/immich/db/`). The **library moved to the 5TB drive**
(`/mnt/media/immich/library/`) on 2026-07-21: the capacity caveat that used
to live here came true — the 37G library plus an in-flight qBittorrent
download (which stages on the SSD by design, see
`services/qbittorrent/docker-compose.yml`) filled `/` to 0B free and
crash-looped `immich_postgres`. Earlier revisions of this README said the
library should get "dedicated storage, not /mnt/media"; that was written
when `/mnt/media` was exfat. It has been ext4 since 2026-05-20, and the
operator chose it as the library's home (2026-07-21) over buying a new
drive. If a dedicated/NVMe tier ever materializes (the Beelink has an empty
M.2 slot), the library can move again — it's a stop-Immich + rsync +
compose-edit operation, same as the 2026-07-21 move.

### Why no Authelia (and why it's still safe)

Deliberately **no `import authelia`** on the `photos.nthncrtr.com` vhost —
Immich's native auto-backup app breaks behind `forward_auth` (the shared
reason, tabulated in `services/authelia/README.md`). The barrier instead is
**tailnet-only reach** (the name resolves to natto's non-routable Tailscale IP;
Immich is not on the Cloudflare Tunnel, so safety rule 8 holds) **+ Immich's
own per-user accounts**.

Because Immich has app-level auth, the port is published on `0.0.0.0:2283` (the
Nextcloud model), *not* `127.0.0.1` — this is not the unauthenticated-LAN-door
scenario that makes the *arrs bind to loopback (safety rule 9).

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

`deploy.sh` creates `/srv/immich/db` and `/mnt/media/immich/library` (empty —
the containers chown their own subtrees on first init), installs the compose
file, warns if `secrets.env` is missing, brings the stack up, and probes
`http://127.0.0.1:2283/api/server/ping`. First boot takes a minute or two
while postgres initializes and Immich runs migrations.

**DNS (one-time, Cloudflare dashboard — not in repo).** Add the per-name
grey-cloud A record this network uses for every tailnet-only subdomain (no
wildcard exists — see CLAUDE.md): Type **A**, Name **photos**, IPv4
**100.122.71.33** (natto's Tailscale IP), Proxy status **DNS only**. Until it
exists the name doesn't resolve even though Caddy serves the vhost; and if it
was queried earlier, inside clients may see `NXDOMAIN` for ~30 min (Pi-hole
negative cache — wait it out, don't restart Pi-hole; CLAUDE.md § "New-subdomain
gotcha").

First use: open `https://photos.nthncrtr.com` (on the tailnet) → create the
admin account → Account Settings → API Keys → create one for the Homepage
widget (`HOMEPAGE_VAR_IMMICH_KEY` in `services/homepage/secrets.env`).

## Importing Google Photos

Export via Google Takeout (Photos only; pick a sensible archive size).
Recommended tool: **`immich-go`** (handles Takeout's split archives, JSON
sidecar metadata and album reconstruction far better than the web uploader).
Run it from a machine that can reach `http://natto.tailaf7ea6.ts.net:2283`
with an API key. The library lands in `/mnt/media/immich/library` — watch
`df -h /mnt/media` before importing the full set.

## Machine learning

Disabled on purpose (CPU/RAM cost on the Beelink). The server runs with
`IMMICH_MACHINE_LEARNING_URL=false`, so browsing, albums, mobile auto-backup
and metadata search all work; **smart (semantic) search and face/object
recognition do not**. To enable later: in `docker-compose.yml` uncomment the
`immich-machine-learning` service and the `model-cache` volume, remove the
`IMMICH_MACHINE_LEARNING_URL=false` line, `sudo ./deploy.sh immich`, then in
Immich run the "Smart Search" and "Face Detection" jobs over the library.

## Backups

Half wired. The nightly natto tarball excludes the postgres datadir
(`/srv/immich/db`; the library is on `/mnt/media`, outside the tar's scope
entirely) and instead writes a logical `pg_dumpall` of
`immich_postgres` into `/srv/immich/db-dump.sql.gz` just before the tar
runs — that file IS under `/srv` and so IS captured. The dump uses
Immich's documented `pg_dumpall --clean --if-exists` recipe, which carries
the `CREATE EXTENSION` statements for VectorChord/pgvector so the restore
is valid against the matching `ghcr.io/immich-app/postgres:*-vectorchord*-
pgvectors*` image. See `services/backup/README.md § Restoring Immich`.

What's still not backed up: **`/mnt/media/immich/library`** (the actual
photos and videos). It's not in the nightly tarball for the same size-class
reason Nextcloud's `data/` is excluded — both are slated for the restic +
Backblaze B2 redesign in WORKLIST 8.2. Until that lands the library is the
only copy — and since 2026-07-21 it shares a single spindle with the
nightly tarballs, so a `/mnt/media` drive failure loses both.

## Rollback

`cd /srv/immich && docker compose down` stops the stack (data in
`/srv/immich/db` and `/mnt/media/immich/library` is preserved). To fully
unwind: remove the `photos.nthncrtr.com` block from the Caddyfile + redeploy
caddy, remove the Homepage entry, `docker compose down -v`, and
`rm -rf /srv/immich /mnt/media/immich`. Also
delete the `photos` A record in the Cloudflare dashboard (there is no
wildcard — it is a real per-subdomain record that must be removed by hand).
