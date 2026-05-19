# Radarr

Movie collection manager. **Live** on natto: container `radarr`, WebUI on
`7878`, behind Caddy at `https://radarr.nthncrtr.com`. Pulls indexers from
Prowlarr, hands grabs to qBittorrent, imports completed files into
`/mnt/media/video/movies/`.

> History note: this used to read "stub — not yet deployed". It was deployed
> (alongside Sonarr/Prowlarr) without a tracked WORKLIST mission; the stub
> wording was stale. Verified live 2026-05-17 (a successful end-to-end grab +
> import is in Radarr's history).

## How it actually wires up (verified, not aspirational)

| Thing | Reality on natto |
|---|---|
| Container / image | `radarr` / `lscr.io/linuxserver/radarr:latest`, branch `master` |
| WebUI / auth | `7878`; `AuthenticationRequired = DisabledForLocalAddresses` (Authelia fronts the public route — see `services/authelia/`) |
| Root folder | `/mnt/media/video/movies` |
| Download client | qBittorrent at `gluetun:8080` (qBit shares gluetun's netns), **category `movies`**. Radarr joins `qbittorrent_default` so it reaches gluetun by name; the 127.0.0.1 host-publish path is dead (safety rule 9). |
| API key | `/srv/radarr/config/config.xml` (`<ApiKey>`); mirrored to `HOMEPAGE_VAR_RADARR_KEY` in `/srv/homepage/secrets.env` for the Homepage widget |
| Mounts | `./config:/config`, `/mnt/media:/mnt/media`, `mem_limit: 512m` |

### Where downloaded files land — the part that drifted

qBittorrent's **global default save path is `/mnt/media/_unsorted/torrents`**
(see `services/qbittorrent/README.md`). The `movies` category has an **empty
save path** and qBit's Automatic Torrent Management is **off**, so an *arr
grab does *not* get a per-category folder — every grab, movies and tv alike,
lands flat in `/mnt/media/_unsorted/torrents/`. Radarr then imports from
there into `/mnt/media/video/movies/`.

The older docs claimed a `/mnt/media/downloads/complete/` save path and
hardlink-on-import. Both are wrong now:

- The save path is `/mnt/media/_unsorted/torrents`, not `…/downloads/complete`.
- `/mnt/media` is **exfat**, which has **no hardlink support**. Radarr's
  default ("keep seeding after import") therefore *copies* the file into the
  library while the original keeps seeding from `_unsorted/torrents/` — i.e.
  every imported title is stored **twice** until you stop seeding it. This is
  a known, accepted trade-off here (seedbox + library on one exfat drive),
  not a bug — but budget disk accordingly.

## Activating / re-provisioning (already done, kept for cold-rebuild)

```sh
ssh natto
cd /srv/nthncrtr-repo && git pull && sudo ./deploy.sh radarr
```

First-run UI steps (only needed on a fresh `/srv/radarr/config`):

1. **Settings → Media Management → Root Folders**: add `/mnt/media/video/movies`
2. **Settings → Download Clients**: add qBittorrent — host `gluetun`, port
   `8080`, category `movies`. Radarr joins `qbittorrent_default` (the
   gluetun/qBit compose net, subnet `172.23.0.0/16`) so it can resolve
   `gluetun` by name; that subnet is in qBit's `WebUI\AuthSubnetWhitelist`,
   so no second login. The old `host.docker.internal:8080` path stopped
   working on 2026-05-18 when safety rule 9 rebound qBit's WebUI to
   `127.0.0.1`.
3. **Settings → General → Security**: copy the API key, then on natto:
   ```sh
   sudo -e /srv/homepage/secrets.env   # add HOMEPAGE_VAR_RADARR_KEY=<paste>
   cd /srv/nthncrtr-repo && sudo ./deploy.sh homepage
   ```

## Gotcha: grabs lost across a qBittorrent/Gluetun stack restart

This is the failure that prompted writing this section. **Radarr/Sonarr do
not recover a grab that vanishes from qBittorrent.** If you `deploy.sh
qbittorrent` (or anything recreates the gluetun container — a compose change
to *either* gluetun or qbittorrent does), the whole VPN+qBit stack is torn
down and rebuilt. A torrent grabbed seconds-to-minutes earlier that hasn't
fetched metadata yet (private tracker, no peers that instant — both indexers
here are private, no DHT/PEX) has no resume data, does not survive the
recreate, and Radarr **silently** drops it back to "missing" with no queue
item, no blocklist, no retry. It just looks like "Radarr did nothing".

Mitigation until something better lands (see `services/qbittorrent/README.md`
§ "*arr grabs vs stack restarts"):

- After any `deploy.sh qbittorrent`, kick a Wanted → Missing search in
  Radarr/Sonarr so orphaned items get re-grabbed.
- Don't deploy the qBit stack while a grab is mid-download if you can help it.

## Resource cap

`mem_limit: 512m` is retained deliberately. natto is a **shared hub** (~7 GiB
RAM total — Pi-hole/DNS, Jellyfin, Nextcloud, Navidrome all co-resident), not
the old 2 GB Pi the original note described. Radarr idles ~150–250 MB and has
not been OOM-killed (`RestartCount=0`), so the cap is headroom-protection for
the *other* services, not a constraint Radarr hits. Raise it only if a large
library import starts getting killed.

## Files / paths

| | Path |
|---|---|
| Compose | `/srv/radarr/docker-compose.yml` |
| Config / DB | `/srv/radarr/config/` |
| Movie library | `/mnt/media/video/movies/` |
| Grabs land in | `/mnt/media/_unsorted/torrents/` (qBit global default) |
| Container | `radarr` |

Ports: `7878` (WebUI only — no peer port; torrent traffic is qBit's, via the VPN).
