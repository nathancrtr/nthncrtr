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

**QSV is mandatory for the 4k library, not optional polish.** With hardware
acceleration off, a 4k HDR remux (HEVC 10-bit + HDR→SDR tone-map + encode,
all in software) pegs the N95's 4 cores at ~350% CPU and buffers within
seconds — it *looks* like a network problem but is 100% CPU. The LG webOS
client (and most TV-native apps) can't decode TrueHD/Atmos or take HDR HEVC
untouched, so they force this transcode; QSV is what makes it real-time.

### The config is NOT in this repo — it must be re-applied on a rebuild

QSV lives in `/srv/jellyfin/config/encoding.xml` (runtime config, not
version-controlled). The nightly `natto-*.tgz` backs it up, so a *restore*
brings it back — but a **from-scratch natto rebuild without a backup restore
silently reverts to software transcoding** (the buffering returns). After
any such rebuild, re-apply via **Dashboard → Playback → Transcoding** (set
HW accel = *Intel QuickSync (QSV)*, QSV device `/dev/dri/renderD128`, enable
VPP tone-mapping, add `hevc`/`vp9`/`av1` to HW decode codecs, save), or edit
`encoding.xml` directly. The keys that matter (verified working 2026-05-16):

```xml
<HardwareAccelerationType>qsv</HardwareAccelerationType>
<QsvDevice>/dev/dri/renderD128</QsvDevice>
<EnableVppTonemapping>true</EnableVppTonemapping>          <!-- HDR→SDR on the iGPU -->
<EnableIntelLowPowerH264HwEncoder>true</EnableIntelLowPowerH264HwEncoder>
<EnableIntelLowPowerHevcHwEncoder>true</EnableIntelLowPowerHevcHwEncoder>
<HardwareDecodingCodecs> h264, hevc, vc1, vp9, av1 </HardwareDecodingCodecs>
```

Restart the container after editing. Verify the pipeline is hardware (not
`libx264`/`tonemapx`): during a transcoded playback the ffmpeg line should
show `hwaccel vaapi` for decode, `-codec:v:0 h264_qsv` for encode, and CPU
should sit at a fraction of one core:

```sh
docker exec jellyfin ls -l /dev/dri                       # renderD128 visible
docker logs jellyfin --since 2m | grep -i 'hwaccel types'  # qsv listed at startup
# during a transcoded playback — expect h264_qsv / hwaccel vaapi, NOT libx264:
docker exec jellyfin sh -c 'ps -o pcpu,args -C ffmpeg | grep -oE "h264_qsv|libx264|hwaccel [a-z]+"'
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
