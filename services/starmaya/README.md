# starmaya — coffee roasting profiler

Two Node.js services that talk to a Behmor 1600+ via an Arduino+MAX31855 thermocouple rig. Runs on the workshop Pi (currently named `kvass`; canonical name is `starmaya`).

## Files

| File | Deployed to | Purpose |
|---|---|---|
| `roaster-daemon.service` | `/etc/systemd/system/roaster-daemon.service` | Owns `/dev/behmor-arduino`. `PrivateNetwork=yes`. |
| `roaster-web.service` | `/etc/systemd/system/roaster-web.service` | HTTP server on `:8080`. SQLite at `/var/lib/roaster/roasts.db`. Depends on the daemon. |
| `99-behmor-arduino.rules` | `/etc/udev/rules.d/99-behmor-arduino.rules` | Stable `/dev/behmor-arduino` symlink across Arduino vendor variants |

## Application code

The systemd units expect built artifacts at:

```
/opt/starmaya/packages/daemon/dist/main.js
/opt/starmaya/packages/server/dist/main.js
```

Source lives in a separate repo. This repo only owns the host plumbing.

## Operating

```sh
# From workhorse:
ssh kvass

# Status:
systemctl status roaster-daemon.service roaster-web.service
ls -l /dev/behmor-arduino   # exists when the Arduino is plugged in

# Restart:
sudo systemctl restart roaster-daemon.service roaster-web.service
```

## Bring-up on a fresh host

```sh
# Clone the repo to /opt/nthncrtr/repo (or wherever), then:
sudo bootstrap/starmaya.sh
```

Installs the `roaster` system user, creates `/var/lib/roaster/`, copies the udev rule (with reload), copies the systemd units (with daemon-reload). Does NOT install Node.js (the units pin `/usr/bin/node` ≥ v22 — operator's choice of source) and does NOT deploy the application code.

## Tailnet status

kvass is **not** on natto's tailnet today (mission 4.1 is blocked on this). To activate the `roast.nthncrtr.com` Caddyfile route, kvass needs to join the tailnet first.
