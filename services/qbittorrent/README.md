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
- **Options → Connection**: uncheck "Use UPnP / NAT-PMP port forwarding from my router". (The listening port itself is managed by the port-updater sidecar — don't set it manually.)
- **Options → Advanced → Network Interface**: `tun0`. Gluetun normalizes the WireGuard interface to `tun0`, same name as OpenVPN — qBit will refuse to bind if Gluetun's tunnel isn't up.
- **Options → Web UI → Authentication**: enable "Bypass authentication for clients on localhost". This is required for the port-updater sidecar (it runs in gluetun's netns, so it reaches qBit as 127.0.0.1 and would otherwise be rejected). Connections from outside the netns — natto's host processes, other Docker containers, your browser — still arrive over the Docker bridge and continue to need a password.

## Port-updater sidecar

A small `qbit-port-updater` container runs alongside gluetun in the same network namespace. It watches `/tmp/gluetun/forwarded_port` (gluetun's running record of the current Proton-assigned port) and pushes any change to qBit's `setPreferences` API. The script is `services/qbittorrent/port-updater.sh`, installed to `/srv/qbittorrent/port-updater.sh` and bind-mounted into the container — edit it on natto with `sudoedit` and `docker compose restart qbit-port-updater` to iterate.

**Debugging:**

```sh
docker logs qbit-port-updater                                # see what it's done
docker exec gluetun cat /tmp/gluetun/forwarded_port          # current Proton port
docker exec qbit-port-updater cat /state/forwarded_port      # same file, from sidecar's view
```

A common failure mode: `WARN: failed to push port` — usually means qBit's localhost-bypass setting wasn't enabled. Re-check the Web UI Authentication option above.

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

## Homepage widget

The qBittorrent widget in `services/homepage/config/services.yaml` is active. It uses the WebUI at `http://natto:8080`, which still works since 8080 is published on gluetun. Add `HOMEPAGE_VAR_QBITTORRENT_PASSWORD=<password>` to `/srv/homepage/secrets.env` (mode 0600) and run `sudo ./deploy.sh homepage` to pick up the secret.
