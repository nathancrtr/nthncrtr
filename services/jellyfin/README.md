# Jellyfin

Local media server for the video library on the 5TB drive. One container
(`lscr.io/linuxserver/jellyfin`), no database sidecar (Jellyfin embeds
SQLite). **Tailscale-only / LAN** — there is no `jellyfin.nthncrtr.com`, no
Caddyfile block, and nothing at the DNS cutover step of a host migration.

## Where things live

| | Path |
|---|---|
| Compose | `/srv/jellyfin/docker-compose.yml` |
| Server config + library DB + metadata | `/srv/jellyfin/config/` |
| Transcode + image cache | `/srv/jellyfin/cache/` |
| Media (read-only) | `/mnt/media/video/` → `/media/video` in-container |
| Container | `jellyfin` |
| Image | `lscr.io/linuxserver/jellyfin:latest` |
| Host port | `8096` → `8096` (+ `7359/udp` LAN discovery) |
| Reachability | **Tailscale-only / LAN** — `http://natto:8096`, `http://natto.tailaf7ea6.ts.net:8096` |

### Why config on internal disk, not the 5TB

Jellyfin's library is SQLite; it needs POSIX locking and atomic renames the
exfat 5TB can't give (the drive is exfat by design so it moves between hosts
UUID-stable — see `runbooks/migrate-natto.md`). Same reasoning as Nextcloud.
Only `config/` and `cache/` live on the Beelink's internal ext4; the media
itself stays on `/mnt/media/video` and is bind-mounted **read-only**.

### Why no Caddy / Cloudflare route

Deliberate, decided with the operator: local streaming only, same posture as
Nextcloud. Reachable on the tailnet and LAN, not the public internet.

## Hardware transcoding (Intel QuickSync)

The Beelink S12 has an Intel iGPU. `/dev/dri` is passed through and the
compose's `group_add` carries natto's host `render` (gid `991`) and `video`
(gid `44`) gids. **These gids are host-specific** — if Jellyfin ever moves
to another host, re-check `getent group render video` there and update the
compose.

After first start: **Dashboard → Playback → Transcoding**, set hardware
acceleration to *Intel QuickSync (QSV)*, enable the codecs the iGPU
supports, save. Verify a forced transcode shows up as `qsv`:

```sh
docker exec jellyfin ls -l /dev/dri                       # renderD128 visible
docker logs jellyfin | grep -i vaapi                      # init on startup
# during a transcoded playback:
docker exec jellyfin sh -c 'cat /proc/*/cmdline 2>/dev/null | tr "\0" " " | grep -o "ffmpeg.*qsv" | head -1'
```

## First-run setup

Browse to `http://natto:8096`, complete the wizard, then add libraries:

| Library | Content type | Folder (in-container) |
|---|---|---|
| Movies | Movies | `/media/video/movies` |
| Shows  | Shows  | `/media/video/tv` |

These map to `/mnt/media/video/{movies,tv}` on natto (already populated).
The bind is read-only, so Jellyfin will not write back into the media tree
(metadata/NFOs land in `/srv/jellyfin/config` instead — fine for this setup).

## Operating

```sh
# Restart (no DNS impact, no confirmation needed — not Pi-hole):
cd /srv/jellyfin && docker compose restart

# Logs:
docker logs -f jellyfin

# Update to a newer image:
cd /srv/jellyfin && docker compose pull && docker compose up -d
```

## Backup policy

`config/` (server settings, users, library DB, watch state) is small and is
covered by the nightly `natto-*.tgz` (it lives under `/srv`, which the
backup tars — unlike `/srv/nextcloud/{data,db}`, there is no exclude here).
`cache/` is disposable and regenerates. The media itself is the 5TB drive
and is not duplicated by this service (it is read-only here).

## Activation status

Stood up alongside the Nextcloud activation after the Pi → Beelink
migration. Comes up via `bootstrap/natto.sh` + `deploy.sh jellyfin`. See
WORKLIST Phase 6.
