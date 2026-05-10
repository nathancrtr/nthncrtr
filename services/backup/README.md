# natto backup

Daily snapshot of natto's stateful bits to a dated tarball on the 5TB drive.

## Files

| File | Deployed to | Purpose |
|---|---|---|
| `backup.sh` | `/usr/local/sbin/natto-backup` | The script |
| `natto-backup.service` | `/etc/systemd/system/natto-backup.service` | oneshot wrapper |
| `natto-backup.timer` | `/etc/systemd/system/natto-backup.timer` | Daily at 03:30 + 15-min jitter, `Persistent=true` |

`bootstrap/natto.sh` `step_backup` installs all three and enables the timer.

## What gets backed up

- `/srv/` — every docker service's config + data (Pi-hole, Navidrome, Homepage, eventually qBittorrent)
- `/usr/local/bin/caddy` — the built binary
- `/etc/caddy/Caddyfile` — routing config
- `/etc/caddy/caddy.env` — Cloudflare API token (the tarball stays on the local-only drive, so this is acceptable)
- `/etc/systemd/system/caddy.service` — Caddy systemd unit

NOT backed up: `/mnt/media/music/` (the music itself — too large; the 5TB drive IS its only storage).

## Where it goes

`/mnt/media/backups/natto-YYYY-MM-DD.tgz`. Atomic write via `.partial` rename so a partial archive never appears at the dated path.

`tar -P` is used so paths in the archive are absolute — restore is just `tar -xzf … -C /`.

## Operating

```sh
# Trigger a backup now:
sudo systemctl start natto-backup.service

# Watch the log:
journalctl -u natto-backup.service -f

# See when the next run is scheduled:
systemctl list-timers natto-backup.timer

# List available backups:
ls -lh /mnt/media/backups/
```

## Failure modes (script exits non-zero)

- Not run as root.
- Any source path missing.
- `/mnt/media/backups/` missing or unwritable.
- Free space less than source-set-size + 10%.

The systemd unit logs to journal with `SyslogIdentifier=natto-backup`; failed runs surface via `systemctl --failed`.

## Retention

None automated — old tarballs accumulate forever. Periodically `ls /mnt/media/backups/ | sort | head -n -7 | xargs -d'\n' rm --` to keep the last 7 (or set up a `find -mtime +N -delete` if you want the timer to do it).
