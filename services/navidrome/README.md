# Navidrome

Music streaming server, scans music from the 5TB drive, serves a web UI + Subsonic API.

## Where things live

| | Path |
|---|---|
| Compose | `/srv/navidrome/docker-compose.yml` |
| Scan database | `/srv/navidrome/data/` (sqlite + cover-art cache) |
| Music library | `/mnt/media/music/` (read-only mount in container) |
| Container name | `navidrome-navidrome-1` |
| Image | `deluan/navidrome:latest` |
| Public URL | `https://music.nthncrtr.com` |
| Internal port | 4533 |

The container runs as `1000:1000` (the natto operator's UID/GID). `/mnt/media` is exfat, mounted with `uid=1000`, so file ownership inside the music dir matches.

## Operating

```sh
# Restart (no confirmation needed — DNS is unaffected):
cd /srv/navidrome && docker compose restart

# Trigger a rescan via the web UI: Settings → Music Folders → Quick scan
# Or full rescan: docker exec -it navidrome-navidrome-1 wget -qO- 'http://localhost:4533/api/scanner/scan?fullScan=true'
```

If `navidrome.db-wal` grows unbounded (real example: a disk-full incident blocked SQLite checkpoints, leaving a 1.4 GB WAL), the restart above forces a checkpoint and shrinks the WAL back to ~12K. Inspect with `ls -la /srv/navidrome/data/navidrome.db*`.

## Library layout

The bind mount is `/mnt/media/music:/music:ro`. Navidrome scans `/music` recursively. The library was reorganized in mission 4.3 — see [`runbooks/media-layout.md`](../../runbooks/media-layout.md) for the canonical layout (`/mnt/media/{music,video,backups,_unsorted/}`).

## Auth for the Homepage widget

Homepage's Navidrome widget needs the Subsonic-style auth: `user`, `token`, `salt` (where `token = md5(password + salt)`). Pulled from `/srv/homepage/secrets.env` as `HOMEPAGE_VAR_NAVIDROME_TOKEN` and `HOMEPAGE_VAR_NAVIDROME_SALT`.

## Password encryption

User passwords are encrypted at rest via `ND_PASSWORDENCRYPTIONKEY`, set in
`/srv/navidrome/secrets.env` (mode 0600, not in the repo — see
`secrets.env.example`; wired through compose `env_file`). Generate the key
with `openssl rand -hex 32`.

**Without this key set, Navidrome stores passwords in plaintext** — that
was the state until 2026-05-18 (WORKLIST 7.2).

**Navidrome does NOT auto-encrypt existing plaintext when you first set a
key** (learned the hard way — WORKLIST 7.2). Its boot routine is
*key-rotation only*: it decrypts each stored value with the **previous**
key and re-encrypts with the new one. Fed a raw-plaintext password it
logs `cipher: message authentication failed`, leaves the value plaintext,
and skips the migration. Login still works (there is a plaintext-compare
fallback) so this is silent — but at rest stays plaintext and the
user-update API 500s. The "already encrypted with current key" sentinel
is the `property` row **`PasswordsEncryptedKey`**; its absence is what
makes Navidrome retry (and re-fail) the migration every boot. The only
reliable plaintext→encrypted path is the scratch-instance transplant in
§ Recovery below.

**The key is not recoverable.** If `secrets.env` is lost or the key
changes, every stored password becomes undecryptable and all users are
locked out. It is captured in the nightly `/srv` backup tarball, so a
restore brings it back; a from-scratch natto rebuild *without* a backup
restore loses it (same class of runtime-secret caveat as `caddy.env`).
Note the encrypted DB and the key live in the *same* backup tarball — this
protects against a DB-only leak, not against loss of the whole backup.

### Forgotten / locked-out password recovery

No reset CLI exists. Method depends on whether a key is configured:

**No key configured (plaintext mode):** stop the container, write the
desired password as plaintext, start. Login's plaintext-compare accepts
it.

```sh
cd /srv/navidrome && docker compose stop
mkdir -p "_pwreset_bak_$(date +%s)" && cp -a data/navidrome.db* "_pwreset_bak_"*/
docker run --rm -v /srv/navidrome/data:/data --entrypoint sh \
  deluan/navidrome:latest -c \
  "sqlite3 /data/navidrome.db \"UPDATE user SET password='NEWPASS' WHERE user_name='USER';\""
docker compose up -d
```

**Key configured (this host):** a plaintext write will NOT work (login's
fallback masks it but the value stays plaintext and user-edit 500s). You
must mint a value Navidrome can decrypt with the live key. Don't hand-roll
the AES — let Navidrome encrypt it in a throwaway instance using the *same*
key, then transplant the ciphertext **and** the `PasswordsEncryptedKey`
property into the real DB:

```sh
cd /srv/navidrome && docker compose stop
mkdir -p "_pwreset_bak_$(date +%s)" && cp -a data/navidrome.db* "_pwreset_bak_"*/
rm -rf /tmp/nd-scratch && mkdir -p /tmp/nd-scratch/data /tmp/nd-scratch/music
docker run -d --name nd-scratch -p 127.0.0.1:4599:4533 \
  --env-file /srv/navidrome/secrets.env -e ND_DATAFOLDER=/data \
  -e ND_MUSICFOLDER=/music -v /tmp/nd-scratch/data:/data \
  -v /tmp/nd-scratch/music:/music:ro deluan/navidrome:latest
# wait for :4599/ping, then create the user with the desired password:
curl -fsS -X POST http://127.0.0.1:4599/auth/createAdmin \
  -H 'Content-Type: application/json' \
  -d '{"username":"USER","password":"NEWPASS"}'
docker stop nd-scratch
# transplant scratch's user.password + PasswordsEncryptedKey into the real DB
# (python3 + stdlib sqlite3; both DBs on the host while real is stopped):
python3 - <<'PY'
import sqlite3
s=sqlite3.connect("/tmp/nd-scratch/data/navidrome.db")
r=sqlite3.connect("/srv/navidrome/data/navidrome.db")
pw,=s.execute("SELECT password FROM user").fetchone()
prop,=s.execute("SELECT value FROM property WHERE id='PasswordsEncryptedKey'").fetchone()
r.execute("UPDATE user SET password=? WHERE user_name=?",(pw,"USER"))
r.execute("INSERT OR REPLACE INTO property(id,value) VALUES('PasswordsEncryptedKey',?)",(prop,))
r.commit()
PY
docker compose up -d
docker rm nd-scratch
docker run --rm -v /tmp:/t --entrypoint sh deluan/navidrome:latest -c 'rm -rf /t/nd-scratch'
```

Verify either way:

```sh
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://127.0.0.1:4533/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"USER","password":"NEWPASS"}'   # expect 200
```

## Last.fm scrobbling

Navidrome scrobbles to Last.fm natively. Two pieces:

1. **Server-side app credentials** (`ND_LASTFM_APIKEY` + `ND_LASTFM_SECRET`
   in `/srv/navidrome/secrets.env`). Obtained once by registering an app at
   <https://www.last.fm/api/account/create>; shared across all Navidrome
   users on this server. `ND_LASTFM_ENABLED` defaults to true so just
   populating the two vars + restarting is enough to expose the "Link"
   button in the UI.
2. **Per-user link** in Navidrome's profile UI: avatar → Settings → Last.fm
   → "Link". OAuth-authorizes that Navidrome user against a last.fm
   account; the binding lives in `navidrome.db`. Each operator-side user
   links separately.

Restart after populating the secrets:

```sh
cd /srv/navidrome && docker compose restart
```

If the "Link" button is missing after restart, check the container env
actually picked the vars up: `docker exec navidrome-navidrome-1 env | grep
ND_LASTFM`. Empty values mean `secrets.env` isn't being read (wrong path,
wrong mode, or compose was never restarted).

## Backup

The scan db (`/srv/navidrome/data/`) goes into the daily tarball. The music library itself does NOT — it's its own thing on the 5TB drive, too large to redundantly tar. The `_pwreset_bak_*` snapshot dirs from the recovery procedure are not pruned automatically — delete them once login is confirmed (they contain old credentials).
