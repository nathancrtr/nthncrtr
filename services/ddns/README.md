# cloudflare-ddns

Keeps **`jellyfin.nthncrtr.com`** pointed at this home's current WAN IP.
Exists only because Jellyfin is the one public service (router port-forward —
see `services/jellyfin/README.md` and WORKLIST 6.6). One container, no
sidecar, no persistent state.

## Why this is needed

`jellyfin.nthncrtr.com` is the **only** `*.nthncrtr.com` record that points
at the home's public WAN IP instead of natto's Tailscale IP. Residential
ISPs rotate that WAN IP without warning. When it rotates:

- **External** Jellyfin access breaks until the A record catches up — this
  container fixes that within `UPDATE_CRON` (5 min).
- **Internal** access is unaffected: inside clients resolve
  `jellyfin.nthncrtr.com` via Pi-hole split-horizon (→ natto LAN IP), never
  via Cloudflare. So a stale record degrades remote-only, never the house.

Every other `*.nthncrtr.com` record is a Tailscale-IP A record and is **not
touched** by this — `DOMAINS` is pinned to the single Jellyfin name on
purpose. Do not widen it.

## Where things live

| | Path |
|---|---|
| Compose | `/srv/ddns/docker-compose.yml` |
| Secret | `/srv/ddns/secrets.env` (mode 0600, root:root) — **not** in the repo |
| Container | `cloudflare-ddns` |
| Image | `favonia/cloudflare-ddns:latest` |
| State | none (stateless; reads WAN IP, PATCHes the record, exits the loop iteration) |
| Reachability | n/a — outbound only, no published port |

## The Cloudflare token

`CLOUDFLARE_API_TOKEN` in `/srv/ddns/secrets.env`. Scope it to **Zone → DNS
→ Edit** on the **`nthncrtr.com` zone only** (see `secrets.env.example` for
the exact click-path). It is the only automation credential that can rewrite
DNS; a wider-scoped token here would be the highest-value secret on natto.

## How it picks the IP

`favonia/cloudflare-ddns` reads the public IP from Cloudflare's `1.1.1.1`
trace endpoint — it does **not** probe the router or use UPnP. `PROXIED=false`
is mandatory and deliberate: the public port is the non-standard `:8443`
(router remaps WAN 443 → natto 8443), Cloudflare's proxy only fronts
standard ports, and its TOS restricts proxied video streaming. We want a
plain **grey-cloud** A record and Caddy-terminated TLS.

## Operating

```sh
# Deploy / update (idempotent):
cd /srv/nthncrtr-repo && git pull && sudo ./deploy.sh ddns

# Did it update the record? (logs the WAN IP and every PATCH):
docker logs --since 30m cloudflare-ddns

# Force a check now instead of waiting for the 5-min cron:
docker restart cloudflare-ddns
```

Sanity-check the record actually tracks the WAN IP:

```sh
dig +short jellyfin.nthncrtr.com @1.1.1.1      # should equal the home WAN IP
curl -s https://1.1.1.1/cdn-cgi/trace | grep ip # the home WAN IP
```

## Pinning the image

`:latest` matches the rest of this repo's convention. If you want
reproducibility, pin to a released tag (e.g. `favonia/cloudflare-ddns:1.x.x`)
in the compose and `deploy.sh ddns` to roll it.

## Activation status

Scaffolded with the Jellyfin public-exposure work (WORKLIST 6.6). Comes up
via `bootstrap/natto.sh` + `deploy.sh ddns` once `/srv/ddns/secrets.env` is
provisioned. Until the token is in place the container starts, logs an auth
error, and changes nothing — safe to deploy ahead of the secret.
