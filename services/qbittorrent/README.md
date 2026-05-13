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

## Homepage widget

The qBittorrent widget in `services/homepage/config/services.yaml` is active. It uses the WebUI at `http://natto:8080`, which still works since 8080 is published on gluetun. Add `HOMEPAGE_VAR_QBITTORRENT_PASSWORD=<password>` to `/srv/homepage/secrets.env` (mode 0600) and run `sudo ./deploy.sh homepage` to pick up the secret.
