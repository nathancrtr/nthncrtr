#!/usr/bin/env python3
"""Find FLAC albums on natto whose OPS group is missing 320 or V0.

For each FLAC album directory under /mnt/media/music (on natto by default):
  1. Parse (artist, album, year) from the directory name.
  2. Query OPS for the matching torrent group (reuses check_availability's
     OPS client, retry chain, and fuzzy matcher).
  3. List the formats already in that group.
  4. Report what's missing from the canonical (FLAC, MP3 320, MP3 V0) set.

Output: a human-readable gap report on stdout (status messages to stderr).
Doesn't transcode or upload — the next step (LAME transcode + add-to-group
upload) is a follow-up. The cache at upload/state/_gap_scan_cache.json keeps
re-runs cheap (7-day TTL).
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

SCAN_DIR = Path(__file__).parent
sys.path.insert(0, str(SCAN_DIR))
from check_availability import OPS, best_match, load_env, normalize, search  # noqa: E402

NATTO_HOST = "natto"
MUSIC_DIR = "/mnt/media/music"
CACHE_PATH = SCAN_DIR / "upload" / "state" / "_gap_scan_cache.json"
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

# Canonical format set we care about for gap-fill. OPS distinguishes
# Lossless (16-bit) from "24bit Lossless"; either FLAC counts as covering
# the FLAC slot for the purposes of "this group has a FLAC at all".
CANONICAL = {
    "FLAC": "Lossless",
    "MP3-320": "320",
    "MP3-V0": "V0 (VBR)",
}

# Dir-name parsers — these are intentionally permissive because the music
# library predates normalize.py's strict convention and contains a mix of
# styles. Anything that doesn't parse cleanly is bucketed as "parse failed"
# and ignored rather than misclassified.
FLAC_TOKEN_RE = re.compile(r"\s*\[(?:WEB\s+)?FLAC(?:\s+24bit)?\]\s*", re.IGNORECASE)
CATALOG_RE = re.compile(r"\s*\{[^}]+\}\s*")
ALBUM_YEAR_RE = re.compile(r"\s*\((\d{4})\)\s*")
TRAILING_YEAR_RE = re.compile(r"\s+(\d{4})\s*$")
PARENS_RE = re.compile(r"\s*\([^)]+\)\s*")


def list_music_dirs(host: str, root: str) -> list[str]:
    """SSH and emit one dir basename per line."""
    remote = (f"find {shlex.quote(root)} -mindepth 1 -maxdepth 1 -type d "
              f"-printf '%f\\n'")
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, remote],
        capture_output=True,
    )
    if r.returncode != 0:
        print(f"ERROR: ssh {host} 'find {root}' failed: "
              f"{r.stderr.decode(errors='replace').strip()}", file=sys.stderr)
        sys.exit(2)
    return [line for line in r.stdout.decode().splitlines() if line.strip()]


def parse_dir(name: str) -> tuple[str, str, str | None] | None:
    """Parse a FLAC album dir name into (artist, album, year-or-None).

    Returns None if the name doesn't contain a FLAC format marker (i.e.,
    not a FLAC album) or the artist/album split fails.
    """
    if not FLAC_TOKEN_RE.search(name):
        return None
    cleaned = FLAC_TOKEN_RE.sub(" ", name)
    cleaned = CATALOG_RE.sub(" ", cleaned)
    # Several labels ship dirs with en-dash (U+2013) or em-dash (U+2014)
    # between artist and album, sometimes with a directional-marker (LRM/RLM)
    # snuck in. Fold all of those to plain ASCII " - " before the split.
    cleaned = (cleaned.replace("–", "-")  # en-dash
                      .replace("—", "-")  # em-dash
                      .replace("‎", "")   # LRM
                      .replace("‏", ""))  # RLM
    year = None
    m = ALBUM_YEAR_RE.search(cleaned)
    if m:
        year = m.group(1)
        cleaned = cleaned[:m.start()] + " " + cleaned[m.end():]
    cleaned = PARENS_RE.sub(" ", cleaned).strip()
    if year is None:
        m = TRAILING_YEAR_RE.search(cleaned)
        if m:
            year = m.group(1)
            cleaned = cleaned[:m.start()].strip()
    parts = cleaned.split(" - ", 1)
    if len(parts) != 2:
        return None
    artist, album = parts[0].strip(), parts[1].strip()
    # Common convention: `Artist - YYYY - Album` instead of `Artist - Album (YYYY)`.
    # If we haven't already pulled a year out and the album starts with a
    # 4-digit year followed by another dash, lift it. Guard with year is None
    # so an explicit (YYYY) in the original name wins.
    if year is None:
        m = re.match(r"^(\d{4})\s*-\s*(.+)$", album)
        if m:
            year = m.group(1)
            album = m.group(2).strip()
    if not artist or not album:
        return None
    return artist, album, year


def load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2) + "\n")


def cache_key(artist: str, album: str, year: str | None) -> str:
    return f"{normalize(artist)}::{normalize(album)}::{year or ''}"


def cache_fresh(entry: dict, ttl: int) -> bool:
    return time.time() - entry.get("checked_at", 0) < ttl


def query_group(ops: OPS, artist: str, album: str, year: str | None) -> dict:
    """Run search → best_match; return a compact group descriptor or no_match."""
    groups = search(ops, artist, album, [artist], release_type="")
    if not groups:
        return {"status": "no_match"}
    group, score, _ = best_match(groups, artist, album, release_type="")
    if group is None:
        return {"status": "no_match"}
    return {
        "status": "matched",
        "score": round(score, 3),
        "group_id": group["groupId"],
        "group_name": group.get("groupName", ""),
        "group_artist": group.get("artist", ""),
        "group_year": group.get("groupYear", ""),
        "torrents": [{
            "format": t.get("format", ""),
            "encoding": t.get("encoding", ""),
            "media": t.get("media", ""),
            "torrent_id": t.get("torrentId"),
            "seeders": t.get("seeders", 0),
        } for t in group.get("torrents", [])],
    }


def format_gaps(torrents: list[dict]) -> tuple[set[str], set[str]]:
    """Return (present, missing) as sets of canonical labels: FLAC, MP3-320, MP3-V0.

    A group is considered to "have" a slot if any torrent (regardless of
    edition/media) matches that slot's (format, encoding). FLAC counts
    whether it's 16-bit Lossless or 24bit Lossless.
    """
    present: set[str] = set()
    for t in torrents:
        f, e = t["format"], t["encoding"]
        if f == "FLAC" and e in ("Lossless", "24bit Lossless"):
            present.add("FLAC")
        elif f == "MP3" and e == "320":
            present.add("MP3-320")
        elif f == "MP3" and e == "V0 (VBR)":
            present.add("MP3-V0")
    missing = set(CANONICAL.keys()) - present
    return present, missing


def print_section(title: str, items: list) -> None:
    print(f"═══ {title} ({len(items)}) ═══")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=NATTO_HOST,
                    help="SSH host to scan (default: natto)")
    ap.add_argument("--root", default=MUSIC_DIR,
                    help="music root on host (default: /mnt/media/music)")
    ap.add_argument("--env-file", type=Path, default=SCAN_DIR / "secrets.env",
                    help="env file holding OPS_API_KEY")
    ap.add_argument("--match-threshold", type=float, default=0.85,
                    help="below this fuzzy score, hits go to 'low confidence' bucket")
    ap.add_argument("--limit", type=int, default=0,
                    help="process at most N FLAC dirs (for testing)")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass OPS lookup cache and re-query each album")
    ap.add_argument("--verbose", action="store_true",
                    help="list already-complete albums too")
    args = ap.parse_args()

    api_key = load_env(args.env_file).get("OPS_API_KEY", "")
    if not api_key:
        print(f"OPS_API_KEY missing in {args.env_file}", file=sys.stderr)
        return 1

    print(f"Listing {args.host}:{args.root} …", file=sys.stderr)
    dirs = list_music_dirs(args.host, args.root)
    print(f"  {len(dirs)} dirs total", file=sys.stderr)

    flac_albums: list[tuple[str, tuple[str, str, str | None]]] = []
    parse_failed: list[str] = []
    for d in dirs:
        parsed = parse_dir(d)
        if parsed is None:
            # Only flag dirs that *looked* like FLAC dirs (had the token).
            # Anything without a FLAC marker is silently ignored — those
            # are MP3-only dirs we wouldn't gap-fill from.
            if FLAC_TOKEN_RE.search(d):
                parse_failed.append(d)
            continue
        flac_albums.append((d, parsed))
    print(f"  {len(flac_albums)} parseable FLAC albums; "
          f"{len(parse_failed)} parse failures", file=sys.stderr)
    if args.limit:
        flac_albums = flac_albums[:args.limit]
        print(f"  --limit: capping to first {args.limit}", file=sys.stderr)

    cache = {} if args.no_cache else load_cache(CACHE_PATH)
    ops = OPS(api_key)

    gaps: list = []
    complete: list = []
    no_match: list = []
    low_conf: list = []

    cached_hits = 0
    for i, (dirname, (artist, album, year)) in enumerate(flac_albums, 1):
        key = cache_key(artist, album, year)
        cached = cache.get(key)
        if cached and cache_fresh(cached, CACHE_TTL_SECONDS) and not args.no_cache:
            entry = cached
            cached_hits += 1
        else:
            print(f"[{i}/{len(flac_albums)}] {artist} — {album} "
                  f"({year or '?'})", file=sys.stderr)
            entry = query_group(ops, artist, album, year)
            entry["checked_at"] = time.time()
            cache[key] = entry

        if entry["status"] == "no_match":
            no_match.append((dirname, artist, album, year))
            continue
        if entry["score"] < args.match_threshold:
            low_conf.append((dirname, artist, album, year, entry))
            continue
        present, missing = format_gaps(entry["torrents"])
        if missing:
            gaps.append((dirname, artist, album, year, entry, present, missing))
        else:
            complete.append((dirname, artist, album, year, entry))

    if not args.no_cache:
        save_cache(CACHE_PATH, cache)

    print(f"\n(cached: {cached_hits}/{len(flac_albums)} lookups)\n",
          file=sys.stderr)

    # Sort gaps by #missing ascending — single-missing-format jobs first (easier wins).
    gaps.sort(key=lambda x: (len(x[6]), x[1].lower(), x[2].lower()))

    print_section("Gaps found", gaps)
    for dirname, artist, album, year, entry, present, missing in gaps:
        url = f"https://orpheus.network/torrents.php?id={entry['group_id']}"
        present_str = " ".join(sorted(present)) or "(none in canonical set)"
        missing_str = " ".join(sorted(missing))
        print(f"  {artist} — {album} ({year or '?'})")
        print(f"    dir:     {dirname}")
        print(f"    group:   {entry['group_id']}  ({url})")
        print(f"    have:    {present_str}")
        print(f"    missing: {missing_str}")
    print()

    if args.verbose:
        print_section("Already complete", complete)
        for dirname, artist, album, year, entry in complete:
            print(f"  {artist} — {album} ({year or '?'})  group {entry['group_id']}")
        print()
    else:
        print(f"═══ Already complete: {len(complete)} (pass --verbose to list) ═══\n")

    print_section("No OPS match (would need full-triplet upload)", no_match)
    for dirname, artist, album, year in no_match:
        print(f"  {artist} — {album} ({year or '?'})  [dir: {dirname}]")
    print()

    print_section("Low-confidence match (review manually)", low_conf)
    for dirname, artist, album, year, entry in low_conf:
        url = f"https://orpheus.network/torrents.php?id={entry['group_id']}"
        print(f"  {artist} — {album} ({year or '?'})  "
              f"candidate group {entry['group_id']} score={entry['score']}")
        print(f"    dir: {dirname}")
        print(f"    {url}")
    print()

    if parse_failed:
        print_section("Parse failed (dir name had [FLAC] but no clean artist/album split)",
                      parse_failed)
        for name in parse_failed:
            print(f"  {name}")
        print()

    print(f"Summary: gaps={len(gaps)} complete={len(complete)} "
          f"no-match={len(no_match)} low-conf={len(low_conf)} "
          f"parse-failed={len(parse_failed)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
