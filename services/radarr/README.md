# Radarr (stub — not yet deployed)

Movie collection manager. The Caddyfile route `radarr.nthncrtr.com` points at `natto:7878`, but no container is currently running there. The compose file in this directory is a ready-to-bring-up stub.

## Activating

```sh
ssh natto
# First-time manual steps — deploy.sh handles dirs idempotently on subsequent runs:
sudo mkdir -p /srv/radarr/config
sudo chown nthncrtr:nthncrtr /srv/radarr/config
sudo mkdir -p /mnt/media/video/movies
cd /srv/nthncrtr-repo && git pull && sudo ./deploy.sh radarr
```

Then visit `https://radarr.nthncrtr.com` and complete initial setup:

1. **Settings → Media Management → Root Folders**: add `/mnt/media/video/movies`
2. **Settings → Download Clients**: add qBittorrent (`host.docker.internal`, port `8080`, username `admin`, category `movies`)
   - **First**: in qBittorrent Settings → Downloads, change the default save path to `/mnt/media/downloads/complete/` so completed files land on the 5 TB drive rather than the SD card (see [Pi 4 B note](#pi-4-b-note) below).
3. **Settings → General → Security**: copy the API key. Then on natto:
   ```sh
   sudo -e /srv/homepage/secrets.env   # add HOMEPAGE_VAR_RADARR_KEY=<paste>
   cd /srv/nthncrtr-repo && sudo ./deploy.sh homepage
   ```

## Once deployed

| | Path |
|---|---|
| Compose | `/srv/radarr/docker-compose.yml` |
| Config / DB | `/srv/radarr/config/` |
| Movie library | `/mnt/media/video/movies/` |
| Container name | `radarr` |
| Image | `lscr.io/linuxserver/radarr:latest` |

Ports: `7878` (WebUI only — no peer port needed).

## Pi 4 B note

The SD card root filesystem (`/`) is only 15 GB. **Do not let qBittorrent write completed downloads to `/srv/qbittorrent/downloads/`** — large video files will fill the SD card quickly. Configure qBittorrent to save to `/mnt/media/downloads/complete/` (on the 5 TB drive). Radarr sees that path at `/mnt/media/downloads/complete/` inside its container (same bind mount) and can move files to `/mnt/media/video/movies/` — all on the same filesystem, no cross-device copy.

Radarr itself is capped at 512 MB RAM via `mem_limit` in the compose file. If Radarr's initial scan hits that limit it will be OOM-killed and Docker will restart it; this is intentional self-defence for the other services on this 2 GB host.
