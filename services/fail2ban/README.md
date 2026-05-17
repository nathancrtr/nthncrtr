# fail2ban

Brute-force protection for the **public Jellyfin login**. Exists only
because `play.nthncrtr.com` is internet-exposed with **no Authelia** in
front (forward_auth breaks Jellyfin's native clients — WORKLIST 6.4/6.6), so
Jellyfin's own login is the sole credential gate and its built-in lockout is
weak. One `lscr.io/linuxserver/fail2ban` container.

## REQUIRED operator step — do this or the jail is useless

Jellyfin must log the **real** attacker IP, not Caddy's `127.0.0.1`. Set it
once in the Jellyfin UI:

> **Dashboard → Networking → Known proxies → add `127.0.0.1`**, save,
> restart Jellyfin.

Caddy already forwards `X-Forwarded-For`; this makes Jellyfin trust it and
write the real client IP into the auth log. **Without this, every failed
login is logged as `127.0.0.1`** and fail2ban will (at best) ban the proxy.
Verify after a deliberate bad login from outside:

```sh
docker exec jellyfin sh -c "grep -h 'has been denied' /config/log/*.log | tail -1"
# the (IP: ...) must be the real client address, NOT 127.0.0.1
```

## How it works

| | |
|---|---|
| Compose | `/srv/fail2ban/docker-compose.yml` |
| Container | `fail2ban` (host network, `NET_ADMIN`+`NET_RAW`, runs as root) |
| Image | `lscr.io/linuxserver/fail2ban:latest` |
| Watches | `/srv/jellyfin/config/log/*.log` → `/remotelogs/jellyfin` (ro) |
| Rules (version-controlled) | `config/fail2ban/{filter.d,jail.d}/jellyfin.conf` |
| Runtime state (NOT in repo) | `config/fail2ban.sqlite3`, generated `jail.local`, logs — see `.gitignore` |
| Policy | 5 fails / 10 min → 24 h ban, escalating to 14 d for repeat offenders |

**Why host network + root:** fail2ban must write the *host's* netfilter
rules to drop attacker packets. The public attack path is
`WAN 443 → router → natto:8443 → native Caddy` (a host process, **not** a
Docker-published port), so a ban in the host `INPUT` chain stops the
attacker before Caddy sees them. This is exactly why the public listener is
native-Caddy `:8443` and not a Docker-published port — it keeps the ban
path simple (no `DOCKER-USER` chain needed).

deploy.sh pushes only the two `jellyfin.conf` rule files (it does **not**
rsync the whole config dir — that would clobber the container's
`fail2ban.sqlite3` and generated `jail.local`) and reloads fail2ban.

## Operating

```sh
cd /srv/nthncrtr-repo && git pull && sudo ./deploy.sh fail2ban

docker exec fail2ban fail2ban-client status jellyfin   # jail health + banned IPs
docker exec fail2ban fail2ban-client reload jellyfin    # re-read rules
docker exec fail2ban fail2ban-client set jellyfin unbanip 1.2.3.4   # free a friend
docker logs --since 1h fail2ban                         # what it's doing
```

Test it end to end: from *outside* the LAN, fail the Jellyfin login 5×,
then `fail2ban-client status jellyfin` should list your IP and the login
page should stop responding to you (only).

## If Jellyfin changes the log wording

The filter matches the 10.8 and 10.9 phrasings. If a future Jellyfin breaks
it, find the new line and update `filter.d/jellyfin.conf`:

```sh
docker exec jellyfin sh -c "grep -h 'denied' /config/log/*.log | tail"
```

## Brute-force defense, layer 2 (follow-up, not deployed)

This natto Caddy build has **no** `rate_limit` module. A defense-in-depth
follow-up is to add `github.com/mholt/caddy-ratelimit` to
`services/caddy/build.sh`, rebuild, and redeploy the binary — rate-limiting
`/Users/AuthenticateByName` at the edge so attempts never reach Jellyfin.
Tracked in WORKLIST 6.6; fail2ban is the shipped layer-1 defense.

## Activation status

Scaffolded with the Jellyfin public-exposure work (WORKLIST 6.6). Comes up
via `bootstrap/natto.sh` + `deploy.sh fail2ban`. Safe to deploy before the
Jellyfin "Known proxies" step — it just won't ban usefully until that's set.
