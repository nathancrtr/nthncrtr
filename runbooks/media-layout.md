# /mnt/media layout

Decision record for how the 5TB drive is organized, plus the canonical
storage-model and hardlink notes the service READMEs point back to. Lives at
`/mnt/media` on natto (**ext4** — see § Mount details).

## Target layout

```
/mnt/media/
├── music/        — audio, scanned by Navidrome (bind: /mnt/media/music:/music:ro)
├── seed-only/    — MP3 transcodes of OPS uploads (320/V0). qBit seeds these
                    but Navidrome doesn't see them, so the same album in three
                    formats doesn't triple in the library. See
                    tools/orpheus/upload/README.md for the upload pipeline.
├── video/        — movies/ + tv/, served read-only by Jellyfin (see § Jellyfin)
├── backups/      — natto-YYYY-MM-DD.tgz, written by services/backup
├── immich/       — library/ = Immich's photo/video library (UPLOAD_LOCATION),
                    moved here from the root SSD on 2026-07-21 after the disk
                    filled. Live service state — the one exception to this
                    drive's bulk-media/backup role. See services/immich/README.md.
└── _unsorted/    — junk that landed on the drive over time (installer
                    artifacts, Seagate factory files, *arr installers, etc.)
                    Operator triages or deletes at leisure.
```

Subdirectories of `_unsorted/`:
- `from-mnt-media-root/` — files moved out of `/mnt/media/` itself
- `from-mnt-media-music/` — files moved out of `/mnt/media/music/`'s top level (everything except the actual music tree, which was nested at `/mnt/media/music/music/`)

## Video torrents: never save/seed directly inside `video/tv`

Lesson from 2026-07-20. A batch of TV torrents had qBit save paths pointing
straight at `/mnt/media/video/tv`, so each release-named folder
(`Show.S01.2160p...-GRP/`) sat beside the Sonarr-managed series folder
(`Show Name/Season 1/`) holding hardlinks of the same files. Jellyfin treats
every top-level folder as a series → the same show appeared **twice** (one
copy often with a garbled release-name title). Two rules follow:

- **Grabs go through Sonarr**: qBit saves to `_unsorted/torrents`, Sonarr
  hardlink-imports into `video/tv/<Series>/`. If a torrent must seed
  long-term, relocate it to `seed-only/` with qBit "set location" — same
  filesystem, so it's a rename: hardlinks into the Sonarr folder survive and
  the seed keeps running (same trick as the music seed-only pattern above).
- **Removing a show can only happen via Sonarr** (or a shell on natto).
  Jellyfin mounts `video/` **read-only** — deleting in the Jellyfin UI drops
  the DB item but leaves the files, and the next library scan resurrects the
  show. Unmonitor + delete files in Sonarr (its `/mnt/media` mount is rw),
  then let Jellyfin rescan. Sonarr keeps an empty series folder for shows it
  still tracks; `rmdir` it if you don't want an empty series tile in
  Jellyfin (Sonarr recreates it on the next import).

## Why this layout

- `music/` and `video/` are clean siblings, so Jellyfin and Navidrome get exactly-scoped bind mounts (no scanning irrelevant binaries, no cross-contamination).
- `backups/` co-located on the largest drive and excluded from media scans by being a sibling, not a child of `music/`.
- `_unsorted/` keeps the drive as-it-was preserved (nothing is deleted) without polluting the categories. Easier to commit to the categorization once the operator can see what's actually there.

## Mount details

- Filesystem: **ext4** (`/dev/sda2`, `UUID=3d0d41ab-bb04-418d-a2b4-2afde44a3e50`), mounted `defaults,noatime,nodiratime` per `/etc/fstab`. The drive was reformatted exfat → ext4 on **2026-05-20** (`runbooks/reformat-mnt-media-to-ext4.sh`), *after* the 2026-05-16 host migration — the original Pi-era setup was exfat with `uid=1000,gid=1000` mount options, but ext4 + per-dir chown is now the model.
- The mount root `/mnt/media` itself is owned `root:root`. Each top-level service subdir (`music/`, `seed-only/`, `video/`, `backups/`, `immich/`, `_unsorted/`) is `chown`ed to `nthncrtr:nthncrtr` at creation, so day-to-day operations (rsync from workhorse, qBit writes, etc.) don't need root. **Adding a new top-level subdir requires sudo**: `sudo mkdir -p /mnt/media/<new> && sudo chown nthncrtr:nthncrtr /mnt/media/<new>` (use the clipboard-paste sudo pattern from CLAUDE.md).
- Read-only for the Navidrome and Jellyfin bind mounts: append `:ro`.

## Storage model — `/srv` vs `/mnt/media`

natto has two filesystems, and which one a thing lives on is deliberate:

- **`/srv`** — the Beelink's internal **238 GB ext4 SSD**. Holds the OS plus
  every service's config, state and **databases** (`/srv/<svc>/`). Fast, and
  it's what the nightly backup tarball targets.
- **`/mnt/media`** — the **5TB ext4** USB drive (this file). Bulk media only.
  It's the *portable* tier: it physically moves to a replacement host on a
  cold migration (`runbooks/migrate-natto.md`), mounted UUID-stable.

So **service databases and config live on `/srv`, never on `/mnt/media`** —
that's the service-state tier (SSD, backed up), whereas `/mnt/media` is bulk,
removable media storage. This is the single reason the SQLite/Postgres/MariaDB
stores for Jellyfin, Navidrome, Immich, Nextcloud, Memos, Seerr and the *arrs
sit under `/srv/<svc>/` and not on the big drive. (Both filesystems are ext4
now — the older docs justified this with "exfat can't do POSIX locking", which
was true of the Pi-era exfat drive but no longer applies; the rationale is the
SSD-vs-bulk split, not a filesystem-capability gap.)

One deliberate exception since 2026-07-21: **Immich's photo/video library**
(`/mnt/media/immich/library/` — bulk assets, not a database; its postgres
stays on `/srv`). The library + an in-flight qBit download filled the 238G
root SSD to 0B free, so the operator moved it here. It rides along on a cold
migration like the rest of the drive.

## Hardlinks on import (the *arrs)

Because `/mnt/media` is **ext4**, hardlinks work across the drive, and
Sonarr/Radarr's default *"Use Hardlinks instead of Copy"* is in effect: when an
*arr imports a completed grab from `/mnt/media/_unsorted/torrents/` into
`/mnt/media/video/{movies,tv}/`, the library entry is a **hardlink** to the same
inode, not a second copy. A title that's both imported *and* still seeding
therefore occupies its bytes **once**, not twice. (Verified: imported files in
`_unsorted/torrents/` carry link count ≥ 2.) The old "stored twice because exfat
has no hardlinks" note in the *arr READMEs predates the ext4 reformat and is no
longer true.

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

## Jellyfin

`/mnt/media/video/` is Jellyfin's library, mounted **read-only** (Jellyfin
never writes back into the media tree; its metadata/DB live on the Beelink's
internal disk at `/srv/jellyfin/config`). The mount line in
`services/jellyfin/docker-compose.yml` is:

```yaml
volumes:
  - /mnt/media/video:/media/video:ro
```

Subdirectory layout (settled at first import): `movies/` and `tv/`, mapped
to Jellyfin libraries as `/media/video/movies` (Movies) and
`/media/video/tv` (Shows). See `services/jellyfin/README.md` for the
first-run library setup and the Intel QuickSync HW-transcode notes.
