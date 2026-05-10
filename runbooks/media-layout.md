# /mnt/media layout

Decision record for how the 5TB drive is organized. Lives at `/mnt/media` on natto (exfat, owner uid=1000).

## Target layout

```
/mnt/media/
├── music/        — audio, scanned by Navidrome (bind: /mnt/media/music:/music:ro)
├── video/        — placeholder for future Jellyfin / video content
├── backups/      — natto-YYYY-MM-DD.tgz, written by services/backup
└── _unsorted/    — junk that landed on the drive over time (installer
                    artifacts, Seagate factory files, *arr installers, etc.)
                    Operator triages or deletes at leisure.
```

Subdirectories of `_unsorted/`:
- `from-mnt-media-root/` — files moved out of `/mnt/media/` itself
- `from-mnt-media-music/` — files moved out of `/mnt/media/music/`'s top level (everything except the actual music tree, which was nested at `/mnt/media/music/music/`)

## Why this layout

- `music/` and `video/` are clean siblings, so Jellyfin and Navidrome get exactly-scoped bind mounts (no scanning irrelevant binaries, no cross-contamination).
- `backups/` co-located on the largest drive and excluded from media scans by being a sibling, not a child of `music/`.
- `_unsorted/` keeps the drive as-it-was preserved (nothing is deleted) without polluting the categories. Easier to commit to the categorization once the operator can see what's actually there.

## Mount details

- Filesystem: exfat (`/dev/sda2`), mounted with `uid=1000,gid=1000,fmask=0022,dmask=0022,iocharset=utf8,errors=remount-ro` per `/etc/fstab`.
- Owner of every file/dir: `nthncrtr:nthncrtr`.
- Read-only for Navidrome (and any future Jellyfin) bind mounts: append `:ro`.

## Migration from previous state (2026-05-09)

The drive arrived with installer junk at `/mnt/media/` root and a deeper junk pile at `/mnt/media/music/` (with the actual music tree nested at `/mnt/media/music/music/`). Migration:

1. Stop Navidrome: `cd /srv/navidrome && docker compose down`.
2. Create target dirs: `mkdir -p /mnt/media/{video,_unsorted/from-mnt-media-root,_unsorted/from-mnt-media-music}`.
3. Move root-level junk to `_unsorted/from-mnt-media-root/` (everything in `/mnt/media/` except `music/`, `video/`, `backups/`, `_unsorted/`, and `System Volume Information/` which stays where Windows put it).
4. Collapse the music nesting: rename `/mnt/media/music` → `/mnt/media/music_old`, then move the actual music up: rename `/mnt/media/music_old/music` → `/mnt/media/music`, move the rest of `/mnt/media/music_old/*` to `/mnt/media/_unsorted/from-mnt-media-music/`, `rmdir /mnt/media/music_old`.
5. Start Navidrome: `cd /srv/navidrome && docker compose up -d`. Navidrome auto-rescans on startup; library will rebuild over the next 30 min – few hours (257 GB across 778 albums).

Bind mount in `services/navidrome/docker-compose.yml` does not change — it stays `/mnt/media/music:/music:ro`. The path inside the container is unchanged; only what's at the path changes.

## Rollback

The reorganization is reversible because every move stays on the same filesystem (no copy, no data loss). To revert:

```sh
cd /srv/navidrome && docker compose down
mv /mnt/media/music /mnt/media/music_new
mkdir /mnt/media/music
mv /mnt/media/_unsorted/from-mnt-media-music/* /mnt/media/music/
mv /mnt/media/music_new /mnt/media/music/music
mv /mnt/media/_unsorted/from-mnt-media-root/* /mnt/media/
rmdir /mnt/media/_unsorted/{from-mnt-media-root,from-mnt-media-music,}
rmdir /mnt/media/video
cd /srv/navidrome && docker compose up -d
```

## Future: Jellyfin

`/mnt/media/video/` is the eventual mount target. When Jellyfin lands:

```yaml
# services/jellyfin/docker-compose.yml (sketch)
services:
  jellyfin:
    volumes:
      - /mnt/media/video:/media/video:ro
```

Subdirectories of `video/` (`movies/`, `tv/`, etc.) follow Jellyfin's preferred layout — defer that decision until first import.
