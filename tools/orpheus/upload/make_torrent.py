#!/usr/bin/env python3
"""Create a .torrent file for an album directory, registered to OPS.

Pure-Python bencode + SHA1; no mktorrent dependency. Hashes the LOCAL files
(workhorse-side, in ~/Downloads after normalize). Those are byte-identical to
the rsynced copies on natto, so qBit will accept the torrent against the natto
savePath with skip_checking=true.

Reads OPS_ANNOUNCE_URL from secrets.env. Writes:
  tools/orpheus/upload/torrents/<album-dir>.torrent
  manifest: { 'torrent_path': ..., 'info_hash': ..., 'piece_length': ..., 'total_bytes': ... }
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

STATE_DIR = Path(__file__).parent / "state"
TORRENT_DIR = Path(__file__).parent / "torrents"
SECRETS_PATH = Path(__file__).parent.parent / "secrets.env"
SOURCE_FLAG = "OPS"


def load_env(env_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not env_file.exists():
        return out
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def bencode(obj) -> bytes:
    if isinstance(obj, int):
        return f"i{obj}e".encode("ascii")
    if isinstance(obj, bytes):
        return f"{len(obj)}:".encode("ascii") + obj
    if isinstance(obj, str):
        b = obj.encode("utf-8")
        return f"{len(b)}:".encode("ascii") + b
    if isinstance(obj, list):
        return b"l" + b"".join(bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        # BEP-3: dict keys are byte strings, sorted in raw byte order.
        def sort_key(item):
            k = item[0]
            return k.encode("utf-8") if isinstance(k, str) else k
        parts = b"d"
        for k, v in sorted(obj.items(), key=sort_key):
            parts += bencode(k) + bencode(v)
        return parts + b"e"
    raise TypeError(f"cannot bencode {type(obj).__name__}")


def pick_piece_length(total_bytes: int) -> int:
    """Target ~1500–3000 pieces; round to next power of two; clamp to [16 KiB, 16 MiB]."""
    target = max(total_bytes // 2000, 1)
    log2 = max(14, min(24, (target - 1).bit_length()))
    return 1 << log2


def hash_pieces(files: list[Path], piece_length: int) -> bytes:
    """Concatenated SHA1 of every piece, in file order."""
    pieces = bytearray()
    buf = bytearray()
    for f in files:
        with f.open("rb") as fh:
            while True:
                want = piece_length - len(buf)
                chunk = fh.read(want)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) == piece_length:
                    pieces.extend(hashlib.sha1(bytes(buf)).digest())
                    buf.clear()
    if buf:
        pieces.extend(hashlib.sha1(bytes(buf)).digest())
    return bytes(pieces)


def make_torrent(source_dir: Path, announce_url: str, output_path: Path) -> dict:
    files = sorted(p for p in source_dir.rglob("*") if p.is_file())
    if not files:
        raise ValueError(f"no files in {source_dir}")
    total = sum(p.stat().st_size for p in files)
    piece_length = pick_piece_length(total)

    pieces = hash_pieces(files, piece_length)

    file_entries = [
        {"length": p.stat().st_size, "path": list(p.relative_to(source_dir).parts)}
        for p in files
    ]
    info: dict = {
        "name": source_dir.name,
        "piece length": piece_length,
        "pieces": pieces,
        "private": 1,
        "source": SOURCE_FLAG,
    }
    if len(files) == 1:
        info["length"] = file_entries[0]["length"]
    else:
        info["files"] = file_entries

    torrent: dict = {
        "announce": announce_url,
        "created by": "nthncrtr-orpheus-tool/0.1",
        "creation date": int(time.time()),
        "info": info,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(bencode(torrent))
    info_hash = hashlib.sha1(bencode(info)).hexdigest()
    return {
        "torrent_path": str(output_path),
        "info_hash": info_hash,
        "piece_length": piece_length,
        "piece_count": len(pieces) // 20,
        "total_bytes": total,
    }


def load_manifest(dirname: str) -> dict:
    p = STATE_DIR / f"{dirname}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_manifest(dirname: str, manifest: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / f"{dirname}.json").write_text(json.dumps(manifest, indent=2) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", type=Path, nargs="+", help="one or more normalized album directories")
    ap.add_argument("--force", action="store_true",
                    help="recreate even if manifest already has torrent_path")
    args = ap.parse_args()

    env = load_env(SECRETS_PATH)
    announce = env.get("OPS_ANNOUNCE_URL", "")
    if not announce:
        print(f"OPS_ANNOUNCE_URL missing in {SECRETS_PATH}", file=sys.stderr)
        return 1

    for path in args.paths:
        dirname = path.name
        if not path.is_dir():
            print(f"  {dirname}: not a directory, skipping", file=sys.stderr)
            continue
        manifest = load_manifest(dirname)
        if "torrent_path" in manifest and not args.force:
            print(f"  {dirname}: already has torrent at {manifest['torrent_path']}; "
                  f"skipping (use --force to recreate)")
            continue
        output = TORRENT_DIR / f"{dirname}.torrent"
        print(f"  {dirname}: hashing pieces...")
        info = make_torrent(path, announce, output)
        info["torrent_path"] = str(output)
        manifest.update(info)
        save_manifest(dirname, manifest)
        size_mb = info["total_bytes"] / 1024 / 1024
        ps_kb = info["piece_length"] // 1024
        print(f"    → {output.name} ({size_mb:.0f} MB, {info['piece_count']} × {ps_kb} KiB pieces)")
        print(f"      info_hash: {info['info_hash']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
