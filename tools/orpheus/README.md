# Orpheus wishlist tools

Triage a [RateYourMusic](https://rateyourmusic.com) wishlist against
[Orpheus](https://orpheus.network) (OPS), then act on the result in either
direction:

- **what's *not* on OPS** → upload candidates (Bandcamp → OPS pipeline lives in [`upload/`](upload/README.md))
- **what *is* on OPS** → download candidates (`download_available.py`, below)

Both directions share the same front end: parse the wishlist, then check each
release against OPS once. That single availability pass is the rate-limited,
expensive part; everything else just consumes its CSV output.

## The flow

```
parse_wishlist.py      RYM wishlist HTML       ->  wishlist.csv
check_availability.py  wishlist.csv            ->  orpheus-available.csv   (on OPS, seeded FLAC)
                                                   orpheus-novel.csv       (not on OPS / no seeded FLAC -> upload)
                                                   orpheus-review.csv      (fuzzy match -> eyeball before acting)
download_available.py  orpheus-available.csv   ->  .torrents fetched + added to qBit (downloads into /mnt/media/music)
make_collection_links.py  available.csv + manifest  ->  rym-collection-links.html (click-through "In Collection" worklist)
```

### 1. Export and parse the wishlist

In a browser, open `https://rateyourmusic.com/collection_p/<user>/wishlist`
(scroll/expand to load every page you want), then **Save Page As → "Web Page,
HTML Only"**. Then:

```sh
cd tools/orpheus
.venv/bin/python parse_wishlist.py ~/Downloads/wishlist.html -o /tmp/wishlist.csv
```

Columns: `artist, artists, album, release_type, rym_id, rym_url`. See the
docstring for how it handles collabs, "credited name" decompositions, and DJ
mixes.

### 2. Check availability on OPS

```sh
.venv/bin/python check_availability.py --probe                 # verify OPS auth first
.venv/bin/python check_availability.py --csv /tmp/wishlist.csv --output-dir /tmp
```

Writes three CSVs to `--output-dir`:

| File | Meaning |
|---|---|
| `orpheus-available.csv` | confident match **and** a seeded FLAC torrent — carries `ops_torrent_id`. **The download list.** |
| `orpheus-novel.csv` | no group, or a group with no seeded FLAC — the upload list. |
| `orpheus-review.csv` | a group matched but below the fuzzy threshold (`--match-threshold`, default 0.85) — triage by hand. |

Throttled to one request per ~3 s (OPS silently empties responses near its
5-per-10 s cap). FLAC selection rubric (CD+log100 → CD → WEB → Vinyl → …, then
seeders) is in the `check_availability.py` docstring.

If after eyeballing `orpheus-review.csv` you decide some rows are real matches,
append them to `orpheus-available.csv` (same columns) before step 3.

### 3. Download what's available

```sh
.venv/bin/python download_available.py --probe                       # verify OPS auth
.venv/bin/python download_available.py --csv /tmp/orpheus-available.csv --dry-run
.venv/bin/python download_available.py --csv /tmp/orpheus-available.csv --use-tokens --limit 1   # one, end to end
.venv/bin/python download_available.py --csv /tmp/orpheus-available.csv --use-tokens             # the whole list
```

For each row it fetches the `.torrent` for `ops_torrent_id` (cached under
`--out`, default `./torrents/`, gitignored) and adds it to qBittorrent on natto,
downloading into `--savepath` (default `/mnt/media/music`, where Navidrome
scans) tagged `--category` (default `wishlist`). Use `--paused` to add stopped
and review in the qBit UI first, or `--fetch-only` to just pull the `.torrent`
files without touching qBit.

### Freeleech tokens (`--use-tokens`)

`--use-tokens` appends `usetoken=1` to each OPS download, so the torrent is
marked **freeleech for you** and its bytes don't count against your ratio. The
cost is **size-based, not one-per-torrent**: OPS charges roughly **one token per
320 MiB** (`ceil(size / 320 MiB)`), so a 2.5 GiB album costs ~8 tokens and a
sub-320 MiB single costs 1. Budget against your token balance accordingly — a
big batch can drain it fast. The token is applied server-side **at
`.torrent`-download time** and is idempotent per torrent (re-downloading the same
id doesn't spend more). Because the token is a server-side effect — not part of the
`.torrent` bytes — a torrent already cached from a non-token run is re-fetched
with `usetoken=1` so the token actually lands; the manifest tracks
`freeleech_token` per torrent so it's only spent once.

If a torrent **can't** take a token (over OPS's token size cap, or you've run
out), that row is **skipped, not silently grabbed on ratio** — the run reports
it so you can decide. Re-run those without `--use-tokens` to take them on ratio.

The summary prints `tokens-spent=N`. Caveat worth a one-time check: do a
`--use-tokens --limit 1` run first and confirm your token balance actually
dropped (OPS profile → tokens) and the torrent shows freeleech for you — that
proves OPS honors `usetoken` on the API download path before you spend across
the whole batch.

**How the qBit add works (no password):** the `.torrent` is piped over `ssh`
into `docker exec -i qbit-port-updater curl` POSTing to `127.0.0.1:8080`. That
container shares gluetun's network namespace, so qBit sees the request as
localhost and applies its auth-bypass — the same trick the upload pipeline's
`qbit_add.py` uses (see CLAUDE.md safety rule 9 on why the host ports are
127.0.0.1-only). It therefore needs your SSH agent loaded
(`ssh-add ~/.ssh/id_ed25519`); the script does a non-interactive preflight and
fails fast if not.

**Idempotency:** a cached `.torrent` is not re-fetched; qBit dedupes by
infohash; and `torrents/download-manifest.json` records per-torrent
fetched/added state, so re-runs are cheap and safe to Ctrl-C.

**Ratio:** this leeches a batch from a private tracker — mind your buffer.

### 4. Mark them "In Collection" on RYM

```sh
.venv/bin/python make_collection_links.py --csv /tmp/orpheus-available.csv     # only what's in qBit
.venv/bin/python make_collection_links.py --csv /tmp/orpheus-available.csv --all  # every available row
open /tmp/rym-collection-links.html
```

**RYM has no public API, and its ToS forbids automated/scripted access** (they
ban for it — reports of bans under one request/minute). So this does **not** POST
to RYM. It doesn't need to: the wishlist HTML already contains each album's exact
release-page URL, which `parse_wishlist.py` captures as `rym_url` and
`check_availability.py` forwards into `orpheus-available.csv`. The generator turns
those URLs into a checklist — you open each and set ownership to **In Collection**
by hand. A few minutes for a batch, zero account risk.

By default it lists only albums that actually landed in qBit (manifest
`qbit_added: true`); `--all` lists every available row. Output is an HTML
checklist whose ticks persist in the browser (localStorage), so you can work
through it across sessions; `--format md|txt` gives a plain list instead. (If you
generated `orpheus-available.csv` before the `rym_url` column existed, rows
without it fall back to a RYM *search* link — re-run `check_availability.py` to
capture the direct URLs.)

## Secrets

`tools/orpheus/secrets.env` (gitignored; see `secrets.env.example`):

```
OPS_API_KEY=...        # check_availability.py + download_available.py
OPS_ANNOUNCE_URL=...   # upload pipeline only
```

No qBit credentials — the add path is the localhost side-channel described above.
