#!/usr/bin/env python3
"""
Fetch .torrent files for the operator's Beyond-HD account.

Used to bootstrap a fresh qBittorrent install (or recover after the on-disk
state and BT_backup are both gone). Output is two subdirectories of .torrent
files — `movies/` and `tv/`, split by BHD category so they can be bulk-added
to qBit with the correct per-savepath flag — plus a manifest per list type.

Auth (both required):
    BHD_API_TOKEN — goes in the URL path; authenticates the JSON request.
    BHD_RSS_KEY   — goes in the POST body; required for the API to return
                    usable `download_url` values for each torrent.

    Both are read from one of (first wins):
      1. --secrets <path>      (an env-file with KEY=VALUE lines)
      2. ./secrets.env         (next to the script)
      3. /srv/qbittorrent/secrets.env
      4. BHD_API_TOKEN / BHD_RSS_KEY environment variables

Rate limit:
    BHD's documented ceiling is conservative; we sleep RATE_LIMIT_SLEEP
    between calls.

Idempotency:
    A .torrent file already present in --out with non-zero size is skipped.
    Safe to Ctrl-C and re-run.

Layout:
    Output directories are <out>/movies/ and <out>/tv/. Other categories
    (anime, ebook, etc.) land in <out>/other/ and won't match either of the
    two video savepaths — useful as a "what else is in there" signal.

Usage:
    ./bhd-restore.py --probe                                # one POST, raw dump
    ./bhd-restore.py --dry-run --type completed             # enumerate only
    ./bhd-restore.py --type completed --limit 1             # one .torrent (probe)
    ./bhd-restore.py --type completed                       # full run
    ./bhd-restore.py --type seeding                         # also pick up seeding-only
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://beyond-hd.me/api/torrents"
RATE_LIMIT_SLEEP = 2.5
HTTP_TIMEOUT = 30
DEFAULT_SECRETS_CANDIDATES = [
    pathlib.Path(__file__).resolve().parent / "secrets.env",
    pathlib.Path("/srv/qbittorrent/secrets.env"),
]

TYPE_TO_FILTER = {
    "completed": "completed",
    "seeding":   "seeding",
    "leeching":  "leeching",
}


def load_secret(name: str, explicit_path: pathlib.Path | None) -> str:
    candidates = []
    if explicit_path is not None:
        candidates.append(explicit_path)
    candidates.extend(DEFAULT_SECRETS_CANDIDATES)
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except PermissionError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, val = line.partition("=")
            if key.strip() == name:
                return val.strip().strip('"').strip("'")
    env = os.environ.get(name)
    if env:
        return env
    sys.exit(
        f"{name} not found in any of: "
        + ", ".join(str(p) for p in candidates)
        + ", or env."
    )


def api_post(api_token: str, body: dict) -> dict:
    url = f"{API_BASE}/{api_token}"
    data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "nthncrtr-bhd-restore/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} on {url}: {e.read()[:300]!r}") from e
    return json.loads(raw)


def fetch_binary(url: str) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": "nthncrtr-bhd-restore/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} on {url}: {e.read()[:300]!r}") from e


def classify(category: str) -> str:
    """BHD's `category` is a string like 'Movies' or 'TV'. Map to subdir."""
    if not category:
        return "other"
    c = category.strip().lower()
    if c.startswith("movie"):
        return "movies"
    if c == "tv" or c.startswith("tv "):
        return "tv"
    return "other"


