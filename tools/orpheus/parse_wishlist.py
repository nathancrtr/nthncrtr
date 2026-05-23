#!/usr/bin/env python3
"""Parse an RYM wishlist HTML snapshot into a CSV.

Input: a "Save Page As" HTML capture of https://rateyourmusic.com/collection_p/<user>/wishlist
Output: CSV with one row per release.

Columns:
  artist        Displayed artist string as RYM renders it (e.g., "Apollo Brown & Che Noir").
                Use this for the Orpheus search.
  artists       Pipe-separated canonical artist names extracted from individual <a> tags
                (e.g., "Apollo Brown|Che Noir"). For collabs, useful as fallback search terms.
  album         Release title.
  release_type  album | ep | single | djmix (from the release URL path).
  rym_id        Numeric RYM release id (from the anchor's title="[AlbumNNN]" attribute).
  rym_url       Full RYM release URL (kept for human review / opening in a browser).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup, Tag

RELEASE_TYPE_RE = re.compile(r"/release/([a-z]+)/")
ALBUM_ID_RE = re.compile(r"Album(\d+)")


def parse_artist_cell(td: Tag) -> tuple[str, list[str]]:
    """Return (display_string, canonical_artist_list).

    Three RYM variants seen in wishlist exports:
      - solo:           <a class="artist">Aphex Twin</a>
      - split:          <a>A</a> &amp; <a>B</a>  (also " vs. ")
      - credited_name:  <span class="credited_name">Display
                          <div class="credited_list">...members...</div>
                        </span>
                        where Display is the form to search on, and the inner
                        members are RYM's canonical decomposition.
    """
    credited = td.find("span", class_="credited_name")
    if credited:
        display = "".join(c for c in credited.children if isinstance(c, str)).strip()
        canonical = [a.get_text(strip=True) for a in credited.select("div.credited_list a.artist")]
        return display, canonical

    anchors = td.find_all("a", class_="artist")
    canonical = [a.get_text(strip=True) for a in anchors]
    # collapse whitespace; preserves the " & " / " vs. " glue between anchors
    display = " ".join(td.get_text(separator=" ", strip=True).split())
    return display, canonical


def parse_album_cell(td: Tag) -> tuple[str, str, str, str]:
    """Return (title, release_type, album_id, url)."""
    a = td.find("a", class_="album")
    title = a.get_text(strip=True)
    url = a.get("href", "")
    rt = RELEASE_TYPE_RE.search(url)
    release_type = rt.group(1) if rt else ""
    aid = ALBUM_ID_RE.search(a.get("title", ""))
    album_id = aid.group(1) if aid else ""
    return title, release_type, album_id, url


def parse_wishlist(html_path: Path) -> list[dict]:
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    rows = soup.select('tr[id^="page_catalog_item_"]')
    items: list[dict] = []
    for tr in rows:
        artist_td = tr.find("td", class_="or_q_artist")
        album_td = tr.find("td", class_="or_q_album")
        if not artist_td or not album_td:
            continue  # the header row has <th> cells and is skipped here
        artist_display, canonical = parse_artist_cell(artist_td)
        title, release_type, album_id, url = parse_album_cell(album_td)
        items.append({
            "artist": artist_display,
            "artists": "|".join(canonical),
            "album": title,
            "release_type": release_type,
            "rym_id": album_id,
            "rym_url": url,
        })
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("html", type=Path, help="path to RYM wishlist HTML snapshot")
    ap.add_argument("-o", "--output", type=Path, default=Path("wishlist.csv"))
    args = ap.parse_args()

    items = parse_wishlist(args.html)
    if not items:
        print("no wishlist items parsed", file=sys.stderr)
        return 1

    fieldnames = ["artist", "artists", "album", "release_type", "rym_id", "rym_url"]
    with args.output.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(items)
    print(f"wrote {len(items)} items to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
