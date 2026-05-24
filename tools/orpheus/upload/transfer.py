#!/usr/bin/env python3
"""Transfer normalized album dirs from workhorse to natto via rsync.

FLAC dirs go to /mnt/media/music/ (scanned by Navidrome).
MP3 dirs go to /mnt/media/seed-only/ (qBit seeds them; Navidrome doesn't see them).

Routing is by the '[WEB <FORMAT>]' suffix in the dir name (set by normalize.py).
The destination path is preserved verbatim so it matches what the .torrent file
created in stage 5 will reference; qBit can then seed without re-checking.
"""
from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path

NATTO_HOST = "natto"
MUSIC_DEST = "/mnt/media/music"
SEED_ONLY_DEST = "/mnt/media/seed-only"

# Accept any OPS-supported media value (kept symmetric with qbit_add.py).
# Routing only cares whether the format starts with FLAC, so media is
# informational here.
SUFFIX_RE = re.compile(
    r"\[(?:CD|WEB|Vinyl|SACD|DVD|Blu-Ray|Cassette|DAT|Soundboard) "
    r"(FLAC( 24bit)?|V0|V2|320|VBR\([^)]+\))\]$"
)


def destination_for(album_dir: Path) -> str:
    """Pick MUSIC_DEST for FLAC (Navidrome sees it), SEED_ONLY_DEST otherwise."""
    m = SUFFIX_RE.search(album_dir.name)
    if not m:
        raise SystemExit(f"dir name lacks expected '[WEB FORMAT]' suffix: {album_dir.name!r} "
                         f"(run normalize.py first)")
    fmt = m.group(1)
    return MUSIC_DEST if fmt.startswith("FLAC") else SEED_ONLY_DEST


def ensure_remote_dirs(dry_run: bool) -> None:
    """Idempotent mkdir on natto for both destinations."""
    cmd = f"mkdir -p {MUSIC_DEST} {SEED_ONLY_DEST}"
    print(f"  remote: ssh {NATTO_HOST} {cmd!r}")
    if not dry_run:
        subprocess.run(["ssh", NATTO_HOST, cmd], check=True)


def transfer_one(album_dir: Path, dry_run: bool) -> dict:
    dest = destination_for(album_dir)
    # Trailing slash on the source copies its contents into the named subdir;
    # rsync creates that subdir under dest (which ensure_remote_dirs has made).
    # Stuck with --progress instead of --info=progress2 because macOS still
    # ships rsync 2.6.9, which doesn't understand --info.
    full_dest = f"{dest}/{album_dir.name}/"
    # macOS rsync 2.6.9 lacks --protect-args; the remote shell sees the path as
    # raw text and chokes on parens and apostrophes. shlex.quote escapes them
    # in a form bash/zsh accept; keep the host:path separator outside the quote.
    remote_arg = f"{NATTO_HOST}:{shlex.quote(full_dest)}"
    rsync = [
        "rsync",
        "-ah",
        "--progress",
        "--partial",
        f"{album_dir}/",
        remote_arg,
    ]
    print(f"  {album_dir.name}  →  {NATTO_HOST}:{full_dest}")
    if dry_run:
        rsync.insert(1, "--dry-run")
    result = subprocess.run(rsync)
    return {"album": album_dir.name, "dest": full_dest, "returncode": result.returncode}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", type=Path, nargs="+",
                    help="one or more normalized album directories")
    ap.add_argument("--dry-run", action="store_true",
                    help="run rsync in --dry-run mode (no files copied)")
    args = ap.parse_args()

    for p in args.paths:
        if not p.is_dir():
            print(f"skip: {p} is not a directory", file=sys.stderr)
            return 1

    print("ensuring remote dirs exist:")
    ensure_remote_dirs(args.dry_run)
    print()

    results = []
    for p in args.paths:
        print(f"transferring:")
        results.append(transfer_one(p, args.dry_run))
        print()

    failed = [r for r in results if r["returncode"] != 0]
    if failed:
        print(f"FAILED: {len(failed)}/{len(results)} transfers had non-zero rsync exit", file=sys.stderr)
        return 1
    print(f"all {len(results)} transfers ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
