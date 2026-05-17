# Jellyfin

Local media server for the video library on the 5TB drive. One container
(`lscr.io/linuxserver/jellyfin`), no database sidecar (Jellyfin embeds
SQLite). **This is the one deliberately internet-exposed service** (trusted
users, per-user Jellyfin accounts — operator decision, WORKLIST 6.6).
Everything else on natto stays tailnet-only; the scoping that keeps it that
way is described under *Reachability* below — read it before touching the
Caddyfile or the router.

## Where things live

| | Path |
|---|---|
| Compose | `/srv/jellyfin/docker-compose.yml` |
| Server config + library DB + metadata | `/srv/jellyfin/config/` |
| Transcode + image cache | `/srv/jellyfin/cache/` |
| Media (read-only) | `/mnt/media/video/` → `/media/video` in-container |
| Container | `jellyfin` |
| Image | `lscr.io/linuxserver/jellyfin:latest` |
| Networking | `network_mode: host` — binds `8096` (+ `8920`, `1900/udp`, `7359/udp`) directly on natto. Not bridge/published; see compose header (DNS-rebinding guard / stable `127.0.0.1` proxy) |
| Reachability | **Public** at `https://play.nthncrtr.com` (one URL, inside + out); raw `http://natto:8096` / tailnet still work |

### How "public, but only Jellyfin" actually works

This is the only `*.nthncrtr.com` name reachable from the internet. The
containment is deliberate and lives in three coupled places — change one
and you must reason about the other two:

1. **Caddy** (`services/caddy/Caddyfile`): the block is
   `play.nthncrtr.com:443, play.nthncrtr.com:8443`. `:8443` is a
   dedicated listener; every other vhost is `:443` only. **No
   `import authelia`** — forward_auth breaks Jellyfin's native TV/phone
   clients (WORKLIST 6.4/6.6).
2. **Router** (operator-managed, not in repo): port-forwards WAN `tcp/443`
   → `natto-LAN-IP:8443`. Because only `:8443` is forwarded and only the
   Jellyfin block listens there, **only Jellyfin is exposed**. Forwarding
   `:443` instead would put Navidrome / Pi-hole / the *arrs / roaster on
   the internet — do not.
3. **DNS, split-horizon**:
   - *Outside* — Cloudflare A record `play` → home WAN IP (grey-cloud,
     proxy off), kept current by `services/ddns`.
   - *Inside* — Pi-hole local DNS `play.nthncrtr.com` → natto LAN IP,
     so inside clients hit the `:443` listener directly and don't depend
     on router NAT hairpin. One `PublishedServerUrl` works both ways.

Cert issuance is unaffected: the global DNS-01 challenge doesn't need the
host reachable on 443, so the non-standard `:8443` is a non-issue.

#### Split-horizon: local DNS records on this Pi-hole (v6)

The *inside* half is **not optional and not automatic** — without it an
inside client resolves `play.nthncrtr.com` to the home WAN IP, bounces off
the router (NAT hairpin), and the **router** (not Jellyfin, not Caddy)
returns `Forbidden: Rejected request from RFC1918 IP to public server
address`. That exact symptom = the split-horizon record is missing.

This Pi-hole is **v6** (Core v6.x / FTL v6.x). v6 has *two* places a local
A record can live; know both:

- **`/etc/pihole/hosts/custom.list`** — what the web UI **Settings → Local
  DNS Records** writes. Format: one `IP<space>name` per line. Saving via
  the UI hot-reloads FTL automatically (no container restart, no DNS
  outage). **This is the preferred path** — it can't clobber other records.
  `play.nthncrtr.com → 192.168.1.240` was added here.
- **`dns.hosts = [ … ]` in `/etc/pihole/pihole.toml`** — the v6 settings
  file. The pre-existing `192.168.1.50 natto.nthncrtr.com` record lives
  here (marked `### CHANGED`). Editing the toml directly needs a reload
  (`pihole reloaddns`) and risks clobbering the array if hand-edited —
  prefer the UI unless scripting.

Both are **runtime state on natto, not in this repo.** They are captured by
the nightly `/srv` backup (Pi-hole config lives under `/srv/pihole`), so a
restore brings them back — but a **from-scratch natto rebuild without a
backup restore loses split-horizon**, and the symptom is the router
`Forbidden` above. Re-add per WORKLIST 6.6 step 3 after any such rebuild.

Verify the record from natto:

```sh
dig +short play.nthncrtr.com @127.0.0.1   # must be natto's LAN IP, NOT the WAN IP
```

### Hardening (this login now faces the internet)

- **fail2ban** (`services/fail2ban`) bans brute-force source IPs at the
  host firewall. It is only useful once Jellyfin's **Known proxies =
  `127.0.0.1`** is set (Dashboard → Networking) so the auth log carries
  the real client IP instead of Caddy's `127.0.0.1`. **Required step.**
- **Per-user accounts**, each non-admin where appropriate, library access
  scoped, Downloads/Live-TV/management off as desired. Strong password on
  *every* account including admin.
- Dashboard → Networking → **disable UPnP automatic port mapping** (the
  router forward is explicit; don't let Jellyfin punch its own).
- Keep the image current (`docker compose pull`) — CVE exposure now
  matters; this is a public service.
- Layer-2 (follow-up, not shipped): add `caddy-ratelimit` to
  `services/caddy/build.sh` to throttle `/Users/AuthenticateByName` at the
  edge. Tracked in WORKLIST 6.6.

### Why config on internal disk, not the 5TB

Jellyfin's library is SQLite; it needs POSIX locking and atomic renames the
exfat 5TB can't give (the drive is exfat by design so it moves between hosts
UUID-stable — see `runbooks/migrate-natto.md`). Same reasoning as Nextcloud.
Only `config/` and `cache/` live on the Beelink's internal ext4; the media
itself stays on `/mnt/media/video` and is bind-mounted **read-only**.

### History: it used to be Tailscale-only

Through WORKLIST 6.2 Jellyfin was Tailscale-only / LAN (same posture as
Nextcloud, which still is). WORKLIST 6.6 changed that to public-for-trusted-
users via the router-port-forward model above. Nextcloud did **not** change
— it remains Tailscale-only with no Caddy/Cloudflare route.

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
**This matters more now that Jellyfin is public:** a remote client on a
constrained uplink is *more* likely to force a transcode (resolution/bitrate
down-scaling on top of the codec/HDR reasons), so verify QSV actually
engages for a remote 4k playback, not just a LAN one.

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

Stood up Tailscale-only alongside Nextcloud after the Pi → Beelink
migration (WORKLIST 6.2). Made public-for-trusted-users in WORKLIST 6.6
(Caddy `:8443` route + `services/ddns` + `services/fail2ban` + router
port-forward + Cloudflare/Pi-hole split-horizon). Comes up via
`bootstrap/natto.sh` + `deploy.sh jellyfin ddns fail2ban`; the router /
Cloudflare / Known-proxies steps are operator actions (see WORKLIST 6.6).
