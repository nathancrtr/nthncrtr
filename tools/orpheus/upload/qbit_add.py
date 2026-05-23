#!/usr/bin/env python3
"""Add the per-album .torrent files to qBittorrent on natto so it seeds them.

Uses a clever side-channel: the qbit-port-updater container shares gluetun's
network namespace with qBittorrent, so requests it makes to 127.0.0.1:8080
hit qBit's "bypass auth for clients on localhost" path — no credentials
needed. We pipe each .torrent over SSH into a `docker exec -i qbit-port-updater
curl` POSTing to /api/v2/torrents/add.

Save paths:
  FLAC dirs → /mnt/media/music     (Navidrome scans here)
  MP3 dirs  → /mnt/media/seed-only (qBit-only)
matching what transfer.py rsynced into. qBit verifies the existing files
against the torrent's piece hashes (skip_checking=false) and transitions
straight to seeding.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

STATE_DIR = Path(__file__).parent / "state"
TORRENT_DIR = Path(__file__).parent / "torrents"
NATTO_HOST = "natto"
QBIT_CONTAINER = "qbit-port-updater"  # runs inside gluetun's netns; localhost-auth-bypass applies
QBIT_API = "http://127.0.0.1:8080/api/v2/torrents/add"

SUFFIX_RE = re.compile(r"\[WEB (FLAC( 24bit)?|V0|V2|320|VBR\([^)]+\))\]$")
MUSIC_DEST = "/mnt/media/music"
SEED_ONLY_DEST = "/mnt/media/seed-only"


def save_path_for(album_dirname: str) -> str:
    m = SUFFIX_RE.search(album_dirname)
    if not m:
        raise SystemExit(f"dir name lacks '[WEB FORMAT]' suffix: {album_dirname!r}")
    return MUSIC_DEST if m.group(1).startswith("FLAC") else SEED_ONLY_DEST


def load_manifest(dirname: str) -> dict:
    p = STATE_DIR / f"{dirname}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_manifest(dirname: str, manifest: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / f"{dirname}.json").write_text(json.dumps(manifest, indent=2) + "\n")


def add_to_qbit(torrent_path: Path, save_path: str, category: str) -> dict:
    """POST the .torrent file through ssh + docker exec curl; return parsed JSON.

    The filename in the multipart field is omitted on purpose — qBit reads
    the torrent name from the bencode itself, and embedding the album dirname
    (which can contain apostrophes etc.) in the curl arg breaks remote-shell
    quoting. shlex.quote() the rest defensively.
    """
    parts = [
        f"docker exec -i {QBIT_CONTAINER} curl -s -X POST",
        "-F torrents=@-",
        f"-F {shlex.quote('savepath=' + save_path)}",
        f"-F {shlex.quote('category=' + category)}",
        "-F skip_checking=false",
        "-F paused=false",
        QBIT_API,
    ]
    remote_cmd = " ".join(parts)
    with torrent_path.open("rb") as fh:
        result = subprocess.run(
            ["ssh", NATTO_HOST, remote_cmd],
            stdin=fh,
            capture_output=True,
            timeout=60,
        )
    if result.returncode != 0:
        raise RuntimeError(f"ssh/docker exec failed (rc={result.returncode}): "
                           f"{result.stderr.decode(errors='replace')}")
    # qBit returns JSON for the standard add response; older versions return "Ok." plaintext.
    body = result.stdout.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def already_added(info_hash: str) -> bool:
    """Check qBit for an existing torrent with this info_hash."""
    remote_cmd = (
        f"docker exec {QBIT_CONTAINER} curl -s "
        f"'http://127.0.0.1:8080/api/v2/torrents/info?hashes={info_hash}'"
    )
    result = subprocess.run(["ssh", NATTO_HOST, remote_cmd],
                            capture_output=True, timeout=30)
    if result.returncode != 0:
        return False
    try:
        return len(json.loads(result.stdout.decode())) > 0
    except json.JSONDecodeError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", type=Path, nargs="+", help="one or more normalized album directories")
    ap.add_argument("--category", default="orpheus",
                    help="qBit category to tag added torrents with (default: 'orpheus')")
    args = ap.parse_args()

    for path in args.paths:
        dirname = path.name
        manifest = load_manifest(dirname)
        if "info_hash" not in manifest or "torrent_path" not in manifest:
            print(f"  {dirname}: manifest missing info_hash/torrent_path; "
                  f"run make_torrent.py first", file=sys.stderr)
            continue
        if manifest.get("qbit_added"):
            print(f"  {dirname}: manifest says already added to qBit; skipping")
            continue
        if already_added(manifest["info_hash"]):
            print(f"  {dirname}: qBit already has info_hash {manifest['info_hash'][:8]}...; "
                  f"recording in manifest and skipping")
            manifest["qbit_added"] = True
            save_manifest(dirname, manifest)
            continue

        save_path = save_path_for(dirname)
        torrent_path = Path(manifest["torrent_path"])
        print(f"  {dirname}")
        print(f"    save_path: {save_path}")
        response = add_to_qbit(torrent_path, save_path, args.category)
        success = response.get("success_count", 0) > 0 or response.get("raw") == "Ok."
        if not success:
            print(f"    FAILED: {response}", file=sys.stderr)
            continue
        print(f"    OK: added (info_hash {manifest['info_hash']})")
        manifest["qbit_added"] = True
        manifest["qbit_save_path"] = save_path
        save_manifest(dirname, manifest)

    return 0


if __name__ == "__main__":
    sys.exit(main())
