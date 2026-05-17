# cloudflared — Cloudflare Tunnel (the public path for Jellyfin)

The **only** way `play.nthncrtr.com` is reachable from the internet. One
container, outbound-only, exposes exactly `play.nthncrtr.com → Jellyfin`
and nothing else on natto.

## Why a tunnel (the GFiber dead-end, short version)

Router port-forwarding was the original plan (WORKLIST 6.6) and is **dead
on GFiber**, proven the hard way:

- GFiber reserves inbound WAN **443** for its own management UI (answers it
  with a self-signed cert, never forwards it).
- GFiber's port-forward feature targets a **phantom device** — a MAC
  (`e4:5f:01:3a:e1:02`) that is *neither* of natto's NICs
  (`enp1s0 78:55:36:09:3e:b1`, `wlp2s0 50:31:23:b0:3c:92`); it never
  DHCP-learned natto because natto is statically configured.
- The only thing GFiber *will* expose is **DMZ = all ports**, which would
  put SSH, the Caddy admin API (`:2019`), Pi-hole, Nextcloud and every
  *arr on the open internet. Unacceptable; the whole design is "only
  Jellyfin."

A tunnel dials **out** to Cloudflare, so GFiber is irrelevant — and it
scopes to one hostname → one service by construction (better than the old
`:8443` Caddy trick).

## Architecture

| Path | Route |
|---|---|
| **Outside** | friend → `play.nthncrtr.com` → Cloudflare edge (proxied) → tunnel → `cloudflared` on natto → `http://localhost:8096` Jellyfin |
| **Inside** | LAN client → Pi-hole split-horizon → `natto:443` Caddy → Jellyfin (no Cloudflare round-trip; local 4k stays local) |

One `PublishedServerUrl = https://play.nthncrtr.com` (no port) works both
ways. Caddy is **not** in the public path anymore (Cloudflare provides the
edge cert); Caddy's `play` block + Pi-hole split-horizon are kept purely
for the inside path.

## Where things live

| | Path |
|---|---|
| Compose | `/srv/cloudflared/docker-compose.yml` |
| Ingress config (version-controlled) | `/srv/cloudflared/config.yml` |
| Tunnel credentials (**secret**, gitignored) | `/srv/cloudflared/credentials.json` (0600) |
| Container | `cloudflared` (host network, outbound only) |
| Image | `cloudflare/cloudflared:latest` |

## One-time operator setup (interactive — not done by deploy.sh)

`cloudflared tunnel login` needs a browser; do this from a machine that
has one, or run cloudflared locally. All commands authenticate against the
`nthncrtr.com` Cloudflare zone.

```sh
# 1. Authenticate (opens a browser; pick the nthncrtr.com zone).
cloudflared tunnel login

# 2. Create the tunnel (name it 'play'). Prints a TUNNEL UUID and writes
#    ~/.cloudflared/<UUID>.json (the credentials file).
cloudflared tunnel create play

# 3. Put the credentials on natto and lock them down:
scp ~/.cloudflared/<UUID>.json natto:/tmp/credentials.json
ssh -t natto 'sudo install -o root -g root -m 0600 /tmp/credentials.json \
  /srv/cloudflared/credentials.json && rm /tmp/credentials.json'

# 4. Fill the tunnel UUID into the DEPLOYED config (NOT the repo copy —
#    the repo keeps the placeholder so it never clobbers a live tunnel):
ssh -t natto "sudo sed -i 's/REPLACE_WITH_TUNNEL_UUID/<UUID>/' /srv/cloudflared/config.yml"

# 5. Create the proxied DNS route (this makes play.nthncrtr.com a CNAME to
#    the tunnel, orange-cloud — REQUIRED for a tunnel). Delete any old
#    grey-cloud `play` A record first (the decommissioned ddns left one).
cloudflared tunnel route dns play play.nthncrtr.com

# 6. Bring it up:
ssh -t natto 'cd /srv/nthncrtr-repo && git pull && sudo ./deploy.sh cloudflared'
```

`deploy.sh cloudflared` installs the compose, installs `config.yml` **only
if the deployed copy still has the placeholder** (so step 4 is never
clobbered), warns if `credentials.json` is missing, and brings the
container up.

## Operating

```sh
docker logs -f cloudflared                 # 'Registered tunnel connection' = healthy
docker exec cloudflared cloudflared tunnel info play   # edge connections
cloudflared tunnel list                    # from the operator machine
```

Health: `docker logs cloudflared` should show 4× `Registered tunnel
connection` (Cloudflare's 4 edge data centers). External check:
`https://play.nthncrtr.com` returns Jellyfin with a **Cloudflare** edge
cert (issuer = Google Trust Services / Let's Encrypt via Cloudflare, not
natto's Caddy LE cert — that's expected for the external path).

## Brute-force protection

Host fail2ban can't see attackers through a tunnel (they hit Cloudflare,
not natto). `services/fail2ban` is therefore configured with the
**Cloudflare API ban action** — it still watches Jellyfin's log and bans
per-IP with escalation, but enforces at Cloudflare's edge. See
`services/fail2ban/README.md`.

## Cloudflare ToS note

Cloudflare's ToS restricts proxying large amounts of video. For a handful
of trusted users at low volume this is pragmatically fine and widely done,
but it is a documented gray area. If Cloudflare ever throttles the zone,
the fallback is the VPS-relay option (WORKLIST 6.6). Keep usage modest;
set the Jellyfin **Internet streaming bitrate limit** (~10–15 Mbps,
Dashboard → Playback) — it also caps how much goes through Cloudflare.

## Activation status

Replaces the router-port-forward + `services/ddns` model (both removed in
the WORKLIST 6.6 pivot). Comes up via the operator setup above +
`deploy.sh cloudflared`. Nothing is public until the tunnel + DNS route
exist.
