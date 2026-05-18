# Homepage

The dashboard at `home.nthncrtr.com`. Shows live container status (via the Docker socket), per-service widgets (Pi-hole queries blocked, Navidrome track count, etc.), and bookmarks.

## Where things live

| | Path |
|---|---|
| Compose | `/srv/homepage/docker-compose.yml` |
| Config dir (bind-mounted) | `/srv/homepage/config/` |
| Secrets file | `/srv/homepage/secrets.env` (mode `0600`, NOT in repo) |
| Container name | `homepage` |
| Image | `ghcr.io/gethomepage/homepage:latest` |
| Public URL | `https://home.nthncrtr.com` |
| Bound port | `127.0.0.1:3000` (Caddy proxies it) |

## Config files in `config/`

| File | Purpose |
|---|---|
| `services.yaml` | The grid: top-level groups by host (natto, starmaya), per-service entries with widgets |
| `docker.yaml` | Defines `my-docker:` pointing at `/var/run/docker.sock` (mounted ro) |
| `bookmarks.yaml` | Tailscale admin, Cloudflare, code repos |
| `settings.yaml` | Title, color, header style, background image, per-group layout, weather provider stubs |
| `widgets.yaml` | Top-bar widgets — System block (CPU/mem/`/`) and Media block (`/mnt/media`), plus search |
| `custom.css` | Auto-loaded by Homepage. "Workshop glass + terminal warmth": Inter prose, JetBrains Mono stats, amber accent, glass cards |

## Secrets

Set up to keep API tokens out of git. `services.yaml` references each as `{{HOMEPAGE_VAR_*}}`; the values come from `/srv/homepage/secrets.env` via `env_file:` in the compose. See `secrets.env.example` for the variable list.

The compose uses `env_file: [{ path: secrets.env, required: false }]` so `docker compose config` works on workhorse where the file is intentionally absent.

## Adding a service to the dashboard

1. Add an entry to `services/homepage/config/services.yaml` under the appropriate host group.
2. If it's a docker container, set `server: my-docker` and `container: <name>`.
3. If it has a Homepage-supported widget, add a `widget:` block (see [Homepage docs](https://gethomepage.dev/configs/service-widgets/)).
4. If the widget needs an API token, add `HOMEPAGE_VAR_<NAME>=` to `secrets.env.example` (committed) AND to the operator's `/srv/homepage/secrets.env` (not).
5. `cd /srv/homepage && docker compose restart` (or `docker compose up -d` if the env_file changed).

## How widgets reach their backends (two paths)

Not all widgets reach their service the same way, because not all services
publish their port the same way:

- **Pi-hole, Navidrome, Nextcloud, Jellyfin** publish on `0.0.0.0`, so they're
  reachable on the docker-bridge gateway. Their widgets use
  `http://host.docker.internal:<port>` (the `extra_hosts: host-gateway` entry).
- **Radarr, Sonarr, Prowlarr, qBittorrent** publish on `127.0.0.1` only
  (safety rule 9 — the Authelia-only path). `host.docker.internal` is the
  *gateway* IP, not loopback, so it **cannot** reach a loopback-only publish —
  this is what produced their "API Error" after the Authelia cutover. The fix:
  the homepage container joins those compose projects' default networks
  (`radarr_default`, `sonarr_default`, `prowlarr_default`,
  `qbittorrent_default`, declared `external: true` in the compose) and the
  widgets address the services **by container name on the in-container port**:
  `http://radarr:7878`, `http://sonarr:8989`, `http://prowlarr:9696`,
  `http://gluetun:8080` (qBit shares gluetun's netns, so the reachable name
  is `gluetun`). This bypasses the host publish entirely — no LAN exposure,
  the *arr API keys and qBit login still apply, Authelia/the human path is
  untouched, and the rule-9 compose files are not modified.

Consequence: **homepage must be deployed after the *arrs/qBittorrent** so
those external networks exist. `deploy.sh`'s default SERVICES order does this.
Steady-state re-deploys are order-independent (the nets persist with the
running containers); only a fully cold bootstrap cares.

## Backup

The whole config dir + secrets.env get included via `/srv/` in the daily tarball.
