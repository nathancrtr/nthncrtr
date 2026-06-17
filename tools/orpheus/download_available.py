#!/usr/bin/env python3
"""Fetch the seeded-FLAC torrents for wishlist albums that ARE on Orpheus, and
add them to qBittorrent so they download.

This closes the loop on the wishlist pipeline — the inverse of the upload side:

    parse_wishlist.py      RYM wishlist HTML     -> wishlist.csv
    check_availability.py  wishlist.csv          -> orpheus-available.csv (+ novel/review)
    download_available.py  orpheus-available.csv -> .torrents fetched + added to qBit

check_availability.py already did the hard part: each row of orpheus-available.csv
carries the best seeded-FLAC `ops_torrent_id` for an album you wishlisted. This
script just turns that column into actual downloads.

For each row it:
  1. fetches the .torrent for ops_torrent_id from OPS (action=download), cached
     under --out as <torrent_id>.torrent (a cached file with non-zero size is
     not re-fetched);
  2. adds it to qBittorrent on natto so it downloads into --savepath
     (default /mnt/media/music, Navidrome's library root), tagged --category.

The qBit add uses the same localhost-auth-bypass side-channel as the upload
pipeline's qbit_add.py: the .torrent is piped over ssh into
`docker exec -i <container> curl` POSTing to 127.0.0.1:8080. The qbit-port-updater
container shares gluetun's network namespace, so qBit sees 127.0.0.1 as the
client and skips auth. No qBit password crosses the wire.

Auth:        OPS_API_KEY from --env-file (default ./secrets.env).
Rate limit:  MIN_INTERVAL between OPS calls, under Gazelle's 5/10s cap.

Idempotency:
  - a cached .torrent with non-zero size is not re-fetched;
  - qBit dedupes by infohash, so re-adding is a harmless no-op;
  - download-manifest.json (in --out) records per-torrent fetched/added state,
    so re-runs skip the ssh round-trip for things already added.
Safe to Ctrl-C and re-run.

Usage:
  ./download_available.py --probe                         # verify OPS auth, exit
  ./download_available.py --dry-run                       # list what would be fetched/added
  ./download_available.py --fetch-only                    # download .torrents, don't touch qBit
  ./download_available.py --limit 1                        # one full round-trip, end to end
  ./download_available.py                                  # the whole available list
  ./download_available.py --paused                         # add to qBit stopped (review first)
  ./download_available.py --use-tokens                     # spend a freeleech token per torrent

Note on ratio: this leeches a batch from a private tracker. Either spend
freeleech tokens (--use-tokens) so the bytes don't count, or mind your buffer.
"""
from __future__ import annotations

import argparse
import csv
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://orpheus.network/ajax.php"
USER_AGENT = "nthncrtr-wishlist-tool/0.1"
MIN_INTERVAL = 3.0  # seconds between OPS calls; matches check_availability.py


def load_env(env_file: Path) -> dict[str, str]:
    if not env_file.exists():
        return {}
    out: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class OPS:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._last = 0.0

    def _throttle(self) -> None:
        wait = MIN_INTERVAL - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)

    def _request(self, params: str, accept_binary: bool):
        self._throttle()
        req = Request(BASE + "?" + params, headers={
            "Authorization": f"token {self.api_key}",
            "User-Agent": USER_AGENT,
            "Accept": "*/*" if accept_binary else "application/json",
        })
        attempts = 0
        while True:
            attempts += 1
            try:
                with urlopen(req, timeout=60) as resp:
                    data = resp.read()
                self._last = time.time()
                return data
            except HTTPError as e:
                self._last = time.time()
                if e.code == 429 and attempts < 4:
                    backoff = 5 * attempts
                    print(f"  429 — sleeping {backoff}s", file=sys.stderr)
                    time.sleep(backoff)
                    continue
                snippet = e.read()[:200] if hasattr(e, "read") else b""
                print(f"  HTTP {e.code} on {params}: {snippet!r}", file=sys.stderr)
                return None
            except (URLError, TimeoutError) as e:
                self._last = time.time()
                print(f"  network error on {params}: {e}", file=sys.stderr)
                return None

    def index(self) -> dict | None:
        raw = self._request("action=index", accept_binary=False)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"  bad index JSON: {e}", file=sys.stderr)
            return None
        if data.get("status") != "success":
            print(f"  index api error: {data.get('error')!r}", file=sys.stderr)
            return None
        return data.get("response")

    def download(self, torrent_id: str | int,
                 use_token: bool = False) -> tuple[bytes | None, str | None]:
        """Fetch the .torrent. Returns (data, error); error is None on success.

        With use_token, append usetoken=1 so OPS marks the torrent freeleech for
        you and spends one freeleech token. The token is applied server-side at
        download time and is idempotent per torrent (re-downloading the same id
        doesn't spend a second one). The .torrent bytes are identical either way,
        so caching the file is fine — what we track is whether the token landed.
        """
        params = f"action=download&id={torrent_id}"
        if use_token:
            params += "&usetoken=1"
        data = self._request(params, accept_binary=True)
        if data is None:
            return None, "request failed"
        if data.startswith(b"d"):
            return data, None
        # Not bencoded -> OPS returned a JSON error envelope (e.g. token refused:
        # too few tokens, or torrent over the token size cap).
        try:
            env = json.loads(data)
            msg = env.get("error") or env.get("status") or repr(data[:120])
        except json.JSONDecodeError:
            msg = f"non-bencode response {data[:120]!r}"
        return None, str(msg)


