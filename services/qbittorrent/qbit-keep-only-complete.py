#!/usr/bin/env python3
"""
Inside-the-gluetun-netns body of qbit-keep-only-complete.sh.

NOT invoked directly. The wrapper bind-mounts this file into a
python:3-alpine container that shares gluetun's network namespace, so
qBit's localhost-auth-bypass applies and we can talk to qBit at
http://127.0.0.1:8080 without a password.

Behavior (all scoped to ONE qBit category, set via env CATEGORY):
  1. Sanity-checks: refuse to run with empty category; warn loudly if any
     in-scope torrent is in an actively-downloading state (suggests the
     operator forgot --paused on qbit-bulk-add.sh).
  2. Polls torrents/info until no in-scope torrent is in a "checking*"
     state (qBit's hash-check is the truth oracle).
  3. Splits results by progress: 100% → keep, <100% → drop.
  4. Reports both buckets by name.
  5. If DRY_RUN=true: stops here.
  6. Otherwise: DELETE the drop bucket from qBit (deleteFiles=false; the
     disk is NEVER touched), then START the keep bucket.

Env (set by the wrapper):
  CATEGORY    required; the safety boundary
  DRY_RUN     "true" or "false"
  POLL        seconds between polls (int)
  MAX_WAIT    abort if checks aren't done in this many seconds (int)
  ALLOW_ACTIVE  "true" disables the active-download abort
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request

QBIT = "http://127.0.0.1:8080"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def fmt_bytes(n: float) -> str:
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024:
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} PiB"


def get_info(category: str) -> list[dict]:
    url = f"{QBIT}/api/v2/torrents/info?category={urllib.parse.quote(category)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def post(path: str, **fields: str) -> str:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(f"{QBIT}{path}", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def main() -> int:
    category = env("CATEGORY")
    dry_run = env("DRY_RUN", "true") == "true"
    poll = int(env("POLL", "5"))
    max_wait = int(env("MAX_WAIT", "3600"))
    allow_active = env("ALLOW_ACTIVE", "false") == "true"

    if not category:
        print("[cull] ERROR: CATEGORY is empty; refusing to run", file=sys.stderr)
        return 2

    print(f"[cull] scope: category={category!r} dry_run={dry_run} "
          f"poll={poll}s max_wait={max_wait}s allow_active={allow_active}",
          flush=True)

    ts = get_info(category)
    if not ts:
        print(f"[cull] no torrents in category {category!r}; nothing to do.")
        return 0
    print(f"[cull] found {len(ts)} torrents in category {category!r}", flush=True)

    active_states = {"downloading", "stalledDL", "queuedDL", "forcedDL", "metaDL",
                     "uploading", "stalledUP", "queuedUP", "forcedUP"}
    active = [t for t in ts if t.get("state", "") in active_states]
    if active and not allow_active:
        print(f"[cull] ERROR: {len(active)} torrent(s) in active (non-paused) "
              f"state — refusing to act:", file=sys.stderr)
        for t in active[:10]:
            print(f"  [{t.get('state','?')}] {t.get('name','?')}", file=sys.stderr)
        if len(active) > 10:
            print(f"  ...and {len(active)-10} more", file=sys.stderr)
        print("[cull] Looks like --paused was missed on qbit-bulk-add.sh, or",
              file=sys.stderr)
        print("[cull] other workflows wrote torrents into this category. Stop",
              file=sys.stderr)
        print("[cull] those first (qBit UI: select all → Stop), or pass",
              file=sys.stderr)
        print("[cull] ALLOW_ACTIVE=true if you really mean to act on them too.",
              file=sys.stderr)
        return 4

    start_ts = time.time()
    while True:
        ts = get_info(category)
        checking = [t for t in ts if str(t.get("state", "")).startswith("checking")]
        if not checking:
            break
        elapsed = int(time.time() - start_ts)
        if elapsed > max_wait:
            print(f"[cull] ERROR: {len(checking)} still hash-checking after "
                  f"{elapsed}s (> max_wait {max_wait}s); bailing without action",
                  file=sys.stderr)
            return 3
        print(f"[cull] {len(checking)}/{len(ts)} still hash-checking "
              f"(elapsed {elapsed}s); sleeping {poll}s...", flush=True)
        time.sleep(poll)

    ts = get_info(category)
    keep = [t for t in ts if float(t.get("progress", 0)) >= 1.0]
    drop = [t for t in ts if float(t.get("progress", 0)) < 1.0]

    keep_b = sum(int(t.get("total_size", t.get("size", 0))) for t in keep)
    drop_b = sum(int(t.get("total_size", t.get("size", 0))) for t in drop)

    print()
    print(f"[cull] decision (category={category!r}):")
    print(f"  keep    : {len(keep):4} torrents, {fmt_bytes(keep_b)}  (progress == 100%)")
    print(f"  delete  : {len(drop):4} torrents, {fmt_bytes(drop_b)}  (progress  < 100%)")
    print()

    print("[cull] --- KEEP (will be resumed) ---")
    for t in sorted(keep, key=lambda x: x.get("name", "")):
        print(f"  100.0% {t.get('name','?')}")
    print()
    print("[cull] --- DELETE FROM QBIT (files on disk NOT touched) ---")
    for t in sorted(drop, key=lambda x: float(x.get("progress", 0)), reverse=True):
        pct = 100 * float(t.get("progress", 0))
        print(f"  {pct:5.1f}% {t.get('name','?')}")
    print()

    if dry_run:
        print("[cull] dry-run: no actions taken.")
        print("[cull] re-run with --yes to delete the bottom bucket and "
              "resume the top.")
        return 0

    if drop:
        hashes = "|".join(t["hash"] for t in drop)
        print(f"[cull] deleting {len(drop)} torrents from qBit "
              f"(deleteFiles=false — disk NOT touched)...", flush=True)
        post("/api/v2/torrents/delete", hashes=hashes, deleteFiles="false")
    if keep:
        hashes = "|".join(t["hash"] for t in keep)
        print(f"[cull] starting {len(keep)} matched torrents (seeding)...",
              flush=True)
        post("/api/v2/torrents/start", hashes=hashes)

    print("[cull] done.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
