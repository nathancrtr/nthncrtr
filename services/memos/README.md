# Memos

Lightweight, self-hosted note-taking / quick-capture — the "what's on your
mind?" box with a feed of recent notes below it. One container (Go server +
bundled web UI + embedded SQLite); no external database. Markdown-native,
MIT-licensed. This is the simplest service in the fleet.

## Where things live

| | Path |
|---|---|
| Compose | `/srv/memos/docker-compose.yml` |
| Data (SQLite DB + uploaded resources) | `/srv/memos/data/` |
| Container | `memos` |
| Image | `neosmemo/memos:0.29.0` (**pinned** — see § Upgrading) |
| Host port | `5230` → container `5230` (published on `0.0.0.0`) |
| Reachability | **Tailnet-only** — `https://notes.nthncrtr.com` (Caddy), `http://natto.tailaf7ea6.ts.net:5230`, `http://natto:5230` |

### Why internal disk, not the 5TB

Memos' embedded SQLite DB needs POSIX locking and atomic renames. The 5TB
drive is exfat by design and gives none of that, so the data dir lives on
the Beelink's internal ext4 (`/srv`). Same resolved reasoning as
Nextcloud/Immich/Jellyfin. The footprint is tiny (text notes), so there's no
capacity concern like Immich's.

### Why no Authelia (and why it's still safe)

`notes.nthncrtr.com` deliberately has **no `import authelia`** on its Caddy
vhost. Memos ships native mobile apps, and `forward_auth` breaks native
clients — the same reason Jellyfin, Seerr, and Immich are exempt (WORKLIST
6.4/6.6). The barrier is instead:

- **Tailnet-only reach** — the name resolves (via Cloudflare DNS) to natto's
  Tailscale IP `100.122.71.33`, so it's only reachable from devices on the
  tailnet. It is **not** on the Cloudflare Tunnel, so safety rule 8 holds
  (Jellyfin + Seerr remain the only internet-exposed services).
- **Memos' own per-user accounts.**

Because there's no Authelia, the `0.0.0.0:5230` publish is **not** an
unauthenticated open door (Memos has its own login) — this is the
Nextcloud/Immich model, not the *arrs' `127.0.0.1` + Authelia model (safety
rule 9). Don't put `notes.nthncrtr.com` behind Authelia.

## Deploy

```sh
ssh -t natto
cd /srv/nthncrtr-repo && git pull
sudo ./deploy.sh memos caddy homepage
```

`deploy_memos` creates `/srv/memos/data`, installs the compose file, brings
the container up, and probes `http://127.0.0.1:5230/healthz`. `caddy`
publishes the `notes.nthncrtr.com` vhost; `homepage` refreshes the dashboard
tile.

### Operator one-time steps (outside the repo)

1. **Cloudflare A record** — add `A / notes / 100.122.71.33`, proxy status
   **DNS only** (grey cloud). Until this exists the name does not resolve,
   even though Caddy serves the vhost. Avoid querying `notes.nthncrtr.com`
   *before* adding the record — Pi-hole negative-caches the NXDOMAIN for
   ~30 min (CLAUDE.md § new-subdomain gotcha). No Pi-hole local override is
   needed: tailnet-only names forward upstream to the Tailscale IP fine.
2. **Create your user** — open `https://notes.nthncrtr.com`, the first
   sign-up becomes the Host (admin) account.
3. **Mint a widget access token** — in Memos: avatar → **Settings** →
   **Access Tokens** → **Create**. Copy it into
   `/srv/homepage/secrets.env` as `HOMEPAGE_VAR_MEMOS_TOKEN=…` (mode 0600),
   then re-run `sudo ./deploy.sh homepage`.

## The Homepage widget

The dashboard tile does both halves of "add + view recent":

- **Add a note** → the tile's `href` opens Memos itself, whose entire UI is
  a one-box quick-capture. (Homepage has no persistent storage and can't host
  a text input, so inline note creation isn't possible — this is the closest
  one-click path.)
- **View recent snippets** → a `customapi` widget (`display: dynamic-list`)
  calls `GET /api/v1/memos` with the Bearer token and lists the most recent
  notes' `content` + `displayTime`.

### customapi version caveat (read before upgrading)

The widget mapping in `services/homepage/config/services.yaml` assumes the
Memos **v0.29** API: `GET /api/v1/memos` → `{"memos": [{ "content": …,
"displayTime": … }]}`. Memos has reshaped this endpoint across releases
(e.g. the v0.22 gRPC-gateway rewrite, field renames). If you bump the pinned
image and the recent-notes tile goes blank, re-check the live response:

```sh
curl -s -H "Authorization: Bearer <token>" http://natto:5230/api/v1/memos | head
```

and adjust the `items` / `name` / `label` mappings to match.

## Upgrading

The image is pinned in `docker-compose.yml` (`neosmemo/memos:0.29.0`) on
purpose. To upgrade: bump the tag, `sudo ./deploy.sh memos`, then verify the
Homepage tile still renders (see the caveat above). Memos' release notes
occasionally call for a data backup before a DB-migrating release — back up
`/srv/memos/data` first if so.

## Backups

Not yet wired into `services/backup`. `/srv/memos/data` is small (text +
small resource uploads) and would slot into the nightly tarball naturally;
tracked as a follow-up in the WORKLIST entry.
