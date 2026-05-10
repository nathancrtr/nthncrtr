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
| `bookmarks.yaml` | Tailscale admin, Cloudflare, repo link |
| `settings.yaml` | Provider stubs (weather APIs) |
| `widgets.yaml` | Top-bar widgets (CPU, memory, disk, search) |

## Secrets

Set up to keep API tokens out of git. `services.yaml` references each as `{{HOMEPAGE_VAR_*}}`; the values come from `/srv/homepage/secrets.env` via `env_file:` in the compose. See `secrets.env.example` for the variable list.

The compose uses `env_file: [{ path: secrets.env, required: false }]` so `docker compose config` works on workhorse where the file is intentionally absent.

## Adding a service to the dashboard

1. Add an entry to `services/homepage/config/services.yaml` under the appropriate host group.
2. If it's a docker container, set `server: my-docker` and `container: <name>`.
3. If it has a Homepage-supported widget, add a `widget:` block (see [Homepage docs](https://gethomepage.dev/configs/service-widgets/)).
4. If the widget needs an API token, add `HOMEPAGE_VAR_<NAME>=` to `secrets.env.example` (committed) AND to the operator's `/srv/homepage/secrets.env` (not).
5. `cd /srv/homepage && docker compose restart` (or `docker compose up -d` if the env_file changed).

## Backup

The whole config dir + secrets.env get included via `/srv/` in the daily tarball.
