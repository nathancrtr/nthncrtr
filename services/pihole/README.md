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

This is the canonical reference for split-horizon — the other service READMEs
(Jellyfin, Seerr, Navidrome) point here for the mechanism and list only their
own record.

**Why these exist.** A few `*.nthncrtr.com` names need to resolve to natto's
*LAN* IP for inside clients, instead of being forwarded upstream (Tailscale IP)
or hairpinning out to Cloudflare. Two cases: (a) LAN-only devices — smart TVs,
Chromecast receivers, anything that can't run Tailscale — can't route to a
`100.x` Tailscale address at all; (b) the two Cloudflare-tunnelled names
(`play`, `requests`) would otherwise round-trip through Cloudflare for traffic
that's local, adding latency and burning uplink. The override sends them to
Caddy on the LAN; SNI matches, so the existing Let's Encrypt cert still verifies.

Current records:

| Hostname | LAN IP | For |
|---|---|---|
| `music.nthncrtr.com` | `192.168.1.50` | Navidrome — LAN-only Subsonic/cast clients. (Was `natto.nthncrtr.com` until the 2026-05-17 rename — WORKLIST 7.1.) |
| `play.nthncrtr.com` | `192.168.1.240` | Jellyfin — keeps local 4k off the Cloudflare tunnel. |
| `requests.nthncrtr.com` | `192.168.1.50` | Seerr — same, alongside Jellyfin. |

(`.240` is natto's primary LAN IP, `.50` an equivalent alias — see CLAUDE.md;
new records should prefer `.240`, the `.50` ones are grandfathered.)

**Two places a record can live (Pi-hole v6 — know both).** This Pi-hole is
v6 (Core/FTL v6.x), which has *two* stores for a local A record:

- **`/etc/pihole/hosts/custom.list`** — what the web UI **Settings → Local
  DNS Records** writes (`IP<space>name` per line). Saving via the UI hot-reloads
  FTL automatically (no container restart, no DNS outage), and it can't clobber
  other records. **This is the preferred path.** `play.nthncrtr.com` lives here.
- **`dns.hosts = [ … ]` in `/etc/pihole/pihole.toml`** — the v6 settings file
  (also surfaced read/write in the same UI panel). The grandfathered
  `music`/`requests` rows live here. Hand-editing the toml needs a reload
  (`pihole reloaddns`) and risks clobbering the array — prefer the UI.

**Both are runtime state, not in this repo.** They ride the nightly `/srv`
backup (Pi-hole config is under `/srv/pihole`), so a *restore* brings them back,
but a from-scratch rebuild without a restore loses split-horizon (LAN streams
then hairpin through Cloudflare; LAN-only music clients break). Re-add via the
UI after any such rebuild. Verify a record from natto:

```sh
dig +short play.nthncrtr.com @127.0.0.1   # must be natto's LAN IP, NOT a Cloudflare edge IP
```

## Editing config — use the UI or CLI, never the file

`pihole.toml` (~70k, in `/srv/pihole/etc-pihole/`) is runtime-managed by pihole-FTL. **Do not edit it via `scp`/text editor.** A real incident: a host-side write raced FTL's own write and produced a zero-byte file, which FTL then read as "default config" — wiping upstream DNS, hosts, etc. Recovery required a container stop, file restore from a local backup, and container start.

Safe ways to change config:
- Web UI at `https://pi-hole.nthncrtr.com`
- `docker exec pihole pihole-FTL --config <key> '[ ...value... ]'` — takes the file lock properly

If FTL's writes silently produce a zero-byte `pihole.toml` and DNS quietly degrades, check disk space first: `ssh natto 'df -h /'`. natto's root filesystem has filled to 100% before (this was the Pi-era SD card; it's now the Beelink's 238 GB SSD, so far less likely — but FTL's serializer still doesn't surface ENOSPC, it just truncates). See CLAUDE.md § "check disk space first".

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
