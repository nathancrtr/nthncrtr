#!/usr/bin/env python3
"""Build a click-through worklist of RYM release links for albums you snatched,
so you can mark them "In Collection" by hand.

RYM has no public API and its ToS forbids automated/scripted access to the site
(they ban for it), so we deliberately do NOT POST to RYM. We don't need to: the
wishlist already hands us the exact release-page URL for every album. parse_wishlist.py
captures it as rym_url (e.g. https://rateyourmusic.com/release/album/akufen/my-way/),
and check_availability.py forwards it into orpheus-available.csv. This just turns
the rym_url of each snatched album into a checklist; you open each and set
ownership to "In Collection" yourself. ToS-clean, zero account risk.

Inputs:
  --csv       orpheus-available.csv from check_availability.py — provides rym_url
              and ops_torrent_id per album.
  --manifest  download_available.py's download-manifest.json — used to filter to
              the albums that actually made it into qBit (qbit_added: true).

By default the list is the intersection: available rows whose ops_torrent_id is
marked qbit_added in the manifest. Pass --all to skip the manifest and list every
available row instead.

Output (default HTML): a checklist; ticks persist in the browser via localStorage
so you can work through it across sessions. --format md|txt for a plain list.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from pathlib import Path
from urllib.parse import quote_plus

RYM_BASE = "https://rateyourmusic.com"


def rym_link(row: dict) -> str:
    url = (row.get("rym_url") or "").strip()
    if url:
        return url if url.startswith("http") else RYM_BASE + url  # hrefs are absolute; relative is a safety net
    # Only reached for a pre-rym_url CSV: land one click from the release via search.
    term = quote_plus(f'{row.get("artist", "")} {row.get("album", "")}'.strip())
    return f"{RYM_BASE}/search?searchterm={term}&searchtype=l"  # l = releases


def load_snatched_tids(manifest_path: Path) -> set[str]:
    if not manifest_path.exists():
        return set()
    data = json.loads(manifest_path.read_text())
    return {tid for tid, rec in data.items() if rec.get("qbit_added")}


def select_rows(csv_path: Path, manifest_path: Path, include_all: bool) -> list[dict]:
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    if include_all:
        return rows
    snatched = load_snatched_tids(manifest_path)
    if not snatched:
        raise SystemExit(
            f"no qbit_added entries in {manifest_path}\n"
            f"  run download_available.py first, or pass --all to list every "
            f"available row regardless of what's in qBit."
        )
    return [r for r in rows if (r.get("ops_torrent_id") or "").strip() in snatched]


def render_html(entries: list[dict]) -> str:
    items = []
    for e in entries:
        key = html.escape(e["key"], quote=True)
        link = html.escape(e["link"], quote=True)
        label = html.escape(e["label"])
        sub = html.escape(e["sub"])
        items.append(
            f'    <li><label><input type="checkbox" data-key="{key}"> '
            f'<a href="{link}" target="_blank" rel="noopener">{label}</a>'
            f'<span class="sub"> — {sub}</span></label></li>'
        )
    body = "\n".join(items)
    total = len(entries)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RYM — mark In Collection ({total})</title>
<style>
  body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 820px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.25rem; }}
  .meta {{ color: #555; margin-bottom: 1rem; }}
  #progress {{ font-weight: 600; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ padding: .25rem 0; border-bottom: 1px solid #eee; }}
  li.done label {{ opacity: .45; text-decoration: line-through; }}
  label {{ display: flex; gap: .5rem; align-items: baseline; cursor: pointer; }}
  input {{ transform: translateY(1px); }}
  a {{ text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .sub {{ color: #888; font-size: .85em; }}
</style>
</head>
<body>
<h1>Mark these {total} albums &ldquo;In Collection&rdquo; on RYM</h1>
<p class="meta">Open each link, set ownership to <b>In Collection</b> on the
release page, then tick it here. Ticks persist in this browser.
<span id="progress"></span></p>
<ul id="list">
{body}
</ul>
<script>
  const KEY = "rym-collection-marked";
  const store = JSON.parse(localStorage.getItem(KEY) || "{{}}");
  const boxes = [...document.querySelectorAll('input[type=checkbox]')];
  function refresh() {{
    let done = 0;
    for (const b of boxes) {{
      const on = !!store[b.dataset.key];
      b.checked = on;
      b.closest('li').classList.toggle('done', on);
      if (on) done++;
    }}
    document.getElementById('progress').textContent = `(${{done}} / ${{boxes.length}} marked)`;
  }}
  for (const b of boxes) {{
    b.addEventListener('change', () => {{
      if (b.checked) store[b.dataset.key] = true; else delete store[b.dataset.key];
      localStorage.setItem(KEY, JSON.stringify(store));
      refresh();
    }});
  }}
  refresh();
</script>
</body>
</html>
"""


def render_text(entries: list[dict], markdown: bool) -> str:
    lines = []
    for e in entries:
        if markdown:
            lines.append(f"- [ ] [{e['label']}]({e['link']}) — {e['sub']}")
        else:
            lines.append(f"{e['label']}  ({e['sub']})\n    {e['link']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, default=Path("/tmp/orpheus-available.csv"),
                    help="orpheus-available.csv from check_availability.py")
    ap.add_argument("--manifest", type=Path,
                    default=Path(__file__).parent / "torrents" / "download-manifest.json",
                    help="download_available.py manifest; filters to qbit_added rows")
    ap.add_argument("--all", action="store_true",
                    help="list every available row, ignoring the manifest/qBit filter")
    ap.add_argument("--output", type=Path, default=Path("/tmp/rym-collection-links.html"))
    ap.add_argument("--format", choices=["html", "md", "txt"], default="html")
    args = ap.parse_args()

    if not args.csv.exists():
        print(f"{args.csv} not found — run check_availability.py first", file=sys.stderr)
        return 1

    rows = select_rows(args.csv, args.manifest, args.all)
    if not rows:
        print("no albums to list (none matched the qBit filter)", file=sys.stderr)
        return 0

    entries = []
    fallbacks = 0
    for r in rows:
        link = rym_link(r)
        if "/search?" in link:
            fallbacks += 1
        entries.append({
            "key": (r.get("rym_id") or r.get("ops_torrent_id") or link).strip(),
            "link": link,
            "label": f'{r.get("artist", "")} — {r.get("album", "")}'.strip(" —"),
            "sub": " ".join(x for x in (r.get("encoding", ""), r.get("media", "")) if x),
        })
    entries.sort(key=lambda e: e["label"].lower())

    if args.format == "html":
        content = render_html(entries)
    else:
        content = render_text(entries, markdown=(args.format == "md"))
    args.output.write_text(content, encoding="utf-8")

    print(f"wrote {len(entries)} links to {args.output}", file=sys.stderr)
    if fallbacks:
        print(f"  note: {fallbacks} row(s) had no rym_url and got a RYM *search* "
              f"link instead — re-run check_availability.py to capture rym_url.",
              file=sys.stderr)
    if args.format == "html":
        print(f"  open it:  open {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
