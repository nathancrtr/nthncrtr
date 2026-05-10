# qBittorrent (stub — not yet deployed)

Torrent client. The Caddyfile route `torrent.nthncrtr.com` already points at `natto:8080`, but no container is currently running there. The compose file in this directory is a ready-to-bring-up stub.

## Activating

```sh
ssh natto
sudo install -d -o nthncrtr -g nthncrtr -m 0755 /srv/qbittorrent/{config,downloads}
sudo cp /opt/nthncrtr/repo/services/qbittorrent/docker-compose.yml /srv/qbittorrent/
cd /srv/qbittorrent && docker compose up -d
docker logs qbittorrent | grep -i 'webui password'   # one-time temporary password
```

Then log in at `https://torrent.nthncrtr.com`, change the admin password, and (recommended) restrict the WebUI to LAN-only via Settings → Web UI.

## Once deployed

| | Path |
|---|---|
| Compose | `/srv/qbittorrent/docker-compose.yml` |
| Config | `/srv/qbittorrent/config/` |
| Downloads | `/srv/qbittorrent/downloads/` |
| Container name | `qbittorrent` |
| Image | `lscr.io/linuxserver/qbittorrent:latest` |

Ports: `8080` (WebUI) + `6881 tcp+udp` (peer port; open in firewall if you want to seed externally).

## Homepage widget

The qBittorrent block in `services/homepage/config/services.yaml` is currently link-only. After deploy, uncomment the widget block and add `HOMEPAGE_VAR_QBITTORRENT_PASSWORD=` to `/srv/homepage/secrets.env`, then restart Homepage.
