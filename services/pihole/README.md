# Pi-hole

DNS sinkhole + ad blocker for the household. **Critical infrastructure — stopping it kills DNS for everyone, so always announce + confirm before any restart.**

## Where things live

| | Path |
|---|---|
| Compose | `/srv/pihole/docker-compose.yml` (matches this repo) |
| etc-pihole | `/srv/pihole/etc-pihole/` (root-owned by container) |
| etc-dnsmasq.d | `/srv/pihole/etc-dnsmasq.d/` |
| Container name | `pihole` |
| Image | `pihole/pihole:latest` |

## Ports

| Host port | Container port | Purpose |
|---|---|---|
| 53/tcp+udp | 53/tcp+udp | DNS (this is the load-bearing one) |
| 8053 | 80 | Web UI (Caddy proxies it as `pi-hole.nthncrtr.com`) |

`network_mode: bridge` — explicitly bridge, not host. Port 53 is bound on all host interfaces.

## Operating

```sh
# DNS sanity check from anywhere on the tailnet:
dig @natto.local example.com

# Restart (announce + confirm first!):
cd /srv/pihole && sudo docker compose restart pihole
```

## Admin password

The web UI password is set inside the container, not via env var. If lost:

```sh
docker exec -it pihole pihole -a -p
```

## Backup

`services/backup/backup.sh` includes `/srv/` so etc-pihole and etc-dnsmasq.d ride along automatically. Restore = extract the tarball and Pi-hole picks up where it left off.
