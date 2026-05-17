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

## Backup

The scan db (`/srv/navidrome/data/`) goes into the daily tarball. The music library itself does NOT — it's its own thing on the 5TB drive, too large to redundantly tar.
