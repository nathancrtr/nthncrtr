# One-time migration off Google Drive into Nextcloud

A **one-shot** data liberation: pull the full contents of a Google Drive into
the self-hosted Nextcloud on natto, verify it, then (manually, deliberately)
stop relying on Google. This is not a recurring sync — there is intentionally
no timer. Run it once, check it, done.

Prereqs: the Nextcloud stack (`services/nextcloud/`) is up on natto and you
have logged into the web UI once as the admin user so the data skeleton
(`/srv/nextcloud/data/<user>/files/`) exists. `rclone` available — run it on
workhorse (it has the data path nowhere, so copy over Tailscale) **or**
install it on natto and copy locally; the local-on-natto path is simpler and
avoids a LAN round-trip, so that's what's documented below.

## 1. Configure an rclone remote for Google Drive

```sh
ssh -t natto
sudo apt-get install -y rclone        # if not present
rclone config
#  n) new remote
#  name> gdrive
#  storage> drive
#  client_id / client_secret> (see note)
#  scope> 1   (full access, read is enough but 1 is simplest)
#  Edit advanced config> n
#  Use auto config> n   (headless: it prints a URL; auth in a browser,
#                        paste the token back)
```

> **Strongly recommended:** create your own Google OAuth client ID
> (`client_id`/`client_secret`) rather than using rclone's shared default.
> The shared default is heavily rate-limited and a full-Drive pull will crawl
> or 403. https://rclone.org/drive/#making-your-own-client-id — 10 minutes of
> setup, hours saved.

## 2. Decide the Google-native export format (the real decision)

Google Docs/Sheets/Slides/Drawings are **not files** — they have no bytes to
download. rclone exports them on the fly to whatever format you choose. This
choice is permanent for this migration; pick before the copy:

| Choice | Flag | Trade-off |
|---|---|---|
| **MS Office** (recommended) | `--drive-export-formats docx,xlsx,pptx,svg` | Opens everywhere (LibreOffice, MS Office, Nextcloud's editors). Highest-fidelity round-trip in practice. |
| **OpenDocument** | `--drive-export-formats odt,ods,odp,svg` | Truly open formats; ideal if you'll standardize on LibreOffice/Collabora. Slightly more conversion drift from Google's side. |
| PDF (archival) | `--drive-export-formats pdf` | Faithful but **read-only** — you lose editability. Only if this is a frozen archive, not a working set. |

The examples below use the recommended MS Office set. Native uploaded files
(real PDFs, images, zips, etc.) are unaffected by this flag — they copy
byte-for-byte.

## 3. Size it first (dry run)

```sh
rclone size gdrive:
rclone copy --dry-run --drive-export-formats docx,xlsx,pptx,svg \
  gdrive: /srv/nextcloud/data/<user>/files/GoogleDrive 2>&1 | tail -20
```

Sanity-check the reported total against the < 50 GB assumption behind the
internal-disk sizing decision (WORKLIST 5.1). If it's materially larger,
**stop** and revisit storage before copying — the Beelink's internal disk is
the constraint.

## 4. Copy into the Nextcloud user's files

```sh
sudo rclone copy --drive-export-formats docx,xlsx,pptx,svg \
  --transfers 4 --tpslimit 8 --progress \
  gdrive: /srv/nextcloud/data/<user>/files/GoogleDrive
```

Files land owned by root (rclone ran under sudo). Nextcloud's PHP runs as the
in-container `www-data`; hand ownership over before scanning:

```sh
docker exec nextcloud chown -R www-data:www-data \
  /var/www/html/data/<user>/files/GoogleDrive
```

## 5. Make Nextcloud index the externally-placed files

Nextcloud only knows about files it placed itself until you rescan:

```sh
docker exec -u www-data nextcloud php occ files:scan \
  --path="<user>/files/GoogleDrive"
# or, for everything: docker exec -u www-data nextcloud php occ files:scan --all
```

## 6. Verify, then (and only then) let go of Google

- Web UI: the `GoogleDrive/` folder is browsable; counts look right.
- Open a migrated **Doc** and a **Sheet** — confirm the export is faithful
  (formulas, formatting, embedded images). Spot-check a few nested folders.
- `docker exec -u www-data nextcloud php occ files:scan --all` reports no
  errors; Administration → Overview shows no new warnings.
- Only after you're satisfied: delete the data from Google Drive **manually**,
  through Google's own UI. Deleting from Google is deliberately **out of
  scope** for this runbook and is never automated — an automated mass delete
  of the source during a migration is exactly the irreversible mistake this
  separation prevents.

## Rollback

Purely additive — nothing in Google is touched until step 6, by hand.

```sh
rm -rf /srv/nextcloud/data/<user>/files/GoogleDrive
docker exec -u www-data nextcloud php occ files:scan --path="<user>/files"
```

Then re-run from step 3 (e.g. with a different export format).
