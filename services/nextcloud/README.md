# Nextcloud

Self-hosted file sync + share — the destination for a one-time migration off
Google Drive. Four containers from one compose file: Nextcloud (apache),
MariaDB, Redis (transactional file locking), and a cron sidecar.

## Where things live

| | Path |
|---|---|
| Compose | `/srv/nextcloud/docker-compose.yml` |
| App code + `config.php` + apps | `/srv/nextcloud/html/` |
| User files (ex-Drive data) | `/srv/nextcloud/data/` |
| MariaDB datadir | `/srv/nextcloud/db/` |
| Secrets | `/srv/nextcloud/secrets.env` (mode 0600, **not** in repo) |
| Containers | `nextcloud`, `nextcloud-db`, `nextcloud-redis`, `nextcloud-cron` |
| Images | `nextcloud:stable`, `mariadb:lts`, `redis:alpine` |
| Host port | `8081` → container `80` |
| Reachability | **Tailscale-only** — `http://natto.tailaf7ea6.ts.net:8081` |

### Why internal disk, not the 5TB

Nextcloud's data dir and the MariaDB datadir need POSIX ownership, locking
and atomic renames. The 5TB drive is **exfat by design** (it physically
moves between hosts UUID-stable — see `runbooks/migrate-natto.md`) and exfat
gives none of that. Drive content is < 50 GB, which fits the Beelink's
internal ext4 with room to spare. This is the resolved outcome of the
hardware discussion in WORKLIST mission 5.1.

### Why no Caddy / Cloudflare route

Deliberate. This holds a mirror of personal Drive data; it is reachable only
on the tailnet. There is no `nextcloud.nthncrtr.com`, no Caddyfile block, and
nothing to change at the DNS cutover step of a host migration.

## Secrets

`secrets.env.example` lists the variables. On natto, `/srv/nextcloud/secrets.env`
(root:root, 0600) is loaded by both `nextcloud-db` and `nextcloud` — one set
of `MYSQL_*` vars wires up both, plus the initial admin account. The compose
marks the file `required: false` so `docker compose config` still parses on
workhorse where it's intentionally absent.

## Backup policy

Nextcloud splits across the two existing backup mechanisms (see
`services/backup/README.md`):

- **Nightly `natto-*.tgz`** — includes `/srv/nextcloud/html` and a logical
  DB dump at `/srv/nextcloud/db-dump.sql.gz` (written by `backup.sh` just
  before the tar). It **excludes** `/srv/nextcloud/data` and
  `/srv/nextcloud/db`: hot-tarring a live InnoDB datadir produces an
  unrestorable archive, and the user data is too large to duplicate nightly.
- **Weekly `nextcloud-data-sync.timer`** — rsyncs `/srv/nextcloud/data` to
  `/mnt/media/backups/nextcloud-data/` as a single space-efficient mirror
  (one copy, `--delete`, not seven daily duplicates).

Restore = extract the tarball (brings back `html/` + the SQL dump), load the
dump into a fresh MariaDB, then rsync the data mirror back into `data/`.

## Operating

```sh
# occ (Nextcloud's admin CLI) — always as the www-data user:
docker exec -u www-data nextcloud php occ status
docker exec -u www-data nextcloud php occ files:scan --all

# Restart (no DNS impact, no confirmation needed):
cd /srv/nextcloud && docker compose restart

# Tail logs:
docker logs -f nextcloud
docker exec -u www-data nextcloud php occ log:tail        # Nextcloud app log

# Add a trusted domain (e.g. a new tailnet name after a host migration):
docker exec -u www-data nextcloud php occ config:system:set \
  trusted_domains 3 --value=natto-1.tailaf7ea6.ts.net
```

After first start, confirm `docker exec -u www-data nextcloud php occ status`
reports `installed: true` and there are no warnings under
`occ setupchecks` / the web UI's Administration → Overview.

## Migrating off Google Drive

The one-time data migration (rclone, including the Google-native-files export
format decision) is its own runbook: [`runbooks/migrate-off-gdrive.md`](../../runbooks/migrate-off-gdrive.md).

## Activation status

Scaffolding only until the Pi → Beelink cutover. Nothing here deploys to the
current Pi (it can't host this well). It comes up on the Beelink via
`bootstrap/natto.sh` + `deploy.sh nextcloud` as part of
`runbooks/migrate-natto.md`. Tracked in WORKLIST Phase 5.
