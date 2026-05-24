# OPS upload pipeline

End-to-end automation: take an album you bought from Bandcamp (or another lossless source) and turn it into a seeded torrent group on Orpheus, with the music landing on natto in the right place for both Navidrome and qBittorrent.

## TL;DR

Two ways to invoke. Pick whichever matches what's on disk.

**Scan mode** — point at a parent dir, the harness auto-groups format dirs by `(artist, album, year)` read from track tags, then walks each ready triplet one at a time prompting only for OPS tags:

```sh
cd tools/orpheus
.venv/bin/python upload/run_pipeline.py --scan ~/Downloads            # plan only
.venv/bin/python upload/run_pipeline.py --scan ~/Downloads --apply    # interactive run
```

**Explicit-paths mode** — pass all format dirs for one album in a single invocation:

```sh
.venv/bin/python upload/run_pipeline.py \
    --apply \
    --tags "indie.pop, indie.folk, indie.rock" \
    ~/Downloads/"A Weather - Cove (FLAC)" \
    ~/Downloads/"A Weather - Cove (mp3 320)" \
    ~/Downloads/"A Weather - Cove (mp3 v0)"
```

In both modes: the FLAC, if present, is uploaded to OPS first (creates the torrent group); the MP3 formats then attach to the same group. Default is dry-run; `--apply` is required to actually mutate. With `--apply`, the harness does a non-interactive `ssh natto` preflight up front and fails fast (pointing at `ssh-add`) so you don't lose work mid-pipeline.

## Pipeline stages

The harness `run_pipeline.py` invokes each stage in order. Each stage is a standalone script — useful for debugging when one fails.

| # | Script | What it does | Mutates |
|---|---|---|---|
| 1 | `album_inspect.py` | Reads tags across FLAC/MP3, detects format/encoding, flags issues | no |
| 2 | `normalize.py` | Strips Bandcamp `Visit ...` from COMMENT tags; renames dir to `Artist - Album (Year) [WEB FORMAT]` | local files |
| 3 | `transfer.py` | rsyncs FLAC dirs to `natto:/mnt/media/music/`; MP3 dirs to `natto:/mnt/media/seed-only/` | natto fs |
| 4 | `art_upload.py` | Uploads the cover sidecar to catbox.moe (looks for `cover.{jpg,jpeg,png,webp}` then `folder.{jpg,jpeg,png}`; or pass `--image-url` to use a manual URL) | catbox.moe |
| 5 | `make_torrent.py` | Builds `.torrent` files (pure-Python bencode + SHA1) with the OPS announce URL + private + source=OPS | local files |
| 6 | `ops_upload.py` | POSTs to `https://orpheus.network/ajax.php?action=upload`. New group on first format, add-to-group on subsequent. | OPS |
| 7 | `qbit_add.py` | Adds each `.torrent` to qBittorrent on natto via the `qbit-port-updater` sidecar's localhost-auth-bypass. | natto qBit |

## Idempotency

Each album-format dir has a manifest at `upload/state/<dirname>.json` (gitignored). Every stage records its outputs there and short-circuits when the relevant key is already populated:

- `image_url` → set by `art_upload`
- `torrent_path`, `info_hash` → set by `make_torrent`
- `ops_group_id`, `ops_torrent_id` → set by `ops_upload`
- `qbit_added` → set by `qbit_add`

Safe to re-run the harness on the same dirs. Use `--force` on individual stages to override.

## Where things land

```
workhorse:~/Downloads/                  ← source dirs (renamed in place by normalize)
  A Weather - Cove (2008) [WEB FLAC]/
  A Weather - Cove (2008) [WEB 320]/
  A Weather - Cove (2008) [WEB V0]/

natto:/mnt/media/music/                 ← Navidrome scans here
  A Weather - Cove (2008) [WEB FLAC]/   ← FLAC: in your library AND seeded

natto:/mnt/media/seed-only/             ← qBit seeds; Navidrome doesn't see
  A Weather - Cove (2008) [WEB 320]/
  A Weather - Cove (2008) [WEB V0]/

tools/orpheus/upload/torrents/          ← per-format .torrent files (gitignored)
tools/orpheus/upload/state/             ← per-album manifests (gitignored)
```

## Prerequisites

- **SSH agent loaded** with the key authorized on natto. Stages 3 (`transfer`) and 7 (`qbit_add`) shell out to `ssh natto …` non-interactively, so an agent prompt mid-pipeline will fail. Before running: `ssh-add ~/.ssh/id_ed25519` (macOS keychain caches the passphrase for subsequent sessions).
- **`tools/orpheus/.venv`** populated from `requirements.txt`. The pipeline scripts assume `tools/orpheus/.venv/bin/python`.

## Secrets (`tools/orpheus/secrets.env`, gitignored)

```
OPS_API_KEY=...                         # ops_upload uses this
OPS_ANNOUNCE_URL=https://home.opsfet.ch/<passkey>/announce   # make_torrent uses this
```

No qBit credentials needed — `qbit_add` reaches qBit through the `qbit-port-updater` container, which shares gluetun's network namespace and triggers qBit's "bypass auth for clients on localhost" path.

## OPS form gotchas worth remembering

The first run uncovered these in order; the scripts now handle them, but if you ever hit similar errors in a future OPS schema change, this is the map:

- **`type` is required even on add-to-group uploads** — sending only `groupid` is rejected with "type is not specified".
- **`remaster_year` is required** even for original editions — OPS distinguishes group year from edition year and rejects without it. Defaults to the album's release year.
- **`album_desc` minimum 10 chars** — we default it to the BBCode tracklist when not provided.
- **OPS may auto-match into an existing empty group**: even if your search doesn't find the album, an empty `<groupId>` may exist (no torrents) and OPS will silently attach your upload there instead of creating a new group. The response carries `newgroup: false` in that case — check the URL.
- **OPS response keys are camelCase** (`torrentId`, `groupId`), not snake_case.

## Running stages individually

For debugging or partial runs:

```sh
.venv/bin/python upload/album_inspect.py "<dir>" [...]
.venv/bin/python upload/normalize.py [--apply] "<dir>" [...]
.venv/bin/python upload/transfer.py [--dry-run] "<dir>" [...]
.venv/bin/python upload/art_upload.py [--image-url URL | --force] "<dir>" [...]
.venv/bin/python upload/make_torrent.py [--force] "<dir>" [...]
.venv/bin/python upload/ops_upload.py "<dir>" --tags "..." [--apply]
.venv/bin/python upload/qbit_add.py "<dir>" [...]
```

## What's NOT here yet

- **No re-upload of MP3s as transcodes from your FLAC** (à la OPSBetter / REDBetter). The current pipeline assumes you have all three formats from the source already (Bandcamp ships them). If you ever have only a FLAC and want to add 320 + V0 to an existing OPS group, write a stage 2.5 that pipes the FLAC through `lame -V 0` / `lame --preset cbr 320`, then re-enter the pipeline at stage 1 on the transcoded dir.
- **No spectral analysis check** — OPS detects transcode-from-lossy uploads via spectral inspection and will warn/ban repeat offenders. For Bandcamp purchases this isn't a risk, but if you ever upload a FLAC of uncertain provenance, run it through `sox <file>.flac -n spectrogram -o /tmp/<name>.png` and eyeball it for a hard cutoff at 16–17 kHz before uploading.
- **No CD-rip workflow** — log/cue handling, EAC/XLD validation. Add when you start uploading CD rips.
