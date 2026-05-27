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

This is now the second `*.nthncrtr.com` name reachable from the internet
(alongside `play.nthncrtr.com`). The containment is structural — the
Cloudflare Tunnel's `ingress:` list in `services/cloudflared/config.yml`
is the allowlist:

```yaml
ingress:
  - hostname: play.nthncrtr.com      # Jellyfin
    service: http://localhost:8096
  - hostname: requests.nthncrtr.com  # Seerr
    service: http://localhost:5055
  - service: http_status:404          # everything else → 404
```

Nothing else on natto is reachable through the tunnel. Adding a third
public service is an explicit operator decision: append a hostname,
update CLAUDE.md safety rule 8, document it. Don't silently extend.

**Inside path** mirrors Jellyfin's:

- **Cloudflare CNAME** `requests.nthncrtr.com → <tunnel-uuid>.cfargotunnel.com`
  (proxied / orange-cloud — created by
  `cloudflared tunnel route dns play requests.nthncrtr.com`).
- **Pi-hole local-DNS override** `192.168.1.240 requests.nthncrtr.com`
  so inside clients hit Caddy directly instead of hairpinning through
  Cloudflare. (Add via Pi-hole UI → Settings → Local DNS Records — see
  the Jellyfin README's split-horizon section. `.240` is natto's primary
  LAN IP; the `.50` alias also works — see CLAUDE.md.)
- **Caddy** inside-only vhost in `services/caddy/Caddyfile` —
  `reverse_proxy 127.0.0.1:5055`, no `import authelia`.

## Auth — Jellyfin SSO, no Authelia

Seerr supports Jellyfin auth natively, so friends use the same account
they already have. **Do not** add `import authelia` to the Caddyfile
block: Seerr ships native mobile apps, and `forward_auth` breaks them
the same way it breaks Jellyfin/Immich (WORKLIST 6.4/6.6).

The brute-force gate is a **Cloudflare WAF Rate-Limiting rule** in the
Cloudflare dashboard (zone `nthncrtr.com` → Security → WAF → Rate
limiting rules), parallel to the existing Jellyfin one. Suggested shape:
match URI path `/api/v1/auth/jellyfin` method `POST`, ~5 req/min per IP →
managed challenge ~10 min. This is dashboard state, **not in this
repo** — same class as the Pi-hole local-DNS records and the Jellyfin
WAF rule.

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

Seerr stores settings + request history in a local SQLite DB at
`/app/config/db/`. SQLite needs POSIX locking + atomic renames — exfat
can give them on /mnt/media's 5TB, but the Beelink's internal ext4 is
the canonical store for live DB state across this repo (Jellyfin,
Nextcloud, Immich, the *arrs). Same reasoning. The nightly `/srv`
backup picks it up; no separate plumbing needed.

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
