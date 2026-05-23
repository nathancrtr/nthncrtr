# qBittorrent (behind Proton VPN via Gluetun)

Torrent client. All traffic is routed through a Gluetun sidecar that maintains a Proton VPN WireGuard tunnel. If Gluetun is unhealthy, qBittorrent has no network — the "kill switch" is structural, not a setting. Caddy routes `torrent.nthncrtr.com` → `natto:8080` (published on the gluetun container, since qbittorrent shares its netns and can't publish ports itself).

See [`runbooks/proton-vpn-setup.md`](../../runbooks/proton-vpn-setup.md) for the full integration walkthrough.

## Provisioning secrets on natto

```sh
ssh natto
sudo install -o root -g root -m 0600 /dev/null /srv/qbittorrent/secrets.env
sudoedit /srv/qbittorrent/secrets.env
# Populate from services/qbittorrent/secrets.env.example with values from a
# Proton VPN WireGuard config (Port Forwarding enabled).
```

The compose file declares `env_file` with `required: false`, so `docker compose config` still works on workhorse where the file is intentionally absent.

## Activating

```sh
ssh natto
cd /srv/nthncrtr-repo && git pull && sudo ./deploy.sh qbittorrent
docker logs qbittorrent | grep -i 'webui password'   # one-time temporary password
```

Then log in at `https://torrent.nthncrtr.com`, change the admin password, and (recommended) restrict the WebUI to LAN-only via Settings → Web UI.

## One-time WebUI config

After first deploy, log in at `https://torrent.nthncrtr.com` and set:

- **Options → Downloads → Default Save Path**: `/mnt/media/_unsorted/torrents` (deploy.sh creates this directory, owned by nthncrtr:nthncrtr). The earlier `/srv/qbittorrent/downloads/` mount has been removed — qbit now sees `/mnt/media` directly so Radarr/Sonarr can hardlink final files instead of copying.
- **Options → Connection**: uncheck "Use UPnP / NAT-PMP port forwarding from my router". (The listening port is managed automatically — see § Port forwarding sync. Don't set it manually; `apply-tuning.sh` also pins `upnp:false`/`random_port:false`.)
- **Options → Advanced → Network Interface**: `tun0`. Gluetun normalizes the WireGuard interface to `tun0`, same name as OpenVPN — qBit will refuse to bind if Gluetun's tunnel isn't up.
- **Options → Web UI → Authentication**: enable "Bypass authentication for clients on localhost". This is required for the port-updater sidecar (it runs in gluetun's netns, so it reaches qBit as 127.0.0.1 and would otherwise be rejected). Connections from outside the netns — natto's host processes, other Docker containers, your browser — still arrive over the Docker bridge and continue to need a password.

## Port forwarding sync (two layers)

Proton hands out a forwarded port that changes on every VPN reconnect. qBit must be listening on exactly that port or inbound peers can't reach it — and on a **private-tracker** torrent there's no DHT/PEX fallback, so a stale port starves it to single-digit KiB/s. Two mechanisms keep qBit in sync:

1. **Primary — gluetun `VPN_PORT_FORWARDING_UP_COMMAND`** (in `docker-compose.yml`). gluetun runs this hook *the instant* the forwarded port changes, in its own netns, `wget`-POSTing the new port to qBit's `setPreferences`. Zero-latency; no poll window.
2. **Backstop — `qbit-port-updater` sidecar**. A small container alongside gluetun in the same netns, watching `/tmp/gluetun/forwarded_port` and pushing changes on a 60s poll. Covers the case where qBit itself restarts (lost its port) but the gluetun hook didn't re-fire. Script: `services/qbittorrent/port-updater.sh`, bind-mounted; edit on natto with `sudoedit` and `docker compose restart qbit-port-updater` to iterate.

**Debugging:**

```sh
docker logs qbit-port-updater                                # see what it's done
docker exec gluetun cat /tmp/gluetun/forwarded_port          # current Proton port
docker exec qbit-port-updater cat /state/forwarded_port      # same file, from sidecar's view
```

A common failure mode: `WARN: failed to push port` — usually means qBit's localhost-bypass setting wasn't enabled. Re-check the Web UI Authentication option above.

## *arr grabs vs stack restarts (known failure mode + design)

**Symptom that surfaced this:** a Sonarr/Radarr download appears "extremely
slow and/or non-functional" while a hand-added torrent works perfectly.

**Root cause:** qBittorrent shares gluetun's network namespace
(`network_mode: service:gluetun`). A compose change to **either** gluetun or
qbittorrent makes `docker compose up -d` recreate the gluetun container,
which tears down and rebuilds the *entire* VPN+qBit stack. `deploy.sh
qbittorrent` does this every time the compose file changes (e.g. commit
`9c25f5c` added a gluetun `environment:` block → full recreate at deploy
time). During the gap qBit's WebUI is unreachable; Sonarr/Radarr log
*"Unable to retrieve queue and history items from qBittorrent"*. A torrent
grabbed shortly before — **not yet past the metadata stage**, which is the
norm on the two **private** indexers here (BeyondHD, TorrentLeech: no
DHT/PEX, peers only via tracker announce on the forwarded port) — has no
`.fastresume` to persist, so it does **not** survive the recreate. The 768
already-seeding music torrents and any in-progress download *do* survive
(they have resume data); only the just-issued, no-metadata grab is lost.

The *arrs have **no auto-recovery** for a torrent that vanishes from the
download client: empty queue, no blocklist, no re-search. The episode/movie
silently reverts to "missing". It reads as "the *arr did nothing".

**Why manual works:** a hand-picked torrent is well-seeded and added when no
deploy is happening, so it gets metadata immediately and persists.

### Mitigations (in order of effort)

1. **Operational (in place now):** `deploy.sh qbittorrent` prints a `warn`
   after every run telling the operator to re-trigger Wanted→Missing in
   Sonarr/Radarr. Zero code risk; relies on the operator acting.
2. **Don't recreate the stack for tuning-only changes.** Most qBit "deploys"
   only change `apply-tuning.sh` (applied live via the WebUI API, no
   restart needed) — yet any compose-file edit still forces a full recreate.
   A future `deploy.sh` improvement: detect when only non-compose files
   changed and skip `compose_up` (run `apply-tuning.sh` alone). Bounded,
   testable, no service-behavior change. **Not yet implemented** — needs a
   reliable "did the effective compose config actually change" check
   (`docker compose config --hash`, or compare against the running
   container's config digest) so it never skips a real recreate.
3. **Auto-recover after a recreate.** A post-deploy hook that calls the
   Sonarr/Radarr `MissingEpisodeSearch` / `MoviesSearch` command APIs (keys
   already in `/srv/homepage/secrets.env`). Highest automation, but couples
   `deploy.sh` to *arr API surface + keys; defer until option 2 proves
   insufficient.

Decision: ship #1 now (done), document #2/#3 as the planned path. This is
deliberately a design note, not silent code — the *arr READMEs link here.

## Seedbox tuning

natto is a shared household hub (Pi-hole DNS, Jellyfin, Nextcloud, Navidrome), **not** a dedicated seedbox — so the tuning is *generous-but-bounded*, never "unlimited". It is version-controlled, not left to drift in qBittorrent.conf: `apply-tuning.sh` is the source of truth and `deploy.sh qbittorrent` re-asserts it via the WebUI API on every deploy (idempotent; survives a `config/` wipe). Change values in `apply-tuning.sh` and this table together.

| Setting | Value | Rationale |
|---|---|---|
| Queueing | **on** (10 active downloads, 1000 active uploads, slow torrents *do* count) | Was off through 2026-05-22 on the theory that "every completed torrent seeds 24/7" needs no queueing. After the Orpheus + BHD mass-restores (800+ torrents, hundreds simultaneously in `downloading`/`stalledDL`) the 2000-conn global cap split into ~3–9 peers per torrent (measured 509 of 536) — *over*-subscribed in aggregate but starved per-torrent — and throughput collapsed to <1 MB/s against a 30 MiB/s ceiling. Queueing 10 downloads at a time gives each ~200 connections (matches `max_connec_per_torrent`). **`dont_count_slow_torrents` is deliberately `false`** here: in the mass-restore steady state every active torrent is *already* under the 2 KB/s "slow" threshold, so `true` would mark all torrents slow → none counts → the 10-slot cap silently nullifies itself (verified 2026-05-22). With `false` the cap is a hard limit; ~370 torrents sit in `queuedDL` and rotate in as the active ones finish. `max_active_uploads=1000` preserves the "everything seeds" guarantee — only *downloads* are queued. |
| Download / upload limit (off-peak) | **30 / 30 MiB/s** | Full speed 20:00–08:00. Bounded so it never fully saturates natto's link. |
| Alternative limits (scheduled) | **15 down / 30 MiB/s up** | Active **08:00–20:00 daily**. Daytime *download* cap (15 MiB/s) leaves headroom for Jellyfin/Nextcloud/DNS during waking hours. Daytime *upload* cap matches off-peak (30 MiB/s) because the binding upload constraint is /mnt/media's USB-HDD random-read rate (~6 MB/s, measured 2026-05-20) — qBit can't exceed it regardless, so a smaller daytime upload cap just costs ratio. Revisit after NVMe upgrade. |
| Max connections (global / per-torrent) | **2000 / 200** | High-volume seeding. |
| Max upload slots (global / per-torrent) | **100 / 8** | Defaults (20/4) starve a many-torrent seedbox. |
| Temp path (in-progress dir) | **`/incomplete`** (bind: `/srv/qbit-incomplete`) | In-progress pieces land on the SATA SSD instead of `/mnt/media` (USB HDD + exfat), which capped aggregate downloads at ~10 MB/s on small random writes regardless of network. Completed files move to `save_path` on `/mnt/media` (cross-fs copy, USB-HDD-bound). Caveat: `/srv` has ~90 GB free, so concurrent in-progress downloads are space-limited until the planned NVMe upgrade. |
| Disk cache | **512 MB** | Default `-1` is ~64 MB. Holds hot pieces in RAM so popular seeds don't re-read from `/mnt/media` (USB HDD, contended) for every leecher request. Safe with the 8 GB host because `vm.swappiness=10` (see `bootstrap/sysctl-natto-tuning.conf`) stops swap thrash. Revisit if RAM is upgraded. |
| Piece extent affinity | **on** | Serves piece requests in extent order instead of peer-arrival order — turns scattered random reads into more sequential ones. Single biggest HDD-seeder knob in libtorrent. |
| Coalesce read/write | **on** | Merges adjacent small I/Os into larger ones, cutting per-op overhead on the slow disk. |
| Hashing threads | **4** | Default 2. Lets a 4-core host finish post-restart rechecks faster, returning torrents to seeding sooner. |
| Reannounce on address change | **on** | When Proton rotates the forwarded port (every few hours), reannounce immediately so trackers don't serve leechers a stale port until the next scheduled announce. Critical for ratio. |

```sh
sudo /srv/qbittorrent/apply-tuning.sh           # re-assert (deploy.sh does this for you)
sudo /srv/qbittorrent/apply-tuning.sh --show    # dump current live prefs, change nothing
```

If you genuinely want unlimited overnight, raise the off-peak `dl_limit`/`up_limit` in `apply-tuning.sh` — but remember a saturated uplink degrades household DNS responsiveness.

## Files / paths

| | Path |
|---|---|
| Compose | `/srv/qbittorrent/docker-compose.yml` |
| qBit config | `/srv/qbittorrent/config/` |
| Gluetun state | `/srv/qbittorrent/gluetun-state/` (contains `forwarded_port`, `ip`, etc.) |
| Port-updater script | `/srv/qbittorrent/port-updater.sh` (bind-mounted into the sidecar) |
| Secrets | `/srv/qbittorrent/secrets.env` (mode 0600, root:root, NOT in repo) |
| qBit data root | `/mnt/media/` (downloads land in `_unsorted/torrents/` by default) |
| Containers | `qbittorrent` (LSIO), `gluetun` (qmcgaw/gluetun), `qbit-port-updater` (curlimages/curl) |

Ports: `8080` (WebUI, published on the gluetun container). The torrent peer port is whatever Proton's port-forward assigns; it is reachable via the VPN's external IP, not via natto's IP, so no host-side publish is needed.

## Disaster recovery: rebuilding from scratch via Orpheus

If `/srv/qbittorrent/config/qBittorrent/BT_backup/` is gone (fresh install, lost drive, beets-stomped-your-music-and-now-nothing-seeds), the operator's Orpheus account is the source of truth for "which torrents was I supposed to have." Two scripts in this directory rebuild from there:

1. **`orpheus-restore.py`** — Python 3, stdlib only. Reads `ORPHEUS_API_KEY` from `/srv/qbittorrent/secrets.env` (or `--secrets` / env var), enumerates the operator's `snatched` and/or `uploaded` torrents via Orpheus's Gazelle API, and downloads each `.torrent` file as `<torrentId>.torrent`. Idempotent: re-running skips files already on disk. Honors Orpheus's ~5-req/10s rate limit with a 2.5s sleep between calls.

2. **`qbit-bulk-add.sh`** — bash. Takes the directory of .torrent files produced above and POSTs each to qBit's WebUI API. Runs as a one-shot `curlimages/curl` container inside gluetun's netns, so qBit's localhost auth-bypass applies — no password needed and nothing crosses the docker bridge. qBit dedupes by infohash, so this is safe to re-run.

3. **`orpheus-plan.py`** — Python 3, stdlib only. Optional. Enriches the `manifest-snatched.json` with per-torrent freeleech status (`action=torrent` API, rate-limited, resumable via checkpoint) and emits a sortable CSV recovery plan: which torrents are globally FL (free), which are largest candidates for token application, which fit in your ratio budget, which to abandon. Use when ratio cost matters and you want a deliberate selection rather than re-snatching everything.

### Procedure

```sh
ssh natto

# 1. One-time: add ORPHEUS_API_KEY to secrets.env. Get a token from
#    Orpheus → User → Settings → Access Settings → Create API key (User scope).
sudoedit /srv/qbittorrent/secrets.env

# 2. Pull fresh .torrent files. snatched is the canonical "everything I've ever
#    downloaded"; uploaded picks up the few torrents this account originated.
cd /srv/qbittorrent
sudo ./orpheus-restore.py --out ./restore --type snatched
sudo ./orpheus-restore.py --out ./restore --type uploaded

# 3. Prove the qBit add flow on ONE torrent before committing to bandwidth.
sudo ./qbit-bulk-add.sh --dir ./restore --limit 1
#    Watch torrent.nthncrtr.com — it should appear, start downloading,
#    and once complete switch to seeding state.

# 4. Once happy, fan out.
sudo ./qbit-bulk-add.sh --dir ./restore
```

`--dry-run` on `orpheus-restore.py` builds the manifest without fetching any .torrent files, useful for sanity-checking what's about to happen. `--paused` on `qbit-bulk-add.sh` adds torrents in a paused state if you want to inspect first.

The expected steady-state result: each torrent's content downloads (from the swarm + tracker), then qBit transitions to seeding, and the tracker's "snatched/seeding" lists for the user reflect reality again. Once `BT_backup/` is repopulated this way, daily `backup.sh` runs will capture it (via `/srv/` in the SOURCES list) so this whole procedure becomes a one-time fix.

## Disaster recovery: rebuilding from scratch via Beyond-HD

Same shape as the Orpheus path above, but for video — with one critical
difference. The operator's BHD "completed" list is the *history* of
everything ever snatched, the bulk of which has been watched and deleted
on purpose. The actual `/mnt/media/video/{movies,tv}` library is a small
strict subset. So the recovery operation must be **match-and-keep** —
never **didn't-match-and-resume**. Adding all 300+ torrents the naive
way would queue every non-matching entry for download, burning bandwidth
+ ratio + disk space to re-acquire content the operator deliberately
threw away.

The safe pattern, baked into the procedure below: **add paused →
let qBit hash-check → keep only the 100%-match torrents, delete the
rest from qBit (files on disk never touched).** Hash-check is the truth
oracle; everything below 100% gets dropped. The category tag is the
safety boundary — the culler refuses to run without one and only acts
on torrents in that category, so it cannot touch music seeds or
*arr-managed downloads.

1. **`bhd-restore.py`** — Python 3, stdlib only. Reads `BHD_API_TOKEN` (URL path) and `BHD_RSS_KEY` (POST body, required for the API to return usable per-torrent `download_url` values) from `/srv/qbittorrent/secrets.env`. Enumerates the operator's `completed` / `seeding` / `leeching` torrent lists via BHD's `/api/torrents/<token>` endpoint with the matching filter flag. Per-torrent: classifies by BHD `category` (`Movies` → `movies/`, `TV` → `tv/`, anything else → `other/`), then downloads the `.torrent` from the API-provided `download_url` as `<bucket>/<torrentId>.torrent`. Idempotent: re-running skips files already on disk. Includes a `--probe` mode that posts once against page 1 and dumps the raw response — useful for sanity-checking pagination and field names against the first real run.

2. **`qbit-bulk-add.sh --paused`** — same script the Orpheus path uses, just called with `--paused` so qBit hash-checks but never starts a peer connection / download until we explicitly resume. Run once per bucket (movies, tv) with the matching savepath/category.

3. **`qbit-keep-only-complete.sh`** — runs a one-shot `python:3-alpine` container inside gluetun's netns. Polls `torrents/info?category=<CAT>` until all in-scope torrents have left the `checking*` state, then for each: `progress >= 1.0` → resume; `progress < 1.0` → delete from qBit with `deleteFiles=false`. Default is dry-run; `--yes` is required to act. Aborts if any in-scope torrent is in an active download state (a signal that `--paused` was missed). Refuses to run without `--category` — that's the safety boundary.

### Procedure

```sh
ssh -t natto

# 1. One-time: add BHD_API_TOKEN and BHD_RSS_KEY to secrets.env. Both come
#    from beyond-hd.me → Settings → Security (separate fields).
sudoedit /srv/qbittorrent/secrets.env

# 2. Sanity-check the API + creds with one POST before any pagination.
cd /srv/qbittorrent
sudo ./bhd-restore.py --probe --type completed

# 3. Pull fresh .torrent files into restore-bhd/{movies,tv,other}/.
sudo ./bhd-restore.py --out ./restore-bhd --type completed

# 4. ADD PAUSED — one bucket at a time. qBit hash-checks each torrent but
#    starts NO downloads because everything is paused on add.
sudo ./qbit-bulk-add.sh --dir ./restore-bhd/movies \
  --savepath /mnt/media/video/movies --category bhd-movies --paused
sudo ./qbit-bulk-add.sh --dir ./restore-bhd/tv \
  --savepath /mnt/media/video/tv     --category bhd-tv     --paused

# 5. DRY-RUN the keep/delete decision. Reports the keep/delete buckets by
#    name, takes no action. Inspect: does the keep count match what you
#    actually still have on disk? Are the "delete" entries actually the
#    content you watched and threw away?
sudo ./qbit-keep-only-complete.sh --category bhd-movies
sudo ./qbit-keep-only-complete.sh --category bhd-tv

# 6. ACT. Keeps the 100%-match torrents (resumes them → seeding); drops
#    everything below 100% from qBit. Files on disk are NEVER touched
#    (deleteFiles=false on every delete call).
sudo ./qbit-keep-only-complete.sh --category bhd-movies --yes
sudo ./qbit-keep-only-complete.sh --category bhd-tv     --yes
```

`--dry-run` on `bhd-restore.py` builds the manifest without fetching any
.torrent files. `restore-bhd/other/` collects any torrent whose BHD
category isn't `Movies` or `TV` — those aren't auto-added (their
savepath is ambiguous); inspect and add manually if needed.

### Why "add paused" is safe even if we never run the culler

A paused torrent in qBit stays paused across restarts (the state is
persisted to `BT_backup/`). If steps 5/6 are skipped, the worst outcome
is N stopped torrents cluttering the qBit UI — no bandwidth, no
download, no ratio impact. The operator can remove them manually from
the UI at any time; `deleteFiles=false` is the default in qBit's UI
delete dialog too.

### When `qbit-keep-only-complete.sh` aborts on "active state"

The culler refuses to act if any in-scope torrent is in a downloading
or uploading state — that almost always means `--paused` was missed on
the bulk-add (so torrents are already pulling pieces from the swarm).
**Don't pass `--allow-active` reflexively.** Stop the offending
torrents first (qBit UI: select all in category → Stop), confirm
they're all in `stoppedDL` / `stoppedUP`, then re-run the culler.
`--allow-active` is only correct if you genuinely want to delete
in-flight downloads.

## Homepage widget

The qBittorrent widget in `services/homepage/config/services.yaml` reaches qBit at **`http://gluetun:8080`** over the shared `qbittorrent_default` docker network — *not* `host.docker.internal` and *not* a host port. Since the Authelia cutover, 8080 is published on `127.0.0.1` only (safety rule 9), so the host-gateway path the widget used to take is dead. qBit shares gluetun's netns, so the reachable container name on that net is `gluetun`. gluetun blocks all inbound by default, so this also requires `FIREWALL_INPUT_PORTS=8080` in the gluetun service (see the compose comment). Add `HOMEPAGE_VAR_QBITTORRENT_PASSWORD=<password>` to `/srv/homepage/secrets.env` (mode 0600) and run `sudo ./deploy.sh homepage` to pick up the secret. Full rationale: `services/homepage/README.md` § "How widgets reach their backends".

## Troubleshooting: WebUI down / 502 / silent crash-loop (stale single-instance lock)

**Symptom:** `torrent.nthncrtr.com` 502s (or 302s straight to the Authelia portal with no qBit behind it), the Homepage widget errors, and `curl http://127.0.0.1:8080` on natto gives `000`. `docker ps` shows `qbittorrent` **Up** (not restarting) and gluetun **healthy** — so it looks fine. But `docker exec qbittorrent ps -ef | grep qbittorrent-nox` shows a **new PID every ~1–2s**, and `/config/qBittorrent/logs/qbittorrent.log` is just this, forever, with **no error**:

```
qBittorrent vX started. Process ID: NNN
Using config directory: /config/qBittorrent
qBittorrent termination initiated
qBittorrent is now ready to exit
```

**Cause:** qBit's single-instance guard. It records its container hostname + PID in `/config/qBittorrent/lockfile` and opens `/config/qBittorrent/ipc-socket`. On a **clean** shutdown it removes both. On an **unclean** stop — host reboot, OOM, `docker kill`, or being killed inside gluetun's netns during a stack recreate — they're left behind carrying the *old* container's hostname. The next qBit can't verify that PID is dead on a "different host", conservatively assumes another instance is live, forwards its CLI args over the stale socket, and **exits 0**. s6 respawns it; same result; ~1s loop forever. The WebUI never binds. (This caused a multi-hour outage on 2026-05-18, triggered by the Authelia/`127.0.0.1` recreate.)

**Fix (manual):** the files are transient runtime state — safe to delete (back up `/srv/qbittorrent/config/qBittorrent` first if paranoid):

```sh
ssh natto
rm -f /srv/qbittorrent/config/qBittorrent/ipc-socket \
      /srv/qbittorrent/config/qBittorrent/lockfile
# s6 respawns qbit-nox within ~1s; it now starts clean and binds 8080.
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/v2/app/version  # expect 200
```

No stack restart needed — just deleting the stale pair lets the next (already-looping) qbit-nox start clean. `deploy.sh qbittorrent` now self-heals this automatically: it waits for the WebUI to bind after `compose up`, and if it doesn't, clears the stale pair and restarts qBit once (and no longer blind-`sleep`s before `apply-tuning`).

**Not** the cause, ruled out on 2026-05-18: the qBittorrent 5.x split profile layout (`config/`+`data/` subdirs). The LSIO image sets `XDG_CONFIG_HOME=/config` `XDG_DATA_HOME=/config`, so qBit uses the **flat** `/config/qBittorrent/` profile (config `qBittorrent.conf`, `BT_backup/`, etc. directly there). The `/config/qBittorrent/{config,data}/` subdirs qBit auto-creates are unused noise; don't migrate into them.
