# Jellyfin

Local media server for the video library on the 5TB drive. One container
(`lscr.io/linuxserver/jellyfin`), no database sidecar (Jellyfin embeds
SQLite). **One of two deliberately internet-exposed services** (the other
is [Seerr](../seerr/), the request manager; trusted users, per-user
Jellyfin accounts — operator decisions, WORKLIST 6.6 + 6.7). Everything
else on natto stays tailnet-only; the scoping that keeps it that way is
described under *Reachability* below — read it before touching the
Caddyfile or the router.

## Where things live

| | Path |
|---|---|
| Compose | `/srv/jellyfin/docker-compose.yml` |
| Server config + library DB + metadata | `/srv/jellyfin/config/` |
| Transcode + image cache | `/srv/jellyfin/cache/` |
| Video (read-only) | `/mnt/media/video/` → `/media/video` in-container |
| Music (read-only) | `/mnt/media/music/` → `/media/music` in-container (also served by Navidrome — see *Music* below) |
| Container | `jellyfin` |
| Image | `lscr.io/linuxserver/jellyfin:latest` |
| Networking | `network_mode: host` — binds `8096` (+ `8920`, `1900/udp`, `7359/udp`) directly on natto. Not bridge/published; see compose header (DNS-rebinding guard / stable `127.0.0.1` proxy) |
| Reachability | **Public** at `https://play.nthncrtr.com` (one clean URL, no port, inside + out) via Cloudflare Tunnel; raw `http://natto:8096` / tailnet still work |

### How "public, but only Jellyfin + Seerr" actually works

This is one of two `*.nthncrtr.com` names reachable from the internet
(the other is `requests.nthncrtr.com` → Seerr), and the containment is
structural — the Cloudflare Tunnel's `ingress:` list is the entire
exposure allowlist:

1. **Cloudflare Tunnel** (`services/cloudflared`): `cloudflared` on natto
   dials **out** to Cloudflare and serves only the ingress rules
   `play.nthncrtr.com → http://localhost:8096` (Jellyfin) and
   `requests.nthncrtr.com → http://localhost:5055` (Seerr). Nothing else
   on natto is reachable through it, by construction — better scoping
   than any router/Caddy trick. The router is bypassed entirely.
2. **Inside path** (`services/caddy/Caddyfile` + Pi-hole split-horizon):
   LAN clients must *not* round-trip through Cloudflare for local 4k.
   Pi-hole resolves `play.nthncrtr.com` → natto's LAN IP → Caddy's
   `play.nthncrtr.com` `:443` block → Jellyfin. **No `import authelia`** —
   forward_auth breaks Jellyfin's native TV/phone clients (WORKLIST
   6.4/6.6). Caddy is *not* in the public path (Cloudflare provides the
   edge cert there); this block is inside-only.
3. **One `PublishedServerUrl` = `https://play.nthncrtr.com`** (no port)
   is correct for both: outside resolves to Cloudflare → tunnel → Jellyfin;
   inside resolves (split-horizon) to Caddy → Jellyfin.

> **Why a tunnel and not a router port-forward:** GFiber makes the forward
> impossible (it reserves WAN 443, targets a phantom MAC, and only offers
> all-ports DMZ). Proven dead end — do not retry it. Full reasoning in
> `services/cloudflared/README.md` § "Why a tunnel".

#### Split-horizon: the inside path (`play.nthncrtr.com → LAN`)

The *inside* half is **not optional**. Without it, an inside client resolves
`play.nthncrtr.com` to Cloudflare's proxied edge and every LAN stream hairpins
out to Cloudflare and back — it "works" but adds latency, burns home uplink,
and pushes local 4k through Cloudflare's video-proxying gray area for no reason.
The Pi-hole local override keeps LAN traffic on `Caddy :443 → Jellyfin`.

The record (`play.nthncrtr.com → 192.168.1.240`) is runtime state on natto, not
in this repo, and must be re-added via the Pi-hole UI after a from-scratch
rebuild (WORKLIST 6.6). For how Pi-hole stores these records and how to verify
one, see `services/pihole/README.md` § "Local DNS records (split-horizon)".

### Hardening (this login now faces the internet)

