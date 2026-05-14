#!/usr/bin/env python3
"""
Fetch .torrent files for the operator's Orpheus account.

Used to bootstrap a fresh qBittorrent install (or recover after the on-disk
state and BT_backup are both gone). Output is a directory of .torrent files
named <torrentId>.torrent plus a manifest.json listing what was fetched.

Auth:
    ORPHEUS_API_KEY is read from one of (first wins):
      1. --secrets <path>     (an env-file with KEY=VALUE lines)
      2. ./secrets.env        (next to the script)
      3. /srv/qbittorrent/secrets.env
      4. ORPHEUS_API_KEY environment variable

Rate limit:
    Orpheus's documented ceiling is ~5 requests / 10 seconds. We sleep
    RATE_LIMIT_SLEEP between calls.

Idempotency:
    A .torrent file already present in --out with non-zero size is skipped.
    Safe to Ctrl-C and re-run.

Usage:
    ./orpheus-restore.py --dry-run                         # enumerate only
    ./orpheus-restore.py --type snatched --limit 1         # one .torrent
    ./orpheus-restore.py --type snatched                   # full run
    ./orpheus-restore.py --type uploaded                   # also pick up own uploads
"""

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

API_BASE = "https://orpheus.network/ajax.php"
RATE_LIMIT_SLEEP = 2.5
HTTP_TIMEOUT = 30
DEFAULT_SECRETS_CANDIDATES = [
    pathlib.Path(__file__).resolve().parent / "secrets.env",
    pathlib.Path("/srv/qbittorrent/secrets.env"),
]


def load_token(explicit_path: pathlib.Path | None) -> str:
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
            if key.strip() == "ORPHEUS_API_KEY":
                return val.strip().strip('"').strip("'")
    env = os.environ.get("ORPHEUS_API_KEY")
    if env:
        return env
    sys.exit(
        "ORPHEUS_API_KEY not found in any of: "
        + ", ".join(str(p) for p in candidates)
        + ", or env."
    )


def api_request(url: str, token: str, accept_binary: bool = False):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {token}",
            "User-Agent": "nthncrtr-orpheus-restore/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} on {url}: {e.read()[:300]!r}") from e
    if accept_binary:
        return data
    parsed = json.loads(data)
    if parsed.get("status") != "success":
        raise RuntimeError(f"API error on {url}: {parsed}")
    return parsed


def enumerate_torrents(token: str, user_id: int, kind: str) -> list[dict]:
    items: list[dict] = []
    offset = 0
    while True:
        url = f"{API_BASE}?action=user_torrents&id={user_id}&type={kind}&offset={offset}"
        resp = api_request(url, token)
        page = resp["response"].get(kind, [])
        if not page:
            break
        for t in page:
            items.append(
                {
                    "torrentId": t["torrentId"],
                    "groupId": t.get("groupId"),
                    "name": t.get("name", ""),
                    "artistName": t.get("artistName", ""),
                    "torrentSize": t.get("torrentSize", 0),
                    "type": kind,
                }
            )
        print(
            f"[restore] page offset={offset} got {len(page)} items "
            f"(cumulative={len(items)})",
            flush=True,
        )
        offset += len(page)
        time.sleep(RATE_LIMIT_SLEEP)
    return items


def fetch_torrent(token: str, torrent_id: int, dest: pathlib.Path) -> int:
    url = f"{API_BASE}?action=download&id={torrent_id}"
    data = api_request(url, token, accept_binary=True)
    if not data.startswith(b"d"):
        raise RuntimeError(
            f"response for torrent {torrent_id} is not a bencoded dict "
            f"(first bytes: {data[:80]!r})"
        )
    tmp = dest.with_name(dest.name + ".partial")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return len(data)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="./torrents", help="output directory (default: ./torrents)")
    p.add_argument("--type", default="snatched", choices=["snatched", "uploaded", "seeding", "leeching"])
    p.add_argument("--limit", type=int, default=0, help="cap on .torrent downloads (0 = unlimited)")
    p.add_argument("--dry-run", action="store_true", help="enumerate only, skip .torrent fetches")
    p.add_argument("--secrets", type=pathlib.Path, help="path to secrets.env (default: probe standard locations)")
    args = p.parse_args()

    token = load_token(args.secrets)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print("[restore] validating API key...", flush=True)
    me = api_request(f"{API_BASE}?action=index", token)
    user_id = me["response"]["id"]
    username = me["response"]["username"]
    print(f"[restore] authenticated as {username} (id={user_id})", flush=True)
    time.sleep(RATE_LIMIT_SLEEP)

    print(f"[restore] enumerating type={args.type}...", flush=True)
    manifest = enumerate_torrents(token, user_id, args.type)
    manifest_path = out / f"manifest-{args.type}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"[restore] manifest: {len(manifest)} torrents -> {manifest_path}", flush=True)

    if args.dry_run:
        print("[restore] --dry-run: skipping .torrent fetches", flush=True)
        return 0

    fetched = skipped = errors = 0
    for i, t in enumerate(manifest, start=1):
        if args.limit and (fetched + errors) >= args.limit:
            print(f"[restore] --limit {args.limit} reached", flush=True)
            break
        tid = t["torrentId"]
        dest = out / f"{tid}.torrent"
        if dest.exists() and dest.stat().st_size > 0:
            skipped += 1
            continue
        label = f'{t["artistName"]} - {t["name"]}' if t["artistName"] else t["name"]
        try:
            size = fetch_torrent(token, tid, dest)
            fetched += 1
            print(f"[restore] [{i}/{len(manifest)}] {tid}: {label} ({size} bytes)", flush=True)
        except Exception as e:
            errors += 1
            print(f"[restore] [{i}/{len(manifest)}] {tid}: ERROR {e}", file=sys.stderr, flush=True)
        time.sleep(RATE_LIMIT_SLEEP)

    print(f"[restore] done: fetched={fetched} skipped={skipped} errors={errors}", flush=True)
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
