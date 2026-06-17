# Lidarr

Music collection manager — the audio sibling of Sonarr (TV) / Radarr (movies).
Container `lidarr`, WebUI on `8686`, behind Caddy + Authelia at
`https://lidarr.nthncrtr.com`. Pulls indexers from Prowlarr (including the
**Orpheus** music tracker) and hands grabs to qBittorrent (category `music`).

> ## ⚠️ Read-only by design — Lidarr does NOT import or manage files
>
> On **2026-06-16**, a Lidarr import deleted **6,780 FLACs across 509 albums**
> permanently (copy-into-`Artist/Album`, delete original, no Recycle Bin).
> Recovery was a full re-download from OPS. As a result `/mnt/media` is mounted
> **`:ro`** here — Lidarr **physically cannot write, rename, move, or delete**
> anything in the media tree. It runs as a **monitor + search + grab** front-end
> only: it finds wanted releases via Prowlarr and hands them to qBittorrent.
> **qBittorrent** (its own writable mount, never the culprit) is the *sole*
> writer into `/mnt/media/music` — the tree Navidrome scans — so new grabs still
> appear in Navidrome. See **§ Read-only hardening** below. This matches the
> long-standing workflow (qBit writes directly into the library; no move step).

## How it wires up (matches the other *arrs)

| Thing | Reality on natto |
|---|---|
| Container / image | `lidarr` / `lscr.io/linuxserver/lidarr:latest`, branch `main` |
| WebUI / auth | `8686`; Authentication = Forms/Basic + **Authentication Required = "Disabled for Local Addresses"** (Settings → General → Security). Current Lidarr removed the old `External` option; this is its equivalent — the loopback/proxied path bypasses the in-app login and trusts the Authelia-fronted proxy (the API key still guards `/api`). The two-halves model (this + `127.0.0.1` publish + `arrnet`) is **CLAUDE.md safety rule 9**. |
| Root folder | **`/scratch`** (writable formality — `/srv/lidarr/scratch`). NOT `/mnt/media/music`: that's mounted `:ro` and Lidarr's add-time writability check rejects a read-only folder. No media is stored in `/scratch`. See § Read-only hardening. |
| Download client | qBittorrent at `gluetun:8080` (qBit shares gluetun's netns), **category `music`**. Lidarr joins `qbittorrent_default` so it reaches gluetun by name; the 127.0.0.1 host-publish path is dead (safety rule 9). |
| Indexers | From **Prowlarr** — add Lidarr as a Prowlarr Application + add the Orpheus indexer there (§ Orpheus below). |
| API key | `/srv/lidarr/config/config.xml` (`<ApiKey>`); mirrored to `HOMEPAGE_VAR_LIDARR_KEY` in `/srv/homepage/secrets.env` |
| Mounts | `./config:/config`, **`/mnt/media:/mnt/media:ro`** (read-only), `/srv/lidarr/scratch:/scratch` (writable root-folder formality), `mem_limit: 512m` |

## Read-only hardening

The single, kernel-level guarantee that the 2026-06-16 data loss cannot recur,
plus the in-app belt-and-suspenders layers behind it:

1. **`/mnt/media` mounted `:ro` (primary).** The OS denies every write syscall
   from the Lidarr container against the media tree. No Lidarr bug,
   misconfiguration, or future "organize library" action can delete or rewrite a
   file. This is structural — it holds even if every setting below is wrong.
2. **Writable root folder is a decoy.** Lidarr requires a *writable* root folder
   (it tests write access when you add one), so it gets `/scratch`
   (`/srv/lidarr/scratch`), a small dir holding no media. The real library at
   `/mnt/media/music` is visible to Lidarr only read-only and is never its root.
3. **In-app guards (set in the UI; runtime state, not in repo).** Effective only
   if the mount is ever reverted to writable, but configured anyway:
   - **Recycle Bin** = `/scratch/recycle` — deletes become *recoverable* moves,
     never permanent `unlink`. (Its absence is precisely what turned the
     incident into permanent loss.)
   - **Completed Download Handling → Import** = **OFF** — Lidarr never adopts/
     moves qBit's completed files. qBit owns placement.
   - **Rename Tracks** = **OFF**, **Unmonitor Deleted Tracks** = OFF — no
     rewrite/rename passes over the library.

**Consequence (accepted trade-off):** Lidarr can't import, organize, or even
match your existing library (its root is `/scratch`, not the music tree), so it
will treat owned albums as "missing" and may re-issue grabs — qBit dedupes those
by infohash (HTTP 409 Conflict, harmless). Lidarr's real value here is
*discovery*: monitor wanted artists, RSS/search Orpheus via Prowlarr, hand grabs
to qBit. To unwind hardening (only as an explicit operator decision), revert the
mount to `/mnt/media/music` rw **and** turn the Recycle Bin on first.

### Where downloaded files land

qBittorrent — not Lidarr — places files. The `music` category should have its
save path set to `/mnt/media/music` in qBit (Automatic Torrent Management off),
so Lidarr-issued grabs land in the library that Navidrome scans. `/mnt/media` is
ext4, so seeding the same file costs one inode (no double-storage).

> The `tools/orpheus/download_available.py` side-channel is a *separate* manual
> pipeline: it saves OPS wishlist grabs straight into `/mnt/media/music` under
> qBit category `wishlist`. It does not collide with Lidarr's `music` category.

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
   - **Settings → Media Management → Root Folders**: add **`/scratch`** (NOT
     `/mnt/media/music` — read-only, rejected on add; § Read-only hardening).
   - **Settings → Media Management**: set **Recycle Bin** = `/scratch/recycle`;
     turn **OFF** "Rename Tracks" and Completed Download Handling **Import**.
   - **Settings → Download Clients**: add qBittorrent — host `gluetun`, port
     `8080`, category `music`. Lidarr joins `qbittorrent_default` (subnet
     `172.23.0.0/16`, already in qBit's `WebUI\AuthSubnetWhitelist`), so no
     second login. (The `host.docker.internal:8080` path is dead — safety
     rule 9.)
   - **Settings → General → Security**: set Authentication = **Forms/Basic** +
     **Authentication Required = "Disabled for Local Addresses"** (current Lidarr
     dropped the old `External` option; this is its equivalent — the
     two-halves model, safety rule 9), then copy the API key and on natto:
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
| Root folder (decoy) | `/srv/lidarr/scratch` → `/scratch` in-container (writable; no media) |
| Music library | `/mnt/media/music/` — **read-only** to Lidarr; shared with Navidrome; written only by qBit |
| Grabs land in | `/mnt/media/music/` via qBit's `music` category (qBit places files, not Lidarr) |
| Container | `lidarr` |

Ports: `8686` (WebUI only — no peer port; torrent traffic is qBit's, via the VPN).
