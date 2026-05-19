# natto backup

Daily snapshot of natto's stateful bits to a dated tarball on the 5TB drive.

## Files

| File | Deployed to | Purpose |
|---|---|---|
| `backup.sh` | `/usr/local/sbin/natto-backup` | The nightly snapshot script |
| `natto-backup.service` | `/etc/systemd/system/natto-backup.service` | oneshot wrapper |
| `natto-backup.timer` | `/etc/systemd/system/natto-backup.timer` | Daily at 03:30 + 15-min jitter, `Persistent=true` |
| `nextcloud-data-sync.sh` | `/usr/local/sbin/nextcloud-data-sync` | Weekly Nextcloud data mirror |
| `nextcloud-data-sync.service` | `/etc/systemd/system/nextcloud-data-sync.service` | oneshot wrapper |
| `nextcloud-data-sync.timer` | `/etc/systemd/system/nextcloud-data-sync.timer` | Weekly Sun 04:30 + 30-min jitter, `Persistent=true` |

`bootstrap/natto.sh` `step_backup` installs all six and enables both timers.

## What gets backed up

- `/srv/` — every docker service's config + data (Pi-hole, Navidrome, Homepage, eventually qBittorrent)
- `/usr/local/bin/caddy` — the built binary
- `/etc/caddy/Caddyfile` — routing config
- `/etc/caddy/caddy.env` — Cloudflare API token (the tarball stays on the local-only drive, so this is acceptable)
- `/etc/systemd/system/caddy.service` — Caddy systemd unit

NOT in the nightly tarball:
- `/mnt/media/music/` (the music itself — too large; the 5TB drive IS its only storage).
- `/srv/nextcloud/data/` — Nextcloud user files (the ex-Drive data). Too large
  to duplicate every night with no retention. Mirrored **weekly** instead by
  `nextcloud-data-sync.timer` → `/mnt/media/backups/nextcloud-data/` (a single
  `rsync --delete` mirror, not rotating copies).
- `/srv/nextcloud/db/` — the live MariaDB datadir. Hot-tarring an InnoDB
  datadir produces an unrestorable archive. Instead `backup.sh` runs a
  logical `mariadb-dump` into `/srv/nextcloud/db-dump.sql.gz` just before the
  tar — that file *is* under `/srv` and so *is* captured. The dump step is
  skipped silently when the `nextcloud-db` container isn't present (e.g. on
  the current Pi), and a dump failure warns but does not fail the run.
- `/srv/immich/library/` — the photo/video upload library. Same size-class
  reasoning as Nextcloud's data dir; **not yet** covered by any local mirror
  (tracked as WORKLIST 8.2 → restic + Backblaze B2 — that work also adds the
  music library and an offsite copy for both).
- `/srv/immich/db/` — the live Postgres + VectorChord datadir. Same hot-DB-
  datadir reason as Nextcloud. `backup.sh` runs `pg_dumpall --clean --if-
  exists` into `/srv/immich/db-dump.sql.gz` (Immich's documented backup
  recipe, captures the VectorChord/pgvector `CREATE EXTENSION` statements so
  restore into the matching image actually works); skipped/warns on the same
  rules as the Nextcloud dump.

### Restoring Nextcloud

1. Extract the tarball as usual (`tar -xzPf … -C /`) — brings back
   `/srv/nextcloud/html/` and `/srv/nextcloud/db-dump.sql.gz`.
2. Bring up `nextcloud-db` on an empty datadir, then
   `zcat /srv/nextcloud/db-dump.sql.gz | docker exec -i nextcloud-db sh -c 'mariadb -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"'`.
3. `rsync -aH /mnt/media/backups/nextcloud-data/ /srv/nextcloud/data/` to
   refill user files, then `docker compose up -d` the rest.

### Restoring Immich

1. Extract the tarball — brings back `/srv/immich/docker-compose.yml` and
   `/srv/immich/db-dump.sql.gz`. Note: the **library is not in the tarball**
   (see above); restore it from the restic repo (WORKLIST 8.2) into
   `/srv/immich/library/` *before* starting Immich, or accept an empty
   library and lose the photo files (the DB will still have all the metadata,
   pointing at missing files — Immich will surface them as broken).
2. `docker compose -f /srv/immich/docker-compose.yml up -d immich-db` on an
   empty `/srv/immich/db/`, wait for the `immich_postgres` healthcheck to
   pass (it auto-runs the `POSTGRES_*` init from `secrets.env`).
3. `zcat /srv/immich/db-dump.sql.gz | docker exec -i immich_postgres psql -U
   postgres -d postgres` — `pg_dumpall` is restored against `postgres`, not
   the target db; `--clean --if-exists` in the dump drops and recreates
   `immich` cleanly.
4. `docker compose -f /srv/immich/docker-compose.yml up -d` to bring up
   `immich_server` + `immich_redis`.

## Where it goes

`/mnt/media/backups/natto-YYYY-MM-DD.tgz`. Atomic write via `.partial` rename so a partial archive never appears at the dated path.

`tar -P` is used so paths in the archive are absolute — restore is just `tar -xzf … -C /`.

## Operating

```sh
# Trigger a backup now:
sudo systemctl start natto-backup.service

# Watch the log:
journalctl -u natto-backup.service -f

# See when the next run is scheduled:
systemctl list-timers natto-backup.timer

# Trigger the weekly Nextcloud data mirror now (no-op if NC not deployed):
sudo systemctl start nextcloud-data-sync.service
systemctl list-timers nextcloud-data-sync.timer

# List available backups:
ls -lh /mnt/media/backups/
du -sh /mnt/media/backups/nextcloud-data/   # size of the NC data mirror
```

## Failure modes (script exits non-zero)

- Not run as root.
- Any source path missing.
- `/mnt/media/backups/` missing or unwritable.
- Free space less than source-set-size + 10%.

The systemd unit logs to journal with `SyslogIdentifier=natto-backup`; failed runs surface via `systemctl --failed`.

## Retention

None automated — old tarballs accumulate forever. Periodically `ls /mnt/media/backups/ | sort | head -n -7 | xargs -d'\n' rm --` to keep the last 7 (or set up a `find -mtime +N -delete` if you want the timer to do it).
