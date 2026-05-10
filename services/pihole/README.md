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

## Local DNS records (split-horizon)

| Hostname | LAN IP | Why |
|---|---|---|
| `natto.nthncrtr.com` | `192.168.1.50` | LAN-only devices (smart TV, Chromecast receivers, anything that can't run Tailscale) can't route to natto's Tailscale IP. With this override they reach Caddy on the LAN; SNI matches so the existing Let's Encrypt cert still verifies. |

Stored in `pihole.toml` under `[dns].hosts`; visible in the web UI at **Settings → DNS → Local DNS Records**.

## Editing config — use the UI or CLI, never the file

`pihole.toml` (~70k, in `/srv/pihole/etc-pihole/`) is runtime-managed by pihole-FTL. **Do not edit it via `scp`/text editor.** A real incident: a host-side write raced FTL's own write and produced a zero-byte file, which FTL then read as "default config" — wiping upstream DNS, hosts, etc. Recovery required a container stop, file restore from a local backup, and container start.

Safe ways to change config:
- Web UI at `https://pi-hole.nthncrtr.com`
- `docker exec pihole pihole-FTL --config <key> '[ ...value... ]'` — takes the file lock properly

If FTL's writes silently produce a zero-byte `pihole.toml` and DNS quietly degrades, check disk space first: `ssh natto 'df -h /'`. The SD card has filled to 100% before, and FTL's serializer doesn't surface ENOSPC — it just truncates.

If config is ever lost with no backup, at minimum re-enter:
- `dns.upstreams = [ "8.8.8.8", "8.8.4.4" ]`
- The Local DNS Records table above

## Admin password

The web UI password is set inside the container, not via env var. If lost:

```sh
docker exec -it pihole pihole -a -p
```

## Backup

`services/backup/backup.sh` includes `/srv/` so etc-pihole and etc-dnsmasq.d ride along automatically. Restore = extract the tarball and Pi-hole picks up where it left off.
