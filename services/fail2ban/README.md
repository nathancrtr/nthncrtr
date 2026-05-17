# fail2ban

Brute-force protection for the **public Jellyfin login**. Exists only
because `play.nthncrtr.com` is internet-exposed with **no Authelia** in
front (forward_auth breaks Jellyfin's native clients — WORKLIST 6.4/6.6), so
Jellyfin's own login is the sole credential gate and its built-in lockout is
weak. One `lscr.io/linuxserver/fail2ban` container.

## Bans are enforced at Cloudflare, not the host firewall

The public path is a **Cloudflare Tunnel** (`services/cloudflared`).
External attackers hit Cloudflare's edge; `cloudflared` then connects to
Jellyfin from **localhost**. Their packets never traverse natto, so a host
`iptables` ban would match nothing. The jail therefore uses the
**`cloudflare-token`** ban action: on a ban it adds the offender to a
Cloudflare **IP Access Rule** via the API, blocking them at the edge where
they actually are. Consequence: this container needs only *outbound* HTTPS
to the Cloudflare API — **no host network, no `NET_ADMIN`** (both removed;
they were for the dead host-firewall model from the router-forward era).

## REQUIRED operator steps — two of them, or the jail is useless

**1. Jellyfin must log the real attacker IP.** `cloudflared` connects from
localhost, so Jellyfin only records the true client IP if it trusts that
proxy:

> Jellyfin → **Dashboard → Networking → Known proxies → add `127.0.0.1`**,
> save, restart Jellyfin.

`cloudflared` forwards `X-Forwarded-For` / `CF-Connecting-IP`; this makes
Jellyfin write the real client IP into the auth log. Verify:

```sh
docker exec jellyfin sh -c "grep -h 'has been denied' /config/log/*.log | tail -1"
# the (IP: ...) must be the real client address, NOT a localhost IP
```

**2. Provide the Cloudflare API token** for the ban action. Copy the
template and fill it on natto:

```sh
# template is action.d/cloudflare-token.local.example in the repo
sudo install -o root -g root -m 0600 /dev/stdin \
  /srv/fail2ban/config/fail2ban/action.d/cloudflare-token.local <<'EOF'
[Init]
cftoken = <the API token>
cfzone  = <nthncrtr.com Zone ID>
EOF
docker restart fail2ban
```

The shipped action (`docker exec fail2ban cat
/etc/fail2ban/action.d/cloudflare-token.conf`) calls
`POST/GET .../zones/<cfzone>/firewall/access_rules/rules` with
`Authorization: Bearer <cftoken>`, so it needs **both**:

- **`cftoken`** — API token, scope **Zone → Firewall Services → Edit** on
  Specific zone `nthncrtr.com` (this is the "IP Access Rules" permission;
  *not* Account Filter Lists — a different API this action doesn't use).
- **`cfzone`** — the `nthncrtr.com` **Zone ID** (Cloudflare dashboard →
  nthncrtr.com → Overview → "Zone ID"). Not secret, but required by the
  action's URL.

Nothing else.
fail2ban auto-merges `*.local` over the stock `*.conf`, so the `.local`
only supplies the secret. It is gitignored (`*.local`); never commit it.

## How it works

| | |
|---|---|
| Compose | `/srv/fail2ban/docker-compose.yml` |
| Container | `fail2ban` (default bridge net, outbound-only, no caps) |
| Image | `lscr.io/linuxserver/fail2ban:latest` |
| Watches | `/srv/jellyfin/config/log/*.log` → `/remotelogs/jellyfin` (ro) |
| Rules (version-controlled) | `config/fail2ban/{filter.d,jail.d}/jellyfin.conf` |
| Secret (NOT in repo) | `config/fail2ban/action.d/cloudflare-token.local` (0600) |
| Runtime state (NOT in repo) | `config/fail2ban.sqlite3`, generated `jail.local`, logs — see `.gitignore` |
| Ban action | `cloudflare-token` → Cloudflare IP Access Rule (edge) |
| Policy | 5 fails / 10 min → 24 h ban, escalating to 14 d for repeat offenders |

deploy.sh pushes only the version-controlled rule files (never the whole
config dir — that would clobber `fail2ban.sqlite3` / generated `jail.local`
/ the `.local` secret) and reloads fail2ban.

## Operating

```sh
cd /srv/nthncrtr-repo && git pull && sudo ./deploy.sh fail2ban

docker exec fail2ban fail2ban-client status jellyfin              # jail + banned IPs
docker exec fail2ban fail2ban-client set jellyfin unbanip 1.2.3.4 # free a friend
docker logs --since 1h fail2ban                                    # what it's doing
```

End-to-end test: from *outside*, fail the Jellyfin login 5×; then
`fail2ban-client status jellyfin` lists your IP, **and** Cloudflare →
Security → WAF → Tools → IP Access Rules shows the block. The login page
should stop responding to you (only).

## If Jellyfin changes the log wording

The filter matches the 10.8/10.9 phrasings. If a future Jellyfin breaks it:

```sh
docker exec jellyfin sh -c "grep -h 'denied' /config/log/*.log | tail"
# then update filter.d/jellyfin.conf
```

## Brute-force defense, layer 2 (follow-up, not deployed)

Cloudflare Rate Limiting (a WAF rule on the Jellyfin login path) would be a
zero-maintenance always-on coarse layer in front of this per-IP jail.
Tracked in WORKLIST 6.6; fail2ban is the shipped layer-1 defense.

## Activation status

Re-homed to the Cloudflare-API ban action in the WORKLIST 6.6 pivot (the
router-forward + host-iptables model was abandoned — GFiber can't
port-forward). Comes up via `bootstrap/natto.sh` + `deploy.sh fail2ban`.
Safe to deploy before the token/Known-proxies steps — it just won't ban
usefully until both are done.
