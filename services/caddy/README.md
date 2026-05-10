# Caddy

Reverse proxy + ACME terminator for every `*.nthncrtr.com` URL. Runs natively on natto (not in Docker) so it can bind 443 and renew certs via the Cloudflare DNS-01 challenge.

## Files

| File | Deployed to | Purpose |
|---|---|---|
| `Caddyfile` | `/etc/caddy/Caddyfile` | Routing config |
| `caddy.service` | `/etc/systemd/system/caddy.service` | systemd unit (runs as `caddy:caddy`) |
| `build.sh` | n/a (operator runs it) | Reproduces the binary via `xcaddy` |

## Build

The binary at `/usr/local/bin/caddy` is custom-built because we need the Cloudflare DNS provider module (caddy-dns/cloudflare). `build.sh` pins `CADDY_VERSION` and `CLOUDFLARE_VERSION` and produces a binary identical to what's running. `bootstrap/natto.sh` parses `CADDY_VERSION` from this file to decide whether to rebuild.

## Secrets

`/etc/caddy/caddy.env` (mode `0600`, owner `caddy:caddy`) holds:

```
CF_API_TOKEN=<cloudflare token with edit access to the nthncrtr.com zone>
```

Not in this repo, not auto-restored by bootstrap; operator installs it manually. The systemd unit `EnvironmentFile=`s it.

## Operating

```sh
# Validate before reloading. Note: `caddy validate` tries to provision TLS,
# so it needs CF_API_TOKEN. For syntax-only checks from elsewhere, use:
cat services/caddy/Caddyfile | ssh natto 'caddy adapt --adapter caddyfile --config /dev/stdin'

# Apply a new Caddyfile (after validate passes):
sudo systemctl reload caddy
```

Adding a route: edit `Caddyfile`, validate, push, reload.

## Routes today

- `home.nthncrtr.com` → 127.0.0.1:3000 (Homepage)
- `natto.nthncrtr.com` → tailnet:4533 (Navidrome)
- `pi-hole.nthncrtr.com` → 127.0.0.1:8053 (Pi-hole)
- `torrent.nthncrtr.com` → tailnet:8080 (qBittorrent — stub, no container yet)
- `starmaya.nthncrtr.com` → tailnet:8080 (roaster-web on kvass)
- Catchall `:443` aborts (no implicit hosts)
