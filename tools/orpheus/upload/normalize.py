#!/usr/bin/env python3
"""Normalize a Bandcamp-style album directory for OPS upload.

In-place mutation, dry-run by default. Pass --apply to actually write changes.

Operations:
  1. Strip Bandcamp 'Visit https://*.bandcamp.com' signatures from COMMENT tags
     (only if the comment contains "bandcamp" — preserves any other comments).
  2. Rename the containing directory to the OPS naming convention:
       Artist - Album (Year) [WEB FORMAT]
     where FORMAT is FLAC / FLAC 24bit / V0 / V2 / 320.

Track filenames are left alone — Bandcamp's
'Artist - Album - NN Track.ext' format is OPS-acceptable as-is.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from mutagen import File as MutagenFile

# Import the report-building bits from the sibling stage so we don't drift.
sys.path.insert(0, str(Path(__file__).parent))
from album_inspect import inspect_album  # noqa: E402

# Characters that break on at least one major filesystem (FAT/NTFS). ext4 (natto's
# music drive root) accepts almost everything; we sanitize anyway so the torrent
# name is portable for Windows seeders downloading from OPS.
FS_UNSAFE_RE = re.compile(r'[<>:"/\\|?*]')


def encoding_label(fmt: str, encoding: str) -> str:
    """Map inspect.py's (format, encoding) to the OPS dir-suffix style."""
    if fmt == "FLAC":
        return "FLAC 24bit" if encoding == "24bit Lossless" else "FLAC"
    if fmt == "MP3":
        if encoding == "320":
            return "320"
        if encoding.startswith("V0"):
            return "V0"
        if encoding.startswith("V2"):
            return "V2"
        # generic VBR / other: keep the bracketed kbps marker
        return encoding.replace("(VBR)", "VBR").replace(" ", "")
    return f"{fmt} {encoding}"


def safe(s: str) -> str:
    s = FS_UNSAFE_RE.sub("_", s)
    return s.strip().rstrip(".")  # trailing dots/spaces break on Windows


def build_dirname(report: dict) -> str:
    suffix = encoding_label(report["format"], report["encoding"])
    return f"{safe(report['artist'])} - {safe(report['album'])} ({report['year']}) [WEB {suffix}]"


def strip_bandcamp_comment(track: Path, apply: bool) -> bool:
    """Return True if the track had a Bandcamp comment that was (or would be) stripped."""
    f = MutagenFile(track)
    if f is None or f.tags is None:
        return False
    changed = False
    if track.suffix.lower() == ".flac":
        comments = f.tags.get("comment", [])
        if comments and any("bandcamp" in str(c).lower() for c in comments):
            if apply:
                del f.tags["comment"]
            changed = True
    elif track.suffix.lower() == ".mp3":
        comms = f.tags.getall("COMM")
        if comms and any("bandcamp" in str(c).lower() for c in comms):
            if apply:
                f.tags.delall("COMM")
            changed = True
    if changed and apply:
        f.save()
    return changed


def normalize_album(album_dir: Path, apply: bool) -> dict:
    report = inspect_album(album_dir)
    if "error" in report:
        return {"path": str(album_dir), "actions": [], "error": report["error"]}

    actions: list[str] = []

    # 1. Strip Bandcamp signature from COMMENT.
    stripped = 0
    for t in sorted(album_dir.iterdir()):
        if t.suffix.lower() in (".flac", ".mp3"):
            if strip_bandcamp_comment(t, apply):
                stripped += 1
    if stripped:
        verb = "stripped" if apply else "would strip"
        actions.append(f"{verb} Bandcamp signature from COMMENT in {stripped}/{report['track_count']} tracks")

    # 2. Rename containing dir.
    target_name = build_dirname(report)
    final_path = album_dir
    if album_dir.name != target_name:
        target_path = album_dir.parent / target_name
        if target_path.exists():
            actions.append(f"SKIP rename: target already exists: {target_path}")
        else:
            verb = "renamed" if apply else "would rename"
            actions.append(f"{verb} dir: {album_dir.name!r} → {target_name!r}")
            if apply:
                album_dir.rename(target_path)
                final_path = target_path

    return {"path": str(final_path), "actions": actions}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", type=Path, nargs="+", help="one or more album directories")
    ap.add_argument("--apply", action="store_true",
                    help="actually mutate files/dirs; default is dry-run")
    args = ap.parse_args()

    any_actions = False
    for p in args.paths:
        result = normalize_album(p, args.apply)
        print(f"\n=== {Path(result['path']).name} ===")
        if "error" in result:
            print(f"  ERROR: {result['error']}")
            continue
        if not result["actions"]:
            print("  already normalized; no changes needed")
        else:
            any_actions = True
            for a in result["actions"]:
                print(f"  - {a}")
    if not args.apply and any_actions:
        print("\n(dry-run; re-run with --apply to make changes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
