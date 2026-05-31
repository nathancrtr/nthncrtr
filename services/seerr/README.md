# Seerr

Request manager paired with [Jellyfin](../jellyfin/). Friends log in with
their Jellyfin credentials, search/browse, hit *Request* → Seerr forwards
the request to Sonarr or Radarr → the existing *arr+qBit pipeline grabs
it → Jellyfin serves it. Seerr itself never touches media files.

Upstream: <https://github.com/seerr-team/seerr> — successor to Jellyseerr
and Overseerr.

## Where things live

| | Path |
|---|---|
| Compose | `/srv/seerr/docker-compose.yml` |
| Config + SQLite DB | `/srv/seerr/config/` (owned by UID 1000) |
| Container | `seerr` |
| Image | `ghcr.io/seerr-team/seerr:latest` |
| Port (host) | `127.0.0.1:5055` (loopback only — see *Networking*) |
| Reachability | **Public** at `https://requests.nthncrtr.com` via the same Cloudflare Tunnel as Jellyfin (`services/cloudflared`). Inside clients use Caddy + Pi-hole split-horizon. |

## How "public, but only Jellyfin + Seerr" works

`requests.nthncrtr.com` is the second (and only other) internet-reachable
name, alongside `play.nthncrtr.com`. The containment is structural: the
Cloudflare Tunnel's `ingress:` list is the entire allowlist and everything
else 404s — adding a third public service is an explicit operator decision
(CLAUDE.md safety rule 8). The tunnel mechanics, the GFiber-dead-end backstory,
and the ingress block itself live in `services/cloudflared/README.md`.

**Inside path** mirrors Jellyfin's: a proxied Cloudflare CNAME
(`requests.nthncrtr.com → <tunnel-uuid>.cfargotunnel.com`, from
`cloudflared tunnel route dns play requests.nthncrtr.com`), a Pi-hole local
override (`192.168.1.50 requests.nthncrtr.com`) so inside clients hit Caddy
directly instead of hairpinning through Cloudflare (mechanism:
`services/pihole/README.md`), and an inside-only Caddy vhost
(`reverse_proxy 127.0.0.1:5055`, no `import authelia`).

## Auth — Jellyfin SSO, no Authelia

Seerr supports Jellyfin auth natively, so friends use the same account they
already have. **Do not** add `import authelia` to the Caddyfile block — Seerr's
native mobile app breaks under `forward_auth` (the shared reason, tabulated in
`services/authelia/README.md`).

The brute-force gate is a **Cloudflare WAF Rate-Limiting rule** (dashboard
state, not in repo — same class as the Jellyfin one): match URI path
`/api/v1/auth/jellyfin` method `POST`, ~5 req/min per IP → managed challenge.

## Networking inside Docker

| Target | Path | Why |
|---|---|---|
| Sonarr | `http://sonarr:8989` (arrnet) | Shared external network — same pattern as Sonarr↔Prowlarr. |
| Radarr | `http://radarr:7878` (arrnet) | Same. |
| Jellyfin | `http://host.docker.internal:8096` | Jellyfin is `network_mode: host` + listens on all addrs, so the bridge-gateway path resolves (unlike for the *arrs/qBit, which bind 127.0.0.1 only — safety rule 9 ¶2). |
| Outbound (TMDB, image CDNs) | default bridge | Normal egress. |

Seerr does **not** join `qbittorrent_default`: it talks to the *arrs,
which then talk to qBit.

## Why config on internal disk, not /mnt/media

Seerr's settings + request history are a local SQLite DB — service state, so it
lives on natto's SSD (`/srv/seerr/config/`) like every other service's DB, not
on the bulk media drive. See `runbooks/media-layout.md` § "Storage model". The
nightly `/srv` backup picks it up; no separate plumbing needed.

## First-run setup (interactive — not done by deploy.sh)

Browse to `http://natto:5055` (or `https://requests.nthncrtr.com` once
DNS + tunnel are in place) and complete the setup wizard:

1. **Sign in with Jellyfin.** Server URL: `http://host.docker.internal:8096`.
   Use the Jellyfin admin account. Seerr will then import all existing
   Jellyfin users — they can log in immediately with their Jellyfin
   credentials.
2. **Add Sonarr.** Server URL: `http://sonarr:8989`. API key from
   Sonarr → Settings → General → Security (also in `secrets.env`-class
   notes per existing *arr READMEs). Pick the *Default Quality Profile*
   and root folder `/mnt/media/video/tv`. Test connection.
3. **Add Radarr.** Same pattern: `http://radarr:7878`, API key, root
   folder `/mnt/media/video/movies`.
4. **Request approval policy.** Default leaves request approval as an
   admin step — keep this default unless an explicit operator decision
   changes it. (Per-user auto-approve quotas can be granted later from
   Users → ⋯ → Edit User → Permissions.)

## Operating

```sh
# Restart (no DNS impact, no confirmation needed — not Pi-hole):
cd /srv/seerr && docker compose restart

# Logs:
docker logs -f seerr

# Update to a newer image:
cd /srv/seerr && docker compose pull && docker compose up -d
```

## Activation status

To be stood up alongside Jellyfin's existing tunnel (WORKLIST 6.7). The
Cloudflare DNS route, the Pi-hole local override, the Cloudflare WAF
rule, and the first-run wizard are operator actions; everything else is
covered by `deploy.sh seerr` + `deploy.sh cloudflared` + `deploy.sh
caddy`.

## Backup policy

`/srv/seerr/config/` (SQLite DB + settings + cached metadata) rides the
nightly `natto-*.tgz` tarball under `/srv`. The media itself is the 5TB
drive and is not duplicated by this service (Seerr is metadata only).

## Rollback (removing Seerr cleanly)

1. Remove the `requests.nthncrtr.com` ingress entry from
   `services/cloudflared/config.yml`; `sudo ./deploy.sh cloudflared`.
2. Delete the `requests.nthncrtr.com` block from
   `services/caddy/Caddyfile`; `sudo ./deploy.sh caddy`.
3. `cd /srv/seerr && docker compose down`. `/srv/seerr/config/` can be
   archived from the next backup tarball if state retention is desired,
   then `rm -rf`'d.
4. Cloudflare DNS: delete the `requests.nthncrtr.com` CNAME (dashboard).
5. Pi-hole: remove the `192.168.1.50 requests.nthncrtr.com` local-DNS
   record (UI).
6. Cloudflare WAF: delete the rate-limit rule on `/api/v1/auth/jellyfin`.

No live-service impact at any point — Seerr is purely additive.
