#!/usr/bin/env python3
"""Convert a list of album *folder names* into a wishlist CSV that
check_availability.py can consume.

Input: a text file with one folder name or path per line — e.g. a library
listing, or the Lidarr-deletion manifest
(/mnt/media/backups/lidarr-deleted-albums.txt). Lines look like:

  /mnt/media/music/Asake - Mr. Money With The Vibe (2022) [WEB FLAC]
  Madvillain - 2004 - Madvillainy (FLAC)
  [2017] BROCKHAMPTON - SATURATION 3 (FLAC)

Output: CSV with the same columns parse_wishlist.py emits
(artist, artists, album, release_type, rym_id, rym_url) so the rest of the
pipeline (check_availability.py -> download_available.py) is unchanged. The
rym_* columns are blank (these never came from RYM) and release_type defaults
to "album".

Parsing is best-effort: OPS fuzzy-matches on artist+album, so approximate
output is fine, and anything unparseable lands in orpheus-novel/review for
manual triage rather than being silently dropped. Lines that yield no usable
artist+album are written to --unparsed for eyeballing.

Usage:
  ./parse_library_folders.py /tmp/lidarr-deleted-albums.txt -o /tmp/recovery-wishlist.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

YEAR = r"(?:19|20)\d{2}"


def strip_brackets(s: str) -> str:
    """Remove (...) [...] {...} groups (year/catalog/format tags), repeatedly."""
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\([^()]*\)", " ", s)
        s = re.sub(r"\[[^\[\]]*\]", " ", s)
        s = re.sub(r"\{[^{}]*\}", " ", s)
    return s


def tidy(s: str) -> str:
    return " ".join(s.split()).strip(" -_.")


def parse(name: str) -> tuple[str, str] | None:
    base = os.path.basename(name.rstrip("/"))
    # leading [YYYY] / (YYYY)
    base = re.sub(rf"^\s*[\[(]\s*{YEAR}\s*[\])]\s*", "", base)
    base = strip_brackets(base)
    base = re.sub(r"\bflac\b", " ", base, flags=re.I)
    # an embedded year used as a separator: "Artist - 2004 - Album",
    # "Artist -2015- Album" -> collapse to a single " - "
    base = re.sub(rf"\s+-?\s*{YEAR}\s*-?\s+", " - ", base)
    # scene names: no spaces but underscores/double-hyphens
    if " " not in base:
        base = base.replace("_", " ")
    base = base.replace("--", " - ")

    parts = [tidy(p) for p in re.split(r"\s-\s", base)]
    parts = [p for p in parts if p]
    if not parts:
        return None

    def is_junk(p: str) -> bool:
        # pure year, or a bare numeric scene id
        return bool(re.fullmatch(rf"{YEAR}", p)) or bool(re.fullmatch(r"\d{2,9}", p))

    artist = tidy(parts[0])
    rest = [p for p in parts[1:] if not is_junk(p)]
    album = rest[0] if rest else (parts[1] if len(parts) > 1 else "")
    album = re.sub(rf"\s+{YEAR}$", "", tidy(album))  # trailing year on the title
    if not artist or not album:
        return None
    return artist, album


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("infile", type=Path, help="one folder name/path per line")
    ap.add_argument("-o", "--output", type=Path, default=Path("/tmp/recovery-wishlist.csv"))
    ap.add_argument("--unparsed", type=Path, default=Path("/tmp/recovery-unparsed.txt"))
    args = ap.parse_args()

    lines = [ln.strip() for ln in args.infile.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    rows: list[dict] = []
    unparsed: list[str] = []
    seen: set[tuple[str, str]] = set()
    for ln in lines:
        got = parse(ln)
        if not got:
            unparsed.append(ln)
            continue
        artist, album = got
        key = (artist.lower(), album.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append({"artist": artist, "artists": "", "album": album,
                     "release_type": "album", "rym_id": "", "rym_url": ""})

    fieldnames = ["artist", "artists", "album", "release_type", "rym_id", "rym_url"]
    with args.output.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    args.unparsed.write_text("\n".join(unparsed) + ("\n" if unparsed else ""),
                             encoding="utf-8")
    print(f"parsed {len(rows)} rows -> {args.output}", file=sys.stderr)
    print(f"unparsed {len(unparsed)} -> {args.unparsed}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