def ssh_preflight(host: str) -> bool:
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "true"],
        capture_output=True,
    )
    if r.returncode != 0:
        print(f"ssh {host} failed non-interactively: "
              f"{r.stderr.decode(errors='replace').strip()}\n"
              f"Load your key first:  ssh-add ~/.ssh/id_ed25519", file=sys.stderr)
        return False
    return True


def add_to_qbit(torrent_path: Path, host: str, container: str,
                save_path: str, category: str, paused: bool) -> dict:
    """Pipe the .torrent over ssh into the qbit-port-updater curl side-channel.

    The multipart filename is omitted on purpose (qBit reads the name from the
    bencode); shlex.quote the rest defensively. skip_checking=false so qBit
    hash-checks then downloads the missing pieces — i.e. everything, since this
    is a fresh acquisition. qBit 5.x uses "stopped"; we send "paused" too for
    older builds.
    """
    flag = "true" if paused else "false"
    parts = [
        f"docker exec -i {shlex.quote(container)} curl -s -X POST",
        "-F torrents=@-",
        f"-F {shlex.quote('savepath=' + save_path)}",
        f"-F {shlex.quote('category=' + category)}",
        f"-F {shlex.quote('stopped=' + flag)}",
        f"-F {shlex.quote('paused=' + flag)}",
        "-F skip_checking=false",
        "http://127.0.0.1:8080/api/v2/torrents/add",
    ]
    with torrent_path.open("rb") as fh:
        result = subprocess.run(
            ["ssh", host, " ".join(parts)],
            stdin=fh, capture_output=True, timeout=60,
        )
    if result.returncode != 0:
        raise RuntimeError(f"ssh/docker exec failed (rc={result.returncode}): "
                           f"{result.stderr.decode(errors='replace')}")
    body = result.stdout.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}  # qBit add returns "Ok." plaintext, not JSON


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_manifest(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, indent=2) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, default=Path("/tmp/orpheus-available.csv"),
                    help="orpheus-available.csv from check_availability.py")
    ap.add_argument("--env-file", type=Path,
                    default=Path(__file__).parent / "secrets.env")
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "torrents",
                    help="dir for cached .torrent files + the manifest")
    ap.add_argument("--host", default="natto", help="ssh host running qBit")
    ap.add_argument("--container", default="qbit-port-updater",
                    help="container in gluetun's netns used for the auth-bypass POST")
    ap.add_argument("--savepath", default="/mnt/media/music",
                    help="qBit save path (default: Navidrome's library root)")
    ap.add_argument("--category", default="wishlist", help="qBit category tag")
    ap.add_argument("--paused", action="store_true",
                    help="add to qBit stopped, so you can review before they start")
    ap.add_argument("--use-tokens", action="store_true",
                    help="spend a freeleech token on each download (usetoken=1) so the "
                         "bytes don't count against your ratio; a torrent that can't take "
                         "a token (too large / too few left) is skipped, not grabbed on ratio")
    ap.add_argument("--limit", type=int, default=0,
                    help="only process the first N rows (0 = all)")
    ap.add_argument("--fetch-only", action="store_true",
                    help="download .torrents into --out, don't touch qBit")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be fetched/added; no OPS calls, no qBit")
    ap.add_argument("--probe", action="store_true",
                    help="hit action=index to verify OPS auth, then exit")
    args = ap.parse_args()

    api_key = load_env(args.env_file).get("OPS_API_KEY", "")
    if not api_key:
        print(f"OPS_API_KEY missing in {args.env_file}", file=sys.stderr)
        return 1
    ops = OPS(api_key)

    if args.probe:
        info = ops.index()
        if not info:
            return 2
        stats = info.get("userstats", {})
        print(f"auth ok: id={info.get('id')} username={info.get('username')!r} "
              f"ratio={stats.get('ratio')} class={info.get('class')!r}")
        return 0

    if not args.csv.exists():
        print(f"{args.csv} not found — run check_availability.py first", file=sys.stderr)
        return 1
    rows = list(csv.DictReader(args.csv.open(encoding="utf-8")))
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print("no rows to process", file=sys.stderr)
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / "download-manifest.json"
    manifest = load_manifest(manifest_path)

    do_add = not (args.fetch_only or args.dry_run)
    if do_add and not ssh_preflight(args.host):
        return 1

    n_fetched = n_added = n_skipped = n_errors = n_tokened = 0
    for i, row in enumerate(rows, 1):
        tid = (row.get("ops_torrent_id") or "").strip()
        label = f'{row.get("artist", "")} — {row.get("album", "")}'.strip(" —")
        if not tid:
            print(f"[{i}/{len(rows)}] {label}: row has no ops_torrent_id; skipping",
                  file=sys.stderr)
            n_errors += 1
            continue

        rec = manifest.setdefault(tid, {})
        rec.update(artist=row.get("artist", ""), album=row.get("album", ""),
                   group_id=row.get("ops_group_id", ""),
                   encoding=row.get("encoding", ""), media=row.get("media", ""))
        dest = args.out / f"{tid}.torrent"

        if args.dry_run:
            state = "cached" if dest.exists() and dest.stat().st_size else "would fetch"
            if args.use_tokens and not rec.get("freeleech_token"):
                state += " +freeleech token"
            if args.fetch_only:
                action = "fetch-only"
            else:
                action = "already in qBit" if rec.get("qbit_added") else "would add to qBit"
            print(f"[{i}/{len(rows)}] {label}  (tid {tid}: {state}, {action})")
            continue

        # 1. fetch .torrent. The file is cached, but a freeleech token is a
        # server-side effect applied at download time — so when --use-tokens is
        # set and the token isn't on this torrent yet, re-issue the download
        # (with usetoken=1) even if the .torrent file is already cached.
        cached = dest.exists() and dest.stat().st_size > 0
        want_token = args.use_tokens and not rec.get("freeleech_token")
        if cached and not want_token:
            rec["fetched"] = True
            print(f"[{i}/{len(rows)}] {label}  (tid {tid} cached)", file=sys.stderr)
        else:
            tok = " +freeleech token" if want_token else ""
            print(f"[{i}/{len(rows)}] {label}  fetching tid {tid}{tok}...", file=sys.stderr)
            data, err = ops.download(tid, use_token=want_token)
            if data is None:
                if want_token:
                    print(f"    freeleech token NOT applied (tid {tid}): {err}\n"
                          f"    skipping — re-run without --use-tokens to grab it on ratio.",
                          file=sys.stderr)
                else:
                    print(f"    fetch failed (tid {tid}): {err}", file=sys.stderr)
                n_errors += 1
                save_manifest(manifest_path, manifest)
                continue
            tmp = dest.with_suffix(".torrent.partial")
            tmp.write_bytes(data)
            tmp.replace(dest)
            rec["fetched"] = True
            n_fetched += 1
            if want_token:
                rec["freeleech_token"] = True
                n_tokened += 1

        # 2. add to qBit
        if do_add and not rec.get("qbit_added"):
            try:
                resp = add_to_qbit(dest, args.host, args.container,
                                   args.savepath, args.category, args.paused)
            except Exception as e:  # noqa: BLE001 — report and continue the batch
                print(f"    qBit add ERROR: {e}", file=sys.stderr)
                n_errors += 1
                save_manifest(manifest_path, manifest)
                continue
            ok = resp.get("success_count", 0) > 0 or resp.get("raw", "").startswith("Ok")
            if not ok:
                print(f"    qBit add FAILED: {resp}", file=sys.stderr)
                n_errors += 1
            else:
                rec["qbit_added"] = True
                rec["savepath"] = args.savepath
                print(f"    added → {args.savepath} [{args.category}]"
                      f"{' (stopped)' if args.paused else ''}", file=sys.stderr)
                n_added += 1
        elif rec.get("qbit_added"):
            n_skipped += 1

        save_manifest(manifest_path, manifest)

    token_note = f" tokens-spent={n_tokened}" if args.use_tokens else ""
    print(f"\nsummary: fetched={n_fetched} added={n_added} "
          f"already-added={n_skipped} errors={n_errors}{token_note} (of {len(rows)})",
          file=sys.stderr)
    print(f"  torrents + manifest in {args.out}", file=sys.stderr)
    return 0 if n_errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
