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

## Post-cutover WebUI config

Options → Downloads → Default Save Path: `/mnt/media/_unsorted/torrents` (deploy.sh creates the directory, owned by nthncrtr:nthncrtr). The earlier `/srv/qbittorrent/downloads/` mount has been removed — qbit now sees `/mnt/media` directly so Radarr/Sonarr can hardlink final files instead of copying.

Options → Connection: set "Port used for incoming connections" to the forwarded port reported by Proton (see [the runbook](../../runbooks/proton-vpn-setup.md#5-retrieve-forwarded-port)). Uncheck "Use UPnP / NAT-PMP port forwarding from my router".

Options → Advanced: set Network Interface to `tun0`. (Gluetun normalizes the WireGuard interface to `tun0`, same name as OpenVPN — qBit will refuse to bind if Gluetun's tunnel isn't up.)

## Known limitation: dynamic forwarded port

Proton's port-forwarding API hands out a *dynamic* port that can change when Gluetun reconnects (e.g. after a Proton-side disconnect or a container restart). The manually-set qBit port will then go stale until you re-do the Connection step. Automating this via Gluetun's control server API + qBit's WebUI API is a future improvement.

## Files / paths

| | Path |
|---|---|
| Compose | `/srv/qbittorrent/docker-compose.yml` |
| qBit config | `/srv/qbittorrent/config/` |
| Secrets | `/srv/qbittorrent/secrets.env` (mode 0600, root:root, NOT in repo) |
| qBit data root | `/mnt/media/` (downloads land in `_unsorted/torrents/` by default) |
| Containers | `qbittorrent` (LSIO image), `gluetun` (qmcgaw/gluetun) |

Ports: `8080` (WebUI, published on the gluetun container). The torrent peer port is whatever Proton's port-forward assigns; it is reachable via the VPN's external IP, not via natto's IP, so no host-side publish is needed.

## Homepage widget

The qBittorrent widget in `services/homepage/config/services.yaml` is active. It uses the WebUI at `http://natto:8080`, which still works since 8080 is published on gluetun. Add `HOMEPAGE_VAR_QBITTORRENT_PASSWORD=<password>` to `/srv/homepage/secrets.env` (mode 0600) and run `sudo ./deploy.sh homepage` to pick up the secret.