- **Brute-force = Cloudflare WAF Rate-Limiting rule** on the login path,
  configured in the Cloudflare dashboard (zone `nthncrtr.com` → Security →
  WAF → Rate limiting rules). This is dashboard state, **not in this repo**
  (like the Pi-hole split-horizon record). fail2ban was tried first and
  **retired**: through a tunnel attackers hit Cloudflare not natto (host
  bans match nothing), and the fail2ban→Cloudflare-API path dead-ended on
  Cloudflare's deprecation of the zone IP-Access-Rules endpoint (scoped
  tokens → `10000` regardless of permissions). Full saga: WORKLIST 6.6.
  Rule shape: match URI path `/Users/AuthenticateByName` (Jellyfin login),
  ~5 req/min per IP → block/managed-challenge ~10 min. **Required before
  sharing.** Jellyfin still logs failed auths itself if forensics are ever
  needed (`docker exec jellyfin grep 'denied' /config/log/*.log`).
- **Per-user accounts**, each non-admin where appropriate, library access
  scoped, Downloads/Live-TV/management off as desired. Strong password on
  *every* account including admin.
- Dashboard → Networking → **Known proxies = `127.0.0.1`** (`cloudflared`
  connects from localhost) so Jellyfin behaves correctly behind the proxy
  and logs real client IPs.
- Dashboard → Networking → **disable UPnP automatic port mapping** — there
  is no inbound port to map (the tunnel is outbound); don't let Jellyfin
  punch a hole.
- Keep the image current (`docker compose pull`) — CVE exposure now
  matters; this is a public service.
- Set Dashboard → Playback → **Internet streaming bitrate limit**
  (~10–15 Mbps): protects the home uplink and limits how much traffic
  traverses Cloudflare's video-proxying gray area.

### Why config on internal disk, not the 5TB

Jellyfin's library DB (SQLite) is service state, so `config/` and `cache/` live
on natto's internal SSD (`/srv`); only the media stays on `/mnt/media/video`,
bind-mounted **read-only**. This is the repo-wide storage split — see
`runbooks/media-layout.md` § "Storage model".

### History: it used to be Tailscale-only

Through WORKLIST 6.2 Jellyfin was Tailscale-only / LAN (same posture as
Nextcloud, which still is). WORKLIST 6.6 made it public-for-trusted-users.
That mission first tried a router port-forward (+ `services/ddns`); GFiber
made that impossible (see the tunnel callout above), so it pivoted to the
Cloudflare Tunnel model documented here — `services/ddns` was removed.
Nextcloud did **not** change — it remains Tailscale-only with no
Caddy/Cloudflare route.

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
| Music  | Music  | `/media/music` |

These map to `/mnt/media/video/{movies,tv}` and `/mnt/media/music` on natto
(already populated). All binds are read-only, so Jellyfin will not write back
into the media tree (metadata/NFOs land in `/srv/jellyfin/config` instead —
fine for this setup).

### Music: two servers on one library, on purpose

Navidrome (at `music.nthncrtr.com`, tailnet-only) is the canonical music
server — it's Subsonic-protocol, handles Last.fm scrobbling
(`services/navidrome/README.md`), and is what desktop/phone Subsonic clients
talk to. Jellyfin's music library exists specifically to reach the **LG webOS
TV app** (and other Jellyfin-native TV/console clients) where no good
Subsonic client exists. The two scan and tag independently, and "now playing"
state lives in whichever one you used — that's expected, not a bug. Both
point at `/mnt/media/music` read-only, so neither can corrupt the other's
view of the files.

This also means **music is now reachable via the public
`play.nthncrtr.com`** (the Jellyfin tunnel doesn't distinguish libraries) —
not just tailnet, the way Navidrome is. The per-user-account + WAF
rate-limit posture (above) covers it the same way it covers video.

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
migration (WORKLIST 6.2). Made public-for-trusted-users in WORKLIST 6.6 via
a **Cloudflare Tunnel** (`services/cloudflared`) + a Cloudflare WAF
Rate-Limiting rule for brute-force + Pi-hole split-horizon for the inside
path. Comes up via `bootstrap/natto.sh` + `deploy.sh jellyfin cloudflared`;
the `cloudflared tunnel` login/create/route, the Cloudflare Rate-Limiting
rule, the Pi-hole split-horizon record and Jellyfin Known-proxies/per-user
accounts are operator actions (see WORKLIST 6.6 and the per-service
READMEs). fail2ban was tried for brute-force and retired (WORKLIST 6.6 —
Cloudflare deprecated the zone IP-Access-Rules API it depended on).
