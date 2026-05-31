# Bazarr

Automatic subtitle downloader that pairs with Sonarr and Radarr. One
container (`lscr.io/linuxserver/bazarr`), SQLite config embedded. Web-admin
tier: tailnet-only, behind Authelia, **not** internet-exposed.

## Why it's here — the HDR + subtitles fix

This is the durable fix for "subtitles break HDR on the LG TV." The video
library is mostly UHD remuxes whose only subtitle tracks are **PGS** (image
subtitles). Image subs can't be delivered to a client as a selectable text
track, so Jellyfin **burns them into the picture**, which forces a full 4K
HDR transcode — heavy on the iGPU, and the HDR→SDR tone-map is the fragile
step that was showing wrong colors (see `services/jellyfin/README.md` §
"HDR tone-mapping").

**Text subtitles (SRT/ASS) avoid all of that**: the client renders them
itself as an overlay, so the video **direct-plays with HDR intact** and
~zero CPU. Bazarr fetches SRTs for the library so you can select a text track
instead of the embedded PGS one. Burn-in (and the OpenCL tonemap fallback)
then only matters for the rare PGS-only release.

## Where things live

| | Path |
|---|---|
| Compose | `/srv/bazarr/docker-compose.yml` |
| Config + SQLite DB | `/srv/bazarr/config/` |
| Media (read-write) | `/mnt/media/` → `/mnt/media` in-container |
| Container | `bazarr` |
| Image | `lscr.io/linuxserver/bazarr:latest` |
| Port | `127.0.0.1:6767:6767` (loopback only — see *Auth*) |
| URL | `https://bazarr.nthncrtr.com` (tailnet-only, behind Authelia) |

### Subtitles are sidecar files — why /mnt/media is read-write

Bazarr writes each subtitle next to its video as a sidecar, e.g.
`/mnt/media/video/movies/<Movie>/<Movie>.en.srt`. That needs **write** access
to the media tree, so `/mnt/media` is mounted read-write here — the same
mount the *arrs already use to import. These are additive text files, not
edits to the media, so safety rule 3 (no destructive ops on `/mnt/media`) is
unaffected. Jellyfin (which mounts `/mnt/media/video` read-only) picks the
sidecars up automatically as external subtitle streams.

## Reaching Sonarr/Radarr — arrnet, by name

Bazarr is on **arrnet** (the shared external `*arr` network, created by
`deploy.sh ensure_arrnet`), so it reaches the *arrs by container name:

| Setting | Host | Port |
|---|---|---|
| Settings → Sonarr | `sonarr` | `8989` |
| Settings → Radarr | `radarr` | `7878` |

Plus each *arr's API key (Settings → General in Sonarr/Radarr). This is the
same pattern Prowlarr uses — **not** `host.docker.internal`, which can't reach
the 127.0.0.1-bound *arr ports (CLAUDE.md safety rule 9 / its consequence
note). Bazarr talks only to the *arrs, so unlike them it does **not** join
`qbittorrent_default`.

## Auth (web-admin tier, same posture as the *arrs)

The gate is **Authelia `forward_auth` + a loopback-only publish**, identical
to Sonarr/Radarr/Prowlarr/qBittorrent (CLAUDE.md safety rule 9):

- The only intended path is `bazarr.nthncrtr.com → Caddy → Authelia → here`.
- Bazarr has no *arr-style `External` auth mode. Its equivalent is
  **Settings → General → Security → Authentication = `None`** — which is only
  safe *because* the port is `127.0.0.1`-bound and Authelia fronts it. Don't
  set it back to a `0.0.0.0` publish (that would be an unauthenticated
  LAN-direct door), and don't add app-level login on top of Authelia (double
  login). Bazarr's own API key still guards `/api`.

## First-run setup

Browse to `https://bazarr.nthncrtr.com` (authenticate at Authelia), then:

1. **Settings → Sonarr / Radarr** — add each with the host/port/API key
   above; "Test" should go green.
2. **Settings → Languages** — enable your languages and create a *Languages
   Profile*, then assign it to existing series/movies (Series / Movies tabs →
   select all → edit → set profile) so Bazarr knows what to search for.
3. **Settings → Providers** — add subtitle providers (e.g. an
   OpenSubtitles.com account; Podnapisi needs none).
4. **Settings → General → Security → Authentication = None** (see *Auth*).

On the TV afterward: in the Jellyfin player's subtitle menu, pick the **SRT
(External)** track rather than the PGS one — the video stays direct-play with
HDR intact.

## Backup policy

`config/` (settings + the small SQLite DB) lives under `/srv`, which the
nightly `natto-*.tgz` tars — so it's covered with no extra wiring. The
subtitle sidecars live on `/mnt/media` alongside the media (not separately
duplicated here); Bazarr can always re-fetch them.

## Operating

```sh
# Restart (no DNS impact, no confirmation needed — not Pi-hole):
cd /srv/bazarr && docker compose restart

# Logs:
docker logs -f bazarr

# Update to a newer image:
cd /srv/bazarr && docker compose pull && docker compose up -d
```

## Activation status

Scaffolded in WORKLIST 10.1 (repo files + deploy plumbing). Comes up via
`sudo ./deploy.sh bazarr`. The `bazarr.nthncrtr.com` Cloudflare A record
(→ natto's Tailscale IP, DNS-only), the Sonarr/Radarr connections, the
languages profile assignment, providers, and `Authentication = None` are
operator actions — see WORKLIST 10.1.
