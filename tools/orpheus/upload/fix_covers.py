#!/usr/bin/env python3
"""Re-host a previously-uploaded cover to R2; print the URL for manual paste.

For OPS groups whose wikiImage URL went broken (e.g., the catbox.moe →
R2 cutover), this script:

  1. SSH-fetches the cover.* sidecar from the album's FLAC dir on natto
     (where rsync put it during the original upload).
  2. Uploads to R2 via art_upload.upload_to_r2 (content-addressed key,
     so the URL is stable forever).
  3. Updates the local manifest's image_url.
  4. Prints the new URL alongside the OPS group-edit link.

The actual wikiImage swap on OPS is manual — paste the new URL into
the group's edit form. OPS's action=groupedit is session-auth, not
API-token, and a one-time fix for a handful of groups isn't worth
the form-scrape complexity.

Usage:

  python fix_covers.py "Fat Possum Records - New Beats From the Delta (2019) [WEB FLAC]" \\
                       "Mr. Muthafuckin' eXquire - Kismet (2024) [WEB FLAC]" \\
                       "Eriko Toyoda - Have You Smiled Today_ (2011) [WEB FLAC]"

Each positional arg must match BOTH:
  - the album dir name under /mnt/media/music on natto, AND
  - the manifest stem at upload/state/<name>.json.

(These are the same thing for albums uploaded by run_pipeline.py, since
that's the directory name we chose in normalize.)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import boto3  # noqa: F401  — imported for early failure if missing
from botocore.exceptions import BotoCoreError, ClientError

STAGE_DIR = Path(__file__).parent
sys.path.insert(0, str(STAGE_DIR))
from art_upload import (  # noqa: E402
    SECRETS_PATH,
    STATE_DIR,
    load_env,
    load_manifest,
    make_r2_client,
    save_manifest,
    upload_to_r2,
)

NATTO_HOST = "natto"
NATTO_MUSIC = "/mnt/media/music"
COVER_GLOBS = ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
               "folder.jpg", "folder.jpeg", "folder.png")


def remote_find_cover(album_dir: str) -> str | None:
    """Return the absolute path of the cover sidecar on natto, or None."""
    # One ssh call, test each candidate in order. Bash-only, no python.
    tests = " || ".join(
        f'test -f "$d/{name}" && echo "$d/{name}" && exit 0'
        for name in COVER_GLOBS
    )
    cmd = f'd={shlex.quote(album_dir)}; {tests}; exit 1'
    r = subprocess.run(["ssh", NATTO_HOST, cmd], capture_output=True)
    if r.returncode != 0:
        return None
    line = r.stdout.decode().strip()
    return line or None


def remote_get(remote_path: str, local_path: Path) -> None:
    r = subprocess.run(
        ["scp", "-q", f"{NATTO_HOST}:{remote_path}", str(local_path)],
        capture_output=False)
    if r.returncode != 0:
        raise RuntimeError(f"scp from natto failed (rc={r.returncode})")


def fix_one(client, env: dict, dirname: str, apply: bool) -> int:
    manifest = load_manifest(dirname)
    if not manifest:
        print(f"  {dirname}: no manifest at {STATE_DIR / (dirname + '.json')}",
              file=sys.stderr)
        return 1
    group_id = manifest.get("ops_group_id")
    if not group_id:
        print(f"  {dirname}: manifest has no ops_group_id; nothing to fix",
              file=sys.stderr)
        return 1

    remote_dir = f"{NATTO_MUSIC}/{dirname}"
    print(f"\n=== {dirname} (group {group_id}) ===")
    print(f"  looking for cover under {NATTO_HOST}:{remote_dir}")
    remote_cover = remote_find_cover(remote_dir)
    if remote_cover is None:
        print(f"  ERROR: no cover sidecar found on natto for this album",
              file=sys.stderr)
        return 1
    print(f"  found: {remote_cover}")

    if not apply:
        print("  (dry-run; --apply to upload to R2 and update manifest)")
        return 0

    with tempfile.TemporaryDirectory(prefix="fix_covers_") as tmp:
        local = Path(tmp) / Path(remote_cover).name
        remote_get(remote_cover, local)
        md5 = hashlib.md5(local.read_bytes()).hexdigest()
        old_url = manifest.get("image_url")
        print(f"  cover md5: {md5}")
        print(f"  old URL:   {old_url}")
        try:
            new_url = upload_to_r2(client, env["R2_BUCKET"],
                                   env["R2_PUBLIC_BASE"], local, md5)
        except (BotoCoreError, ClientError) as e:
            print(f"  ERROR: R2 upload failed: {e}", file=sys.stderr)
            return 2
        print(f"  new URL:   {new_url}")

    manifest["image_url"] = new_url
    manifest["cover_md5"] = md5
    save_manifest(dirname, manifest)

    # Also update sibling format manifests for the same album, so a future
    # re-run of art_upload doesn't churn.
    siblings_updated = 0
    if "[WEB" in dirname:
        # Strip the [WEB ...] suffix to find sibling format dirs.
        prefix = dirname.split(" [WEB")[0]
        for p in STATE_DIR.glob(f"{prefix} [WEB *].json"):
            if p.stem == dirname:
                continue
            sm = json.loads(p.read_text())
            if sm.get("ops_group_id") == group_id:
                sm["image_url"] = new_url
                sm["cover_md5"] = md5
                p.write_text(json.dumps(sm, indent=2) + "\n")
                siblings_updated += 1
    if siblings_updated:
        print(f"  also updated {siblings_updated} sibling manifest(s) "
              f"(same group_id)")

    edit_url = (f"https://orpheus.network/torrents.php?"
                f"action=editgroup&groupid={group_id}")
    print(f"\n  ACTION: paste {new_url}")
    print(f"          into the 'Image' field at {edit_url}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dirnames", nargs="+",
                    help="album dir names (basename, no /mnt/media/music prefix); "
                         "must also match a manifest stem under upload/state/")
    ap.add_argument("--apply", action="store_true",
                    help="actually upload to R2 and update manifests "
                         "(default: dry-run)")
    args = ap.parse_args()

    env = load_env(SECRETS_PATH)
    try:
        client = make_r2_client(env) if args.apply else None
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    rc = 0
    for dirname in args.dirnames:
        rc |= fix_one(client, env, dirname, args.apply)

    if not args.apply:
        print("\n(dry-run; re-run with --apply to upload + update manifests)")
    else:
        print("\n=== DONE ===")
        print("Manually paste each new URL into the OPS edit-group form "
              "linked above. (No API-token endpoint for wikiImage edits.)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
