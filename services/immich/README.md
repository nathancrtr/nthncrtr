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

Immich's PostgreSQL datadir and its upload library are live service state, so
both live on natto's SSD (`/srv/immich/`), per the repo-wide storage split —
see `runbooks/media-layout.md` § "Storage model".

> **Capacity caveat (read this).** `/srv` (the 238 GB SSD) had ~130 GB free
> when Immich was set up. A full Google Photos archive can exceed that. Run
> `df -h /` on natto during and after the Takeout import. If the library
> outgrows the SSD it needs **dedicated storage**, not `/mnt/media` (that drive
> is the bulk-media tier, not sized or intended for the Immich library) — so
> don't "solve" a full disk by pointing `UPLOAD_LOCATION` there. Tracked in
> WORKLIST 7.1.

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

`deploy.sh` creates `/srv/immich/{library,db}` (root-owned, empty — the
containers chown their own subtrees on first init), installs the compose
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

Half wired. The nightly natto tarball excludes the bulk paths
(`/srv/immich/{library,db}`) and instead writes a logical `pg_dumpall` of
`immich_postgres` into `/srv/immich/db-dump.sql.gz` just before the tar
runs — that file IS under `/srv` and so IS captured. The dump uses
Immich's documented `pg_dumpall --clean --if-exists` recipe, which carries
the `CREATE EXTENSION` statements for VectorChord/pgvector so the restore
is valid against the matching `ghcr.io/immich-app/postgres:*-vectorchord*-
pgvectors*` image. See `services/backup/README.md § Restoring Immich`.

What's still not backed up: **`/srv/immich/library`** (the actual photos
and videos). It's intentionally excluded from the nightly tarball for the
same size-class reason Nextcloud's `data/` is excluded — both are slated
for the restic + Backblaze B2 redesign in WORKLIST 8.2. Until that lands
the library is the only copy.

## Rollback

`cd /srv/immich && docker compose down` stops the stack (data in
`/srv/immich/{library,db}` is preserved). To fully unwind: remove the
`photos.nthncrtr.com` block from the Caddyfile + redeploy caddy, remove the
Homepage entry, `docker compose down -v`, and `rm -rf /srv/immich`. Also
delete the `photos` A record in the Cloudflare dashboard (there is no
wildcard — it is a real per-subdomain record that must be removed by hand).