def enumerate_torrents(api_token: str, rss_key: str, kind: str) -> list[dict]:
    """Walk paginated results until we run out. BHD returns `results` plus
    `page`, `total_pages`, and `total_results`; we stop on the documented
    last page (and defensively also on an empty results array)."""
    items: list[dict] = []
    page = 1
    filter_key = TYPE_TO_FILTER[kind]
    while True:
        body = {
            "action": "search",
            "rsskey": rss_key,
            filter_key: 1,
            "page": page,
        }
        resp = api_post(api_token, body)
        if resp.get("success") is False:
            raise RuntimeError(f"BHD API error: {resp}")
        results = resp.get("results") or []
        if not results:
            break
        for t in results:
            items.append(
                {
                    "id":           t.get("id"),
                    "name":         t.get("name", ""),
                    "category":     t.get("category", ""),
                    "type":         t.get("type", ""),
                    "size":         t.get("size", 0),
                    "info_hash":    t.get("info_hash", ""),
                    "seeders":      t.get("seeders", 0),
                    "leechers":     t.get("leechers", 0),
                    "download_url": t.get("download_url", ""),
                    "kind":         kind,
                }
            )
        total_pages = resp.get("total_pages")
        current_page = resp.get("page", page)
        total_results = resp.get("total_results")
        print(
            f"[bhd] page={current_page}"
            + (f"/{total_pages}" if total_pages else "")
            + f" got {len(results)} items (cumulative={len(items)}"
            + (f"/{total_results}" if total_results else "")
            + ")",
            flush=True,
        )
        if total_pages is not None and current_page >= total_pages:
            break
        page = (current_page or page) + 1
        time.sleep(RATE_LIMIT_SLEEP)
    return items


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--out", default="./restore-bhd", help="output directory (default: ./restore-bhd)")
    p.add_argument("--type", default="completed", choices=sorted(TYPE_TO_FILTER))
    p.add_argument("--limit", type=int, default=0, help="cap on .torrent downloads (0 = unlimited)")
    p.add_argument("--dry-run", action="store_true", help="enumerate only, skip .torrent fetches")
    p.add_argument(
        "--probe",
        action="store_true",
        help="single POST against page 1 and dump the raw JSON response, then exit",
    )
    p.add_argument(
        "--secrets",
        type=pathlib.Path,
        help="path to secrets.env (default: probe standard locations)",
    )
    args = p.parse_args()

    api_token = load_secret("BHD_API_TOKEN", args.secrets)
    rss_key = load_secret("BHD_RSS_KEY", args.secrets)

    if args.probe:
        body = {
            "action": "search",
            "rsskey": rss_key,
            TYPE_TO_FILTER[args.type]: 1,
            "page": 1,
        }
        print(f"[bhd] probing {API_BASE}/<token> with body={ {k:v for k,v in body.items() if k!='rsskey'} } + rsskey", flush=True)
        resp = api_post(api_token, body)
        keys = list(resp.keys())
        print(f"[bhd] response top-level keys: {keys}", flush=True)
        for k in ("status", "status_code", "total_results", "current_page", "last_page", "per_page"):
            if k in resp:
                print(f"[bhd]   {k}: {resp[k]!r}", flush=True)
        results = resp.get("results") or []
        print(f"[bhd] results: {len(results)} on page 1", flush=True)
        if results:
            print("[bhd] first result keys:", sorted(results[0].keys()), flush=True)
            print(json.dumps(results[0], indent=2), flush=True)
        return 0

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "movies").mkdir(exist_ok=True)
    (out / "tv").mkdir(exist_ok=True)
    (out / "other").mkdir(exist_ok=True)

    print(f"[bhd] enumerating type={args.type}...", flush=True)
    manifest = enumerate_torrents(api_token, rss_key, args.type)
    manifest_path = out / f"manifest-{args.type}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"[bhd] manifest: {len(manifest)} torrents -> {manifest_path}", flush=True)

    by_bucket = {"movies": 0, "tv": 0, "other": 0}
    for t in manifest:
        by_bucket[classify(t["category"])] += 1
    print(
        f"[bhd] by category: movies={by_bucket['movies']} "
        f"tv={by_bucket['tv']} other={by_bucket['other']}",
        flush=True,
    )

    if args.dry_run:
        print("[bhd] --dry-run: skipping .torrent fetches", flush=True)
        return 0

    fetched = skipped = errors = 0
    for i, t in enumerate(manifest, start=1):
        if args.limit and (fetched + errors) >= args.limit:
            print(f"[bhd] --limit {args.limit} reached", flush=True)
            break
        tid = t["id"]
        bucket = classify(t["category"])
        dest = out / bucket / f"{tid}.torrent"
        if dest.exists() and dest.stat().st_size > 0:
            skipped += 1
            continue
        url = t.get("download_url") or ""
        if not url:
            errors += 1
            print(f"[bhd] [{i}/{len(manifest)}] {tid}: ERROR no download_url in manifest entry", file=sys.stderr, flush=True)
            continue
        try:
            data = fetch_binary(url)
            if not data.startswith(b"d"):
                raise RuntimeError(
                    f"response for torrent {tid} is not a bencoded dict "
                    f"(first bytes: {data[:80]!r})"
                )
            tmp = dest.with_name(dest.name + ".partial")
            tmp.write_bytes(data)
            tmp.replace(dest)
            fetched += 1
            print(
                f"[bhd] [{i}/{len(manifest)}] {tid} [{bucket}]: "
                f"{t['name']} ({len(data)} bytes)",
                flush=True,
            )
        except Exception as e:
            errors += 1
            print(f"[bhd] [{i}/{len(manifest)}] {tid}: ERROR {e}", file=sys.stderr, flush=True)
        time.sleep(RATE_LIMIT_SLEEP)

    print(f"[bhd] done: fetched={fetched} skipped={skipped} errors={errors}", flush=True)
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
