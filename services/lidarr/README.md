# Lidarr

Music collection manager — the audio sibling of Sonarr (TV) / Radarr (movies).
Container `lidarr`, WebUI on `8686`, behind Caddy + Authelia at
`https://lidarr.nthncrtr.com`. Pulls indexers from Prowlarr (including the
**Orpheus** music tracker), hands grabs to qBittorrent (category `music`), and
imports completed releases into `/mnt/media/music/` — **the same tree Navidrome
scans**, so Lidarr-managed albums appear in Navidrome automatically.

> Status: **scaffolded** (repo plumbing shipped; operator activation pending —
> see WORKLIST Phase 11). The "how it wires up" table below is the intended
> shape; the runtime config (root folder, download client, API key,
> Authentication=External) is set in the UI on first run.

## How it wires up (matches the other *arrs)

| Thing | Reality on natto |
|---|---|
| Container / image | `lidarr` / `lscr.io/linuxserver/lidarr:latest`, branch `main` |
| WebUI / auth | `8686`; set `AuthenticationMethod=External` in Settings → General → Security — no in-app login page, trusts the Authelia-fronted proxy (the API key still guards `/api`). The full two-halves model (External + `127.0.0.1` publish + `arrnet`) is **CLAUDE.md safety rule 9**. |
| Root folder | `/mnt/media/music` (where Navidrome scans) |
| Download client | qBittorrent at `gluetun:8080` (qBit shares gluetun's netns), **category `music`**. Lidarr joins `qbittorrent_default` so it reaches gluetun by name; the 127.0.0.1 host-publish path is dead (safety rule 9). |
| Indexers | From **Prowlarr** — add Lidarr as a Prowlarr Application + add the Orpheus indexer there (§ Orpheus below). |
| API key | `/srv/lidarr/config/config.xml` (`<ApiKey>`); mirrored to `HOMEPAGE_VAR_LIDARR_KEY` in `/srv/homepage/secrets.env` |
| Mounts | `./config:/config`, `/mnt/media:/mnt/media`, `mem_limit: 512m` |

### Where downloaded files land

Same model as Sonarr/Radarr: qBit's global save path is
`/mnt/media/_unsorted/torrents`, the `music` category has an **empty** save
path, and Automatic Torrent Management is **off** — so music grabs land flat in
`/mnt/media/_unsorted/torrents/`, and Lidarr imports from there into
`/mnt/media/music/`. `/mnt/media` is **ext4**, so "keep seeding" hardlinks each
imported track into the library (one inode, one copy on disk). See
`runbooks/media-layout.md` § "Hardlinks on import".

> The `tools/orpheus/download_available.py` side-channel is a *separate* manual
> pipeline: it saves OPS wishlist grabs straight into `/mnt/media/music` under
> qBit category `wishlist`. It does not collide with Lidarr's `music` category
> — different category, and Lidarr only adopts downloads it issued itself.

## Orpheus (OPS) — the music tracker, via Prowlarr

Following the established pattern (Prowlarr is the single indexer manager for
the *arr stack — see `services/prowlarr/`), Orpheus is added **in Prowlarr**,
not directly in Lidarr. Prowlarr then syncs it to Lidarr as a Torznab indexer
with **RSS sync** enabled, so new Orpheus uploads matching wanted albums flow
into Lidarr automatically.

1. **Prowlarr → Indexers → Add Indexer → "Orpheus"** (built-in Gazelle
   definition). Auth: the **OPS API key** — the same value already in
   `tools/orpheus/secrets.env` (`OPS_API_KEY`; generate at
   `orpheus.network/user.php?action=edit` → Access Settings → Create API Key,
   Torrents-read scope). Test → Save.
2. **Prowlarr → Settings → Apps → Add Application → Lidarr**: Prowlarr Server
   `http://prowlarr:9696`, Lidarr Server `http://lidarr:8686` (both resolve by
   container name over `arrnet`), Lidarr's API key. Sync Level "Full Sync".
3. Leave the Lidarr app **tag-less** so Prowlarr syncs all indexers to it —
   the **TAG-SYNC GOTCHA** in `services/prowlarr/docker-compose.yml` applies
   verbatim (a tagged app with untagged indexers silently syncs nothing).
4. Prowlarr pushes Orpheus into Lidarr's indexer list; Lidarr runs RSS sync on
   it on its normal interval. No RSS URL is pasted by hand — Prowlarr owns it.

> Why via Prowlarr and not a hand-pasted RSS URL in Lidarr: it keeps one source
> of truth for indexers (the same place beyond-hd lives for Sonarr/Radarr),
> gets search + RSS in one definition, and means the OPS key lives in exactly
> one indexer config instead of being copied around.

## Activating (operator steps — not automated)

```sh
ssh -t natto
cd /srv/nthncrtr-repo && git pull && sudo ./deploy.sh lidarr
```

Then:

1. **Cloudflare A record**: `lidarr / 100.122.71.33 / DNS only` (grey cloud) —
   without it the name doesn't resolve even though Caddy serves it. (CLAUDE.md
   § New-subdomain gotcha — inside clients resolve via Pi-hole forwarding the
   zone; add a Pi-hole local override only if you want LAN-direct like
   `music`/`play`.)
2. `sudo ./deploy.sh caddy` (after the A record exists) → reload the
   `lidarr.nthncrtr.com` vhost.
3. In Lidarr (https://lidarr.nthncrtr.com, behind Authelia):
   - **Settings → Media Management → Root Folders**: add `/mnt/media/music`.
   - **Settings → Download Clients**: add qBittorrent — host `gluetun`, port
     `8080`, category `music`. Lidarr joins `qbittorrent_default` (subnet
     `172.23.0.0/16`, already in qBit's `WebUI\AuthSubnetWhitelist`), so no
     second login. (The `host.docker.internal:8080` path is dead — safety
     rule 9.)
   - **Settings → General → Security**: set Authentication = **External** (the
     two-halves model — safety rule 9), then copy the API key and on natto:
     ```sh
     sudo -e /srv/homepage/secrets.env   # add HOMEPAGE_VAR_LIDARR_KEY=<paste>
     cd /srv/nthncrtr-repo && sudo ./deploy.sh homepage
     ```
4. Wire up Orpheus via Prowlarr (§ Orpheus above).

## Gotcha: grabs lost across a qBittorrent/Gluetun stack restart

Identical to Sonarr/Radarr — a `deploy.sh qbittorrent` (or any gluetun/qBit
compose change) recreates the VPN+qBit stack and an in-flight, not-yet-started
private-tracker grab does **not** survive it; Lidarr silently drops it back to
"missing" with no retry. After any qBit redeploy, re-run Lidarr's
Wanted → Missing search to recover orphaned grabs. Full incident write-up:
`services/sonarr/README.md` § "grabs lost…" and
`services/qbittorrent/README.md` § "*arr grabs vs stack restarts".

## Resource cap

`mem_limit: 512m` — same deliberate bound as the other *arrs on this shared
~7 GiB hub. Lidarr idles ~150–250 MB; the cap protects co-resident services.

## Files / paths

| | Path |
|---|---|
| Compose | `/srv/lidarr/docker-compose.yml` |
| Config / DB | `/srv/lidarr/config/` |
| Music library | `/mnt/media/music/` (shared with Navidrome) |
| Grabs land in | `/mnt/media/_unsorted/torrents/` (qBit global default) |
| Container | `lidarr` |

Ports: `8686` (WebUI only — no peer port; torrent traffic is qBit's, via the VPN).
