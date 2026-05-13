# Runbook: Integrating Proton VPN with qBittorrent

This runbook details the deployment of a containerized split-tunnel VPN using Gluetun and Proton VPN. The architecture ensures only qBittorrent traffic is routed through the VPN, preserving `natto`'s local ingress, Tailscale connectivity, and Pi-hole operations.

## Preconditions
- A Proton VPN Plus subscription.
- A WireGuard configuration file downloaded from the Proton VPN dashboard.
- **CRITICAL:** The WireGuard config must be generated with **Port Forwarding enabled** to allow inbound connections for private trackers.

## 1. Prepare secrets in the repository

`services/qbittorrent/secrets.env.example` is already committed (with `secrets.env` in `services/qbittorrent/.gitignore`):

```env
VPN_TYPE=wireguard
VPN_SERVICE_PROVIDER=protonvpn
WIREGUARD_PRIVATE_KEY=
WIREGUARD_ADDRESSES=
SERVER_COUNTRIES=Switzerland
VPN_PORT_FORWARDING=on
PORT_FORWARD_ONLY=on
```

`VPN_PORT_FORWARDING=on` is the toggle that actually requests a forwarded port from Proton — without it, no port will be forwarded. `PORT_FORWARD_ONLY=on` is a complementary server *filter* that restricts Gluetun to PF-capable servers, so reconnects don't land on a server that drops the forwarded port. Both should be on.

## 2. Provision secrets on natto

Provision the production secrets file using the `PrivateKey` and `Address` from the downloaded Proton WireGuard config. `SERVER_COUNTRIES` defaults to Switzerland; Netherlands is another privacy-friendly option.

```sh
ssh natto
sudo install -o root -g root -m 0600 /dev/null /srv/qbittorrent/secrets.env
sudoedit /srv/qbittorrent/secrets.env
# Populate from services/qbittorrent/secrets.env.example, filling in actual values.
```

## 3. docker-compose.yml

`services/qbittorrent/docker-compose.yml` is already updated to introduce the Gluetun sidecar. The WebUI port is published on the `gluetun` container, since qBittorrent uses `network_mode: service:gluetun` and cannot publish its own ports.

```yaml
services:
  gluetun:
    image: qmcgaw/gluetun:latest
    container_name: gluetun
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    ports:
      - "8080:8080"  # WebUI port exposed via Gluetun for Caddy ingress
    env_file:
      - path: secrets.env
        required: false
    environment:
      - TZ=America/New_York
    restart: unless-stopped

  qbittorrent:
    image: lscr.io/linuxserver/qbittorrent:latest
    container_name: qbittorrent
    network_mode: "service:gluetun"  # Routes all qBit traffic through the VPN sidecar
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/New_York
      - WEBUI_PORT=8080
    volumes:
      - ./config:/config
      - /mnt/media:/mnt/media  # Bound to media root to support Radarr/Sonarr hardlinking
    depends_on:
      - gluetun
    restart: unless-stopped
```

## 4. Deploy

Commit the repository changes, SSH into natto, and execute the standard deployment pattern:

```sh
ssh natto
cd /srv/nthncrtr-repo
git pull
sudo ./deploy.sh qbittorrent
```

`deploy.sh` creates `/mnt/media/_unsorted/torrents` (owned by `nthncrtr:nthncrtr`) so qBit has a save path ready post-cutover. The old `/srv/qbittorrent/downloads/` directory is no longer used and can be removed by hand once you've verified the new layout works.

## 5. Retrieve forwarded port

Once both containers are healthy, retrieve the dynamically assigned forwarded port. Two equivalent options:

```sh
# Canonical — Gluetun writes the port to a file:
docker exec gluetun cat /tmp/gluetun/forwarded_port

# Or via logs (best-effort grep):
docker logs gluetun 2>&1 | grep -iE "port.*forward"
```

Note the forwarded port number provided by Proton.

## 6. Configure qBittorrent (kill switch & port)

Log into the qBittorrent WebUI at `https://torrent.nthncrtr.com` and apply:

**Options → Downloads:**
- Default Save Path: `/mnt/media/_unsorted/torrents`

**Options → Connection:**
- "Port used for incoming connections": enter the forwarded port from step 5.
- Uncheck "Use UPnP / NAT-PMP port forwarding from my router".

**Options → Advanced:**
- Change "Network Interface" from `Any interface` to `tun0`. Gluetun normalizes both WireGuard and OpenVPN to the `tun0` name. This is defense-in-depth — the implicit kill switch already comes from `network_mode: service:gluetun` (no Gluetun = no network for qBit at all), but binding to `tun0` ensures qBit won't fall back to the Docker bridge interface if the netns is otherwise reachable.

## Known limitation: dynamic forwarded port

Proton's port-forwarding API issues a port that **changes** when Gluetun reconnects (e.g. after a Proton-side disconnect or a container restart). The port you set in step 6 will go stale on the next reconnect, and inbound peer connections will silently fail until you re-do step 5 + step 6.

Automating this loop — Gluetun control server API → qBittorrent WebUI API — is a follow-up worth its own mission. Until then, after any `docker compose restart gluetun` or extended Proton outage, expect to re-check the forwarded port.

## Verification

```sh
# Caddy ingress still works:
curl -fsSL -o /dev/null -w '%{http_code}\n' https://torrent.nthncrtr.com   # expect 200

# qBittorrent's egress IP is the VPN, not natto's:
docker exec qbittorrent sh -c 'wget -qO- https://api.ipify.org'           # should be a Proton-Swiss IP

# Forwarded port is set:
docker exec gluetun cat /tmp/gluetun/forwarded_port                        # should print a port number
```
