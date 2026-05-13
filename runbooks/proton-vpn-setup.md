# Runbook: Integrating Proton VPN with qBittorrent

This runbook details the deployment of a containerized split-tunnel VPN using Gluetun and Proton VPN. The architecture ensures only qBittorrent traffic is routed through the VPN, preserving `natto`'s local ingress, Tailscale connectivity, and Pi-hole operations. A small port-updater sidecar keeps qBit's listening port in sync with the dynamic port Proton assigns on each reconnect.

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

`services/qbittorrent/docker-compose.yml` defines three services: `gluetun` (Proton VPN tunnel), `qbittorrent` (sharing gluetun's netns), and `qbit-port-updater` (a tiny sidecar that watches gluetun's forwarded-port file and pushes changes into qBit's WebUI API). The WebUI port 8080 is published on the `gluetun` container, since qBittorrent uses `network_mode: service:gluetun` and cannot publish its own ports.

The `./gluetun-state` bind mount on gluetun (`:/tmp/gluetun`) is shared read-only with the updater (`:/state`), so it can read `forwarded_port` without `docker exec` or the Gluetun control server.

```yaml
services:
  gluetun:
    image: qmcgaw/gluetun:latest
    cap_add: [NET_ADMIN]
    devices: ["/dev/net/tun:/dev/net/tun"]
    ports: ["8080:8080"]
    volumes:
      - ./gluetun-state:/tmp/gluetun  # shared with qbit-port-updater
    env_file: [{ path: secrets.env, required: false }]
    environment: [TZ=America/New_York]
    restart: unless-stopped

  qbittorrent:
    image: lscr.io/linuxserver/qbittorrent:latest
    network_mode: "service:gluetun"
    environment: [PUID=1000, PGID=1000, TZ=America/New_York, WEBUI_PORT=8080]
    volumes:
      - ./config:/config
      - /mnt/media:/mnt/media   # for Radarr/Sonarr hardlinking
    depends_on: [gluetun]
    restart: unless-stopped

  qbit-port-updater:
    image: curlimages/curl:latest
    network_mode: "service:gluetun"
    volumes:
      - ./gluetun-state:/state:ro
      - ./port-updater.sh:/port-updater.sh:ro
    entrypoint: ["/bin/sh", "/port-updater.sh"]
    environment: [POLL_INTERVAL=60, TZ=America/New_York]
    depends_on: [gluetun, qbittorrent]
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

## 5. Configure qBittorrent (one-time)

Log into the qBittorrent WebUI at `https://torrent.nthncrtr.com` and apply:

**Options → Downloads:**
- Default Save Path: `/mnt/media/_unsorted/torrents`.

**Options → Connection:**
- Uncheck "Use UPnP / NAT-PMP port forwarding from my router".
- *Do not set the listening port manually.* The port-updater sidecar manages it from gluetun's `forwarded_port` file.

**Options → Advanced:**
- Change "Network Interface" from `Any interface` to `tun0`. Gluetun normalizes both WireGuard and OpenVPN to the `tun0` name. This is defense-in-depth — the implicit kill switch already comes from `network_mode: service:gluetun` (no Gluetun = no network for qBit at all), but binding to `tun0` ensures qBit won't fall back to the Docker bridge interface if the netns is otherwise reachable.

**Options → Web UI → Authentication:**
- Enable "Bypass authentication for clients on localhost". This is required for the port-updater sidecar — it runs in gluetun's netns, so it appears to qBit as 127.0.0.1 and would otherwise be rejected. Connections from outside the netns (natto's host, your browser, other containers) still arrive via the Docker bridge and continue to need a password.

## 6. Verify the port-updater

```sh
# Port-updater logs should show it pushing the current port:
docker logs qbit-port-updater | tail -10
# Expect a line like:  ... qbit-port-updater: pushed port 36014 to qBittorrent

# Gluetun's view of the current forwarded port (this is what the updater reads):
docker exec gluetun cat /tmp/gluetun/forwarded_port

# qBit's view of its current listening port (sanity check — should match):
curl -fsS https://torrent.nthncrtr.com/api/v2/app/preferences \
  --cookie "SID=<browser session cookie>" | jq .listen_port
```

If the updater logs `WARN: failed to push port` repeatedly, the most likely cause is that **Options → Web UI → "Bypass authentication for clients on localhost"** is still unchecked.

## Verification

```sh
# Caddy ingress still works:
curl -fsSL -o /dev/null -w '%{http_code}\n' https://torrent.nthncrtr.com   # expect 200

# qBittorrent's egress IP is the VPN, not natto's:
docker exec qbittorrent sh -c 'wget -qO- https://api.ipify.org'           # should be a Proton-Swiss IP

# Forwarded port is set:
docker exec gluetun cat /tmp/gluetun/forwarded_port                        # should print a port number

# Port-updater pushed the port:
docker logs qbit-port-updater | tail -5                                    # expect "pushed port NNNNN"
```
