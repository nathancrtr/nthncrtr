# Navidrome

Music streaming server, scans music from the 5TB drive, serves a web UI + Subsonic API.

## Where things live

| | Path |
|---|---|
| Compose | `/srv/navidrome/docker-compose.yml` |
| Scan database | `/srv/navidrome/data/` (sqlite + cover-art cache) |
| Music library | `/mnt/media/music/` (read-only mount in container) |
| Container name | `navidrome-navidrome-1` |
| Image | `deluan/navidrome:latest` |
| Public URL | `https://music.nthncrtr.com` |
| Internal port | 4533 |

The container runs as `1000:1000` (the natto operator's UID/GID). `/mnt/media` is exfat, mounted with `uid=1000`, so file ownership inside the music dir matches.

## Operating

```sh
# Restart (no confirmation needed — DNS is unaffected):
cd /srv/navidrome && docker compose restart

# Trigger a rescan via the web UI: Settings → Music Folders → Quick scan
# Or full rescan: docker exec -it navidrome-navidrome-1 wget -qO- 'http://localhost:4533/api/scanner/scan?fullScan=true'
```

If `navidrome.db-wal` grows unbounded (real example: a disk-full incident blocked SQLite checkpoints, leaving a 1.4 GB WAL), the restart above forces a checkpoint and shrinks the WAL back to ~12K. Inspect with `ls -la /srv/navidrome/data/navidrome.db*`.

## Library layout

The bind mount is `/mnt/media/music:/music:ro`. Navidrome scans `/music` recursively. The library was reorganized in mission 4.3 — see [`runbooks/media-layout.md`](../../runbooks/media-layout.md) for the canonical layout (`/mnt/media/{music,video,backups,_unsorted/}`).

## Auth for the Homepage widget

Homepage's Navidrome widget needs the Subsonic-style auth: `user`, `token`, `salt` (where `token = md5(password + salt)`). Pulled from `/srv/homepage/secrets.env` as `HOMEPAGE_VAR_NAVIDROME_TOKEN` and `HOMEPAGE_VAR_NAVIDROME_SALT`.

## Password encryption

User passwords are encrypted at rest via `ND_PASSWORDENCRYPTIONKEY`, set in
`/srv/navidrome/secrets.env` (mode 0600, not in the repo — see
`secrets.env.example`; wired through compose `env_file`). Generate the key
with `openssl rand -hex 32`.

**Without this key set, Navidrome stores passwords in plaintext** — that
was the state until 2026-05-18 (WORKLIST 7.2). The first Navidrome start
after the key is set encrypts any existing plaintext passwords in place.

**The key is not recoverable.** If `secrets.env` is lost or the key
changes, every stored password becomes undecryptable and all users are
locked out. It is captured in the nightly `/srv` backup tarball, so a
restore brings it back; a from-scratch natto rebuild *without* a backup
restore loses it (same class of runtime-secret caveat as `caddy.env`).
Note the encrypted DB and the key live in the *same* backup tarball — this
protects against a DB-only leak, not against loss of the whole backup.

### Forgotten / locked-out password recovery

No reset CLI exists. With the container stopped, write the desired
password as **plaintext** into the DB; the next start re-encrypts it with
the configured key (or stores it plaintext if no key is set):

```sh
cd /srv/navidrome && docker compose stop
cp -a data/navidrome.db "data/../_pwreset_bak_$(date +%s)/" # snapshot first
# one-off container (no host sqlite3); single-quote-safe passwords only:
docker run --rm -v /srv/navidrome/data:/data --entrypoint sh \
  deluan/navidrome:latest -c \
  "sqlite3 /data/navidrome.db \"UPDATE user SET password='NEWPASS' WHERE user_name='USER';\""
docker compose up -d
# verify: curl -s -o /dev/null -w '%{http_code}' -X POST \
#   http://127.0.0.1:4533/auth/login -H 'Content-Type: application/json' \
#   -d '{"username":"USER","password":"NEWPASS"}'   # expect 200
```

## Backup

The scan db (`/srv/navidrome/data/`) goes into the daily tarball. The music library itself does NOT — it's its own thing on the 5TB drive, too large to redundantly tar. The `_pwreset_bak_*` snapshot dirs from the recovery procedure are not pruned automatically — delete them once login is confirmed (they contain old credentials).
