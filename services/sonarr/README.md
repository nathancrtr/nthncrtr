# Sonarr

TV series collection manager. **Live** on natto: container `sonarr`, WebUI on
`8989`, behind Caddy at `https://sonarr.nthncrtr.com`. Pulls indexers from
Prowlarr, hands grabs to qBittorrent, imports completed files into
`/mnt/media/video/tv/`.

> History note: this used to read "stub — not yet deployed". It was deployed
> (alongside Radarr/Prowlarr) without a tracked WORKLIST mission; the stub
> wording was stale. Verified live 2026-05-17.

## How it actually wires up (verified, not aspirational)

| Thing | Reality on natto |
|---|---|
| Container / image | `sonarr` / `lscr.io/linuxserver/sonarr:latest`, branch `main` |
| WebUI / auth | `8989`; `AuthenticationRequired = DisabledForLocalAddresses` (Authelia fronts the public route) |
| Root folder | `/mnt/media/video/tv` |
| Download client | qBittorrent at `host.docker.internal:8080`, **category `tv`** |
| API key | `/srv/sonarr/config/config.xml` (`<ApiKey>`); mirrored to `HOMEPAGE_VAR_SONARR_KEY` in `/srv/homepage/secrets.env` |
| Mounts | `./config:/config`, `/mnt/media:/mnt/media`, `mem_limit: 512m` |

### Ghost `tv-sonarr` category — clean it up

qBittorrent currently has **both** a `tv` category (the one Sonarr is
configured to use) **and** a stray `tv-sonarr` category. `tv-sonarr` is
Sonarr's built-in default name; its existence means Sonarr ran at some point
*before* the download client was set to category `tv`. It has no torrents and
nothing references it. It's harmless but it's a footgun (a future config slip
could split downloads across two categories). Delete it in the qBit UI
(Categories → right-click `tv-sonarr` → Delete) — operator action, not
automated, since touching qBit categories is out of `deploy.sh`'s scope.

### Where downloaded files land — the part that drifted

Same as Radarr: qBit's global save path is `/mnt/media/_unsorted/torrents`,
the `tv` category has an **empty** save path, and Automatic Torrent
Management is **off** — so tv grabs land flat in
`/mnt/media/_unsorted/torrents/`, and Sonarr imports from there into
`/mnt/media/video/tv/`. The old `/mnt/media/downloads/complete/` +
hardlink claims are wrong: that path doesn't exist, and `/mnt/media` is
**exfat** (no hardlinks), so "keep seeding" means each imported episode is
**copied** into the library while the original keeps seeding — stored twice
until seeding stops. Accepted trade-off, but budget disk for it.

## Activating / re-provisioning (already done, kept for cold-rebuild)

```sh
ssh natto
cd /srv/nthncrtr-repo && git pull && sudo ./deploy.sh sonarr
```

First-run UI steps (only on a fresh `/srv/sonarr/config`):

1. **Settings → Media Management → Root Folders**: add `/mnt/media/video/tv`
2. **Settings → Download Clients**: add qBittorrent — `host.docker.internal`,
   port `8080`, category `tv`. (Reaches qBit over the Docker bridge, so it
   needs the WebUI username/password — localhost-bypass does not apply.)
3. **Settings → General → Security**: copy the API key, then on natto:
   ```sh
   sudo -e /srv/homepage/secrets.env   # add HOMEPAGE_VAR_SONARR_KEY=<paste>
   cd /srv/nthncrtr-repo && sudo ./deploy.sh homepage
   ```

## Gotcha: grabs lost across a qBittorrent/Gluetun stack restart

**This is the bug that prompted the 2026-05-17 debugging session.** Concrete
incident: Sonarr grabbed *Planet Earth III S01E01* at 20:00 ("Report sent to
qBittorrent"). At ~20:36 the seedbox commit (`9c25f5c`) was deployed, which
changed the **gluetun** service and so recreated the whole gluetun+qBit
stack. Sonarr logged *"Unable to retrieve queue and history items from
qBittorrent"*, the not-yet-started private-tracker torrent did not survive
the recreate, and Sonarr **silently** dropped it — empty queue, no blocklist,
no retry, all 8 episodes still "missing". It looked exactly like "the *arr is
non-functional"; it wasn't slow, the grab was *eaten by a deploy*.

Sonarr/Radarr have no auto-recovery for a torrent that disappears out from
under them. Mitigation until something better lands (see
`services/qbittorrent/README.md` § "*arr grabs vs stack restarts"):

- After any `deploy.sh qbittorrent`, trigger Wanted → Missing search so
  orphaned grabs get re-issued.
- A manually-added qBit torrent with **no category** (e.g. a season pack you
  grabbed by hand) is *not* a Sonarr-tracked download — Sonarr will never
  import it and will keep the episodes "missing". Use Sonarr's
  Manual Import on the folder, or add it with category `tv`, if you want
  Sonarr to adopt it.

## Resource cap

`mem_limit: 512m` retained deliberately — see the Radarr README's identical
section. natto is a ~7 GiB shared hub, not the old 2 GB Pi; Sonarr idles
~150–250 MB and has not been OOM-killed (`RestartCount=0`). The cap protects
the *other* co-resident services.

## Files / paths

| | Path |
|---|---|
| Compose | `/srv/sonarr/docker-compose.yml` |
| Config / DB | `/srv/sonarr/config/` |
| TV library | `/mnt/media/video/tv/` |
| Grabs land in | `/mnt/media/_unsorted/torrents/` (qBit global default) |
| Container | `sonarr` |

Ports: `8989` (WebUI only — no peer port; torrent traffic is qBit's, via the VPN).
