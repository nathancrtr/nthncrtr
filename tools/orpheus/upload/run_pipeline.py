#!/usr/bin/env python3
"""End-to-end pipeline harness: workhorse album dir → OPS torrent + natto seed.

Orchestrates the 7 per-stage scripts. Each stage is idempotent via per-album
manifests at tools/orpheus/upload/state/<dirname>.json, so re-running this
harness re-uses prior stage outputs and only does new work.

Pass ALL format dirs for ONE album in a single invocation (FLAC + 320 + V0).
The FLAC, if present, is uploaded to OPS first to create the group; other
formats then attach to the same group.

  Dry-run (default):
    python run_pipeline.py "~/Downloads/A Weather - Cove (FLAC)" \
                           "~/Downloads/A Weather - Cove (mp3 320)" \
                           "~/Downloads/A Weather - Cove (mp3 v0)"

  Apply (actually mutate, transfer, upload, seed):
    python run_pipeline.py --apply --tags "indie.pop, indie.folk, indie.rock" \
                           <same dirs...>
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Same dir as this script:
STAGE_DIR = Path(__file__).parent
INSPECT = STAGE_DIR / "inspect.py"
NORMALIZE = STAGE_DIR / "normalize.py"
TRANSFER = STAGE_DIR / "transfer.py"
ART_UPLOAD = STAGE_DIR / "art_upload.py"
MAKE_TORRENT = STAGE_DIR / "make_torrent.py"
OPS_UPLOAD = STAGE_DIR / "ops_upload.py"
QBIT_ADD = STAGE_DIR / "qbit_add.py"

PY = sys.executable  # use the same interpreter (the venv)

# Mirror of normalize.py's build_dirname() — duplicated here so the harness
# can predict post-normalize paths *before* normalize runs, so it knows what
# paths to feed into stages 3+. Kept tiny on purpose; if normalize's naming
# scheme changes, update both.
def predict_normalize_name(report: dict) -> str:
    fmt, enc = report["format"], report["encoding"]
    if fmt == "FLAC":
        suffix = "FLAC 24bit" if enc == "24bit Lossless" else "FLAC"
    elif fmt == "MP3":
        if enc == "320":
            suffix = "320"
        elif enc.startswith("V0"):
            suffix = "V0"
        elif enc.startswith("V2"):
            suffix = "V2"
        else:
            suffix = enc.replace("(VBR)", "VBR").replace(" ", "")
    else:
        suffix = f"{fmt} {enc}"
    return f"{report['artist']} - {report['album']} ({report['year']}) [WEB {suffix}]"


def inspect_dir(path: Path) -> dict:
    r = subprocess.run([PY, str(INSPECT), "--json", str(path)],
                       capture_output=True, check=True)
    return json.loads(r.stdout.decode())[0]


def run_stage(label: str, cmd: list) -> bool:
    print(f"\n----- {label} -----")
    r = subprocess.run([str(c) for c in cmd])
    if r.returncode != 0:
        print(f"  ! {label} failed (rc={r.returncode})", file=sys.stderr)
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", type=Path, nargs="+",
                    help="all format dirs for ONE album (FLAC + 320 + V0)")
    ap.add_argument("--apply", action="store_true",
                    help="actually run the pipeline. Without this, prints the planned actions.")
    ap.add_argument("--tags", default="",
                    help="OPS tags, comma-separated (required when this album is new on OPS)")
    ap.add_argument("--label", default="", help="record label (for OPS edition info)")
    ap.add_argument("--catnum", default="", help="catalogue number (for OPS edition info)")
    ap.add_argument("--releasetype", type=int, default=1,
                    help="OPS release type integer (default 1 = Album)")
    ap.add_argument("--release-desc", default="Sourced from Bandcamp.",
                    help="per-torrent BBCode description for OPS")
    ap.add_argument("--album-desc", default="",
                    help="album-level BBCode description (defaults to auto-generated tracklist)")
    ap.add_argument("--skip-qbit", action="store_true",
                    help="don't add to qBittorrent (e.g., if you want to seed elsewhere)")
    args = ap.parse_args()

    # Inspect every dir; verify they all belong to the same album.
    reports = []
    for p in args.paths:
        if not p.is_dir():
            print(f"not a directory: {p}", file=sys.stderr)
            return 1
        reports.append(inspect_dir(p))

    album_keys = {(r["artist"], r["album"], r["year"]) for r in reports}
    if len(album_keys) > 1:
        print(f"ERROR: provided dirs span multiple albums: {sorted(album_keys)}",
              file=sys.stderr)
        return 1
    artist, album, year = next(iter(album_keys))
    print(f"=== {artist} - {album} ({year}) ===")
    for r in reports:
        print(f"  format: {r['format']} {r['encoding']:>18}  ({r['track_count']} tracks, "
              f"{r['total_size_bytes']/1024/1024:.0f} MB)")

    target_names = [predict_normalize_name(r) for r in reports]
    new_paths = [p.parent / name for p, name in zip(args.paths, target_names)]

    if not args.apply:
        # Show what normalize would do (its own dry-run); skip stages 3+.
        run_stage("STAGE 2: normalize (dry-run)", [PY, NORMALIZE, *args.paths])
        print("\nWould then run stages 3–7 against post-normalize paths:")
        for np in new_paths:
            print(f"  {np}")
        print("\n(re-run with --apply to execute)")
        return 0

    # Stage 2: normalize (mutating).
    if not run_stage("STAGE 2: normalize", [PY, NORMALIZE, "--apply", *args.paths]):
        return 1
    for np in new_paths:
        if not np.is_dir():
            print(f"ERROR: expected post-normalize dir not found: {np}", file=sys.stderr)
            return 1

    # Stage 3: rsync to natto.
    if not run_stage("STAGE 3: transfer to natto", [PY, TRANSFER, *new_paths]):
        return 1

    # Stage 4: catbox upload of cover.jpg (dedupes by content md5).
    if not run_stage("STAGE 4: cover.jpg upload", [PY, ART_UPLOAD, *new_paths]):
        return 1

    # Stage 5: create per-format .torrent files.
    if not run_stage("STAGE 5: make_torrent", [PY, MAKE_TORRENT, *new_paths]):
        return 1

    # Stage 6: OPS upload. FLAC first (creates group); others attach via groupid.
    flac_paths = [p for p, r in zip(new_paths, reports) if r["format"] == "FLAC"]
    other_paths = [p for p, r in zip(new_paths, reports) if r["format"] != "FLAC"]
    for path in flac_paths + other_paths:
        cmd = [PY, OPS_UPLOAD, str(path), "--apply",
               "--releasetype", str(args.releasetype),
               "--release-desc", args.release_desc]
        if args.tags:
            cmd.extend(["--tags", args.tags])
        if args.label:
            cmd.extend(["--label", args.label])
        if args.catnum:
            cmd.extend(["--catnum", args.catnum])
        if args.album_desc:
            cmd.extend(["--album-desc", args.album_desc])
        if not run_stage(f"STAGE 6: ops_upload ({path.name})", cmd):
            return 1

    # Stage 7: add to qBit on natto so it seeds.
    if not args.skip_qbit:
        if not run_stage("STAGE 7: qbit_add", [PY, QBIT_ADD, *new_paths]):
            return 1

    print(f"\n=== DONE: {artist} - {album} ({year}) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
