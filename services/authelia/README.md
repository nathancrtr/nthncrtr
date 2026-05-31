# Authelia — single sign-on for the web-admin tier

One login replaces the per-app password-manager entries for **Sonarr,
Radarr, Prowlarr, qBittorrent and Homepage**. Caddy's `forward_auth` calls
Authelia before letting a browser through to any of those sites; a single
session cookie scoped to `.nthncrtr.com` covers all of them.

This is deliberately scoped to the **web-admin tier only** (operator
decision, optimising for convenience over a hard security boundary — the
services already sit behind Tailscale/Caddy).

### Canonical list of what's *not* behind Authelia (and why)

This table is the single source of truth for the no-Authelia decision; the
individual service READMEs point here rather than re-arguing it. The common
thread for the media/app services is the same one that keeps Jellyfin and
Seerr off Authelia even though they're internet-exposed: **`forward_auth`
issues a browser redirect to the login portal, which native mobile / TV /
sync clients can't follow** — it breaks the very apps that are the point of
the service. So they each carry their *own* per-user auth instead.

| Service | Why it's excluded |
|---|---|
| **Jellyfin** | Native TV/phone/Chromecast clients can't follow the auth redirect. Its own per-user accounts + a Cloudflare WAF rate-limit are the gate (CLAUDE.md safety rule 8). |
| **Seerr** | Native mobile app; same redirect problem. Uses Jellyfin-SSO (inherits Jellyfin's accounts) + a WAF rate-limit. |
| **Immich** | Native auto-backup mobile app breaks under `forward_auth`. Tailnet-only + Immich's own accounts. |
| **Navidrome** | Subsonic mobile clients authenticate per-app, not via browser SSO. Web UI left on Navidrome's own login for consistency with the apps. |
| **Nextcloud** | Tailnet-only; native sync/WebDAV clients would need its OIDC app, not forward-auth. |
| **Memos** | Native mobile app; tailnet-only + Memos' own accounts. |
| **Pi-hole** | Admin UI left on its own auth — unrelated to the media-admin credential set. |

(Could any of these be unified later via Authelia's *OIDC* provider mode
rather than forward-auth? Yes, in principle — out of scope here.)

Realistic end state: one Authelia entry in the password manager, plus the
per-app passwords/accounts the excluded services' native clients need. A real
reduction, not zero.

## Why Authelia (not Authentik / Pocket ID)

Single container, all config is YAML in git, file-based single-user backend,
SQLite storage, in-memory sessions — no Postgres/Redis. Authentik would be
four containers (server + worker + Postgres + Redis) against this repo's
minimalism; OIDC-only IdPs (Pocket ID/Kanidm) can't gate the *arrs or
qBittorrent without an extra forward-auth shim. Authelia does forward-auth
natively and can later also be an OIDC provider if Jellyfin/Nextcloud are
ever pulled in.

## One-time provisioning on natto

```sh
ssh natto

# 1. Secrets: three independent random strings.
sudo install -o root -g root -m 0600 /dev/null /srv/authelia/secrets.env
for _ in 1 2 3; do \
  docker run --rm authelia/authelia:4.39 \
    authelia crypto rand --length 64 --charset alphanumeric; done
sudoedit /srv/authelia/secrets.env   # paste, one per variable (see secrets.env.example)

# 2. User database: generate the argon2 hash (interactive, not echoed).
docker run --rm -it authelia/authelia:4.39 authelia crypto hash generate argon2
sudo install -o root -g root -m 0600 /dev/null /srv/authelia/users.yml
sudoedit /srv/authelia/users.yml     # from users.yml.example, paste the hash
```

`docker compose config` still works on workhorse without these files
(`secrets.env` is `required: false`; `users.yml` is only read by the
running container).

## DNS prerequisite

`auth.nthncrtr.com` is a **new subdomain** and this network uses one
Cloudflare record per host (no wildcard). Add an `auth` record pointing the
same way the other `*.nthncrtr.com` records do (Cloudflare DNS → natto's
Tailscale IP) **before** deploying Caddy, or the portal — and therefore
every gated site's login redirect — won't resolve.

## Deploy (order matters)

Authelia must be up *before* the Caddyfile that references it is reloaded,
or the gated sites 502:

```sh
ssh -t natto
cd /srv/nthncrtr-repo && git pull
sudo ./deploy.sh authelia      # bring the IdP up first
sudo ./deploy.sh caddy         # then flip the gate on (validates via caddy adapt)
```

`authelia` is opt-in — a bare `sudo ./deploy.sh` never touches it.

## App-side settings (do these or you get a double login)

Caddy is native on natto, so every proxied request reaches the backing app
from a *local* address regardless of where the real client is. Use that:

- **Sonarr / Radarr / Prowlarr** — Settings → General → Security →
  *Authentication Method* → **External** (no in-app login page; trust the
  proxy), *Authentication Required* → **"Disabled for Local Addresses"**. The
  API keys still guard `/api`. Result: Authelia is the only browser gate;
  Homepage widgets and Prowlarr↔*arr sync (API-key, local) keep working
  untouched. This is the app half of CLAUDE.md safety rule 9.
- **qBittorrent** — Web UI → Authentication → enable *"Bypass authentication
  for clients in whitelisted IP subnets"* and add the Docker bridge subnet
  Caddy connects from (alongside the existing localhost bypass the
  port-updater needs — see `services/qbittorrent/README.md`). Without this,
  qBit shows its own login *after* Authelia.

## Verifying

```sh
# Service liveness (portal is 127.0.0.1-only):
ssh natto 'curl -s -o /dev/null -w "%{http_code}\n" \
  http://127.0.0.1:9091/api/authz/forward-auth'      # → 401 (up, unauthenticated)

# End to end: open https://radarr.nthncrtr.com in a fresh browser →
# 302 to https://auth.nthncrtr.com → log in → land back on Radarr.
# Then https://sonarr.nthncrtr.com should NOT re-prompt (shared cookie).
```

## Files / paths

| | Path |
|---|---|
| Compose | `/srv/authelia/docker-compose.yml` |
| Config (deployed, not secret) | `/srv/authelia/configuration.yml` |
| User db (SECRET, 0600, not in repo) | `/srv/authelia/users.yml` |
| Secrets (0600, not in repo) | `/srv/authelia/secrets.env` |
| SQLite db + notifier file | `/srv/authelia/data/` |
| Container | `authelia` (authelia/authelia:4.39) |
| Portal | `https://auth.nthncrtr.com` (Caddy → 127.0.0.1:9091) |

## Raising to 2FA later

`configuration.yml`: set `access_control.default_policy: two_factor`, redeploy
authelia (no Caddy change). Users self-enrol TOTP on next login; enrolment
links land in `/srv/authelia/data/notification.txt` (filesystem notifier —
no SMTP in this setup). No redesign required.

## Rollback

Clean — the apps' own auth was only *local-disabled*, never deleted:

```sh
# Repo: revert the Caddyfile `import authelia` lines + portal/snippet.
sudo ./deploy.sh caddy            # caddy adapt validates; gate comes off
cd /srv/authelia && docker compose down
# Optionally re-enable each *arr's auth + remove the qBit subnet bypass.
```
`/srv/authelia/data` is disposable (only sessions + the empty user db state).
