#!/usr/bin/env python3
"""End-to-end pipeline harness: workhorse album dir → OPS torrent + natto seed.

Orchestrates the 7 per-stage scripts. Each stage is idempotent via per-album
manifests at tools/orpheus/upload/state/<dirname>.json, so re-running this
harness re-uses prior stage outputs and only does new work.

Two ways to invoke:

  Explicit paths — pass ALL format dirs for ONE album in a single invocation
  (FLAC + 320 + V0). The FLAC, if present, is uploaded to OPS first to create
  the group; other formats then attach to the same group.

    python run_pipeline.py "~/Downloads/A Weather - Cove (FLAC)" \
                           "~/Downloads/A Weather - Cove (mp3 320)" \
                           "~/Downloads/A Weather - Cove (mp3 v0)"

    python run_pipeline.py --apply --tags "indie.pop, indie.folk, indie.rock" \
                           <same dirs...>

  Scan a parent directory — auto-group format dirs by (artist, album, year)
  read from track tags (not by dir name), then process each ready triplet.
  Prompts once per album for tags; press Enter to skip an album.

    python run_pipeline.py --scan ~/Downloads              # plan only
    python run_pipeline.py --scan ~/Downloads --apply      # interactive run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

STAGE_DIR = Path(__file__).parent
NORMALIZE = STAGE_DIR / "normalize.py"
TRANSFER = STAGE_DIR / "transfer.py"
ART_UPLOAD = STAGE_DIR / "art_upload.py"
MAKE_TORRENT = STAGE_DIR / "make_torrent.py"
OPS_UPLOAD = STAGE_DIR / "ops_upload.py"
QBIT_ADD = STAGE_DIR / "qbit_add.py"

PY = sys.executable  # use the same interpreter (the venv)
NATTO_HOST = "natto"

# Reuse normalize.py's build_dirname() directly — previously a parallel
# `predict_normalize_name()` lived here and drifted (it didn't apply the
# rstrip(".") that `safe()` does, so artists like "Kaho Matsui & i.v."
# computed a name with a trailing dot that didn't match what normalize
# actually produced, and the harness then failed the post-normalize
# existence check).
sys.path.insert(0, str(STAGE_DIR))
from normalize import build_dirname  # noqa: E402
from inspect import inspect_album  # noqa: E402


def preflight_ssh() -> None:
    """Fail fast if `ssh natto` doesn't work non-interactively.

    Stages 3 (transfer) and 7 (qbit_add) shell out to ssh without a TTY,
    so a missing agent identity manifests as a mid-pipeline crash after
    normalize has already mutated the source dirs. Catch it up front.
    """
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         NATTO_HOST, "true"],
        capture_output=True,
    )
    if r.returncode == 0:
        return
    print(f"ERROR: 'ssh {NATTO_HOST}' failed in non-interactive mode "
          f"(rc={r.returncode}).", file=sys.stderr)
    if r.stderr:
        print(f"  stderr: {r.stderr.decode(errors='replace').strip()}",
              file=sys.stderr)
    print(f"\nStages 3 (transfer) and 7 (qbit_add) shell out to ssh "
          f"{NATTO_HOST} without a TTY,\nso the SSH agent needs the key "
          f"authorized on {NATTO_HOST} loaded.\n\n  Fix:  "
          f"ssh-add ~/.ssh/id_ed25519", file=sys.stderr)
    sys.exit(2)


def run_stage(label: str, cmd: list) -> bool:
    print(f"\n----- {label} -----")
    r = subprocess.run([str(c) for c in cmd])
    if r.returncode != 0:
        print(f"  ! {label} failed (rc={r.returncode})", file=sys.stderr)
        return False
    return True


def process_album(paths: list, reports: list, args) -> bool:
    """Run stages 2–7 for one album. Returns True on success."""
    artist = reports[0]["artist"]
    album = reports[0]["album"]
    year = reports[0]["year"]
    print(f"\n=== {artist} - {album} ({year}) ===")
    for r in reports:
        print(f"  format: {r['format']} {r['encoding']:>18}  "
              f"({r['track_count']} tracks, "
              f"{r['total_size_bytes']/1024/1024:.0f} MB)")

    target_names = [build_dirname(r) for r in reports]
    new_paths = [p.parent / name for p, name in zip(paths, target_names)]

    if not args.apply:
        run_stage("STAGE 2: normalize (dry-run)", [PY, NORMALIZE, *paths])
        print("\nWould then run stages 3–7 against post-normalize paths:")
        for np in new_paths:
            print(f"  {np}")
        return True

    if not run_stage("STAGE 2: normalize",
                     [PY, NORMALIZE, "--apply", *paths]):
        return False
    for np in new_paths:
        if not np.is_dir():
            print(f"ERROR: expected post-normalize dir not found: {np}",
                  file=sys.stderr)
            return False

    if not run_stage("STAGE 3: transfer to natto",
                     [PY, TRANSFER, *new_paths]):
        return False
    if not run_stage("STAGE 4: cover upload", [PY, ART_UPLOAD, *new_paths]):
        return False
    if not run_stage("STAGE 5: make_torrent", [PY, MAKE_TORRENT, *new_paths]):
        return False

    # FLAC first (creates the OPS group); others attach via groupid.
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
            return False

    if not args.skip_qbit:
        if not run_stage("STAGE 7: qbit_add", [PY, QBIT_ADD, *new_paths]):
            return False

    print(f"\n=== DONE: {artist} - {album} ({year}) ===")
    return True


# ---------- scan mode helpers ----------

def scan_parent(parent: Path) -> tuple[dict, list]:
    """Walk parent for album dirs; group by tag-derived (artist, album, year).

    Returns (groups, skipped). groups[key] is a list of (path, report).
    skipped is a list of (path, reason) for child dirs that looked like an
    attempt at an album but couldn't be parsed (the truly empty / non-music
    dirs are silently ignored to keep the output clean).
    """
    groups: dict = {}
    skipped: list = []
    for child in sorted(parent.iterdir()):
        if not child.is_dir():
            continue
        report = inspect_album(child)
        if "error" in report:
            # Don't surface every non-album dir; only ones that looked
            # like attempts. "no audio files found" is the common
            # "ignore this, it's clearly not a music dir" case.
            if report["error"] != "no audio files found":
                skipped.append((child, report["error"]))
            continue
        key = (report["artist"], report["album"], report["year"])
        groups.setdefault(key, []).append((child, report))
    return groups, skipped


def classify(group: list) -> str:
    """Return 'ready', 'flac-only', 'mp3-only', or 'duplicate-flac'."""
    formats = [r["format"] for _, r in group]
    fc = formats.count("FLAC")
    mc = formats.count("MP3")
    if fc > 1:
        return "duplicate-flac"
    if fc == 1 and mc >= 1:
        return "ready"
    if fc == 1:
        return "flac-only"
    return "mp3-only"


def fmt_album_key(key: tuple) -> str:
    return f"{key[0]} - {key[1]} ({key[2]})"


def fmt_formats(group: list) -> str:
    """Render the formats in a group as 'FLAC + 320 + V0'-style."""
    def label(r):
        f, e = r["format"], r["encoding"]
        if f == "FLAC":
            return "FLAC 24bit" if e == "24bit Lossless" else "FLAC"
        if e == "320":
            return "320"
        if e.startswith("V0"):
            return "V0"
        if e.startswith("V2"):
            return "V2"
        return e
    # FLAC first if present; then MP3 by bitrate descending (320, V0, V2).
    bits = [label(r) for _, r in group]
    order = {"FLAC 24bit": 0, "FLAC": 1, "320": 2, "V0": 3, "V2": 4}
    bits.sort(key=lambda b: order.get(b, 99))
    return " + ".join(bits)


def scan_mode(parent: Path, args) -> int:
    if not parent.is_dir():
        print(f"--scan: not a directory: {parent}", file=sys.stderr)
        return 2

    groups, skipped = scan_parent(parent)
    classified = {k: classify(g) for k, g in groups.items()}
    ready = [(k, groups[k]) for k, cls in classified.items() if cls == "ready"]
    partial = [(k, groups[k], cls) for k, cls in classified.items() if cls != "ready"]

    print(f"Scanned {parent}, found:\n")
    if ready:
        print(f"ready ({len(ready)}):")
        for i, (key, g) in enumerate(ready, 1):
            print(f"  [{i}] {fmt_album_key(key)}")
            print(f"      {fmt_formats(g)}")
    if partial:
        print(f"\npartial / non-triplet ({len(partial)}):")
        for key, g, cls in partial:
            print(f"  - {fmt_album_key(key)}  [{cls}]")
            print(f"      {fmt_formats(g)}")
    if skipped:
        print(f"\nskipped ({len(skipped)}):")
        for path, reason in skipped:
            print(f"  - {path.name}: {reason}")
    if not ready and not partial and not skipped:
        print("  (no albums detected)")

    if not args.apply:
        if ready:
            print("\n(dry-run; re-run with --apply to process the ready albums)")
        return 0
    if not ready:
        print("\nnothing ready to process.")
        return 0

    print(f"\nProcessing {len(ready)} ready album(s). "
          f"Press Enter at the tag prompt to skip.")

    failed = 0
    processed = 0
    for i, (key, group) in enumerate(ready, 1):
        print(f"\n[{i}/{len(ready)}] {fmt_album_key(key)}")
        print(f"        {fmt_formats(group)}")
        try:
            tags_in = input("  OPS tags (comma-separated, empty to skip): ").strip()
        except EOFError:
            tags_in = ""
        if not tags_in:
            print("  (skipped)")
            continue
        args.tags = tags_in
        paths = [p for p, _ in group]
        reports = [r for _, r in group]
        if process_album(paths, reports, args):
            processed += 1
        else:
            failed += 1

    print(f"\n=== scan complete: processed={processed} failed={failed} "
          f"skipped={len(ready) - processed - failed} ===")
    return 1 if failed else 0


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", type=Path, nargs="*",
                    help="all format dirs for ONE album (FLAC + 320 + V0). "
                         "Mutually exclusive with --scan.")
    ap.add_argument("--scan", type=Path, metavar="PARENT_DIR",
                    help="auto-group all album dirs under PARENT_DIR by their "
                         "(artist, album, year) tags, then process each ready "
                         "triplet. Prompts per-album for tags.")
    ap.add_argument("--apply", action="store_true",
                    help="actually run the pipeline. Without this, prints the "
                         "planned actions.")
    ap.add_argument("--tags", default="",
                    help="OPS tags, comma-separated (required when this album "
                         "is new on OPS). Ignored in --scan mode (prompted per "
                         "album instead).")
    ap.add_argument("--label", default="",
                    help="record label (for OPS edition info)")
    ap.add_argument("--catnum", default="",
                    help="catalogue number (for OPS edition info)")
    ap.add_argument("--releasetype", type=int, default=1,
                    help="OPS release type integer (default 1 = Album)")
    ap.add_argument("--release-desc", default="Sourced from Bandcamp.",
                    help="per-torrent BBCode description for OPS")
    ap.add_argument("--album-desc", default="",
                    help="album-level BBCode description (defaults to "
                         "auto-generated tracklist)")
    ap.add_argument("--skip-qbit", action="store_true",
                    help="don't add to qBittorrent (e.g., if you want to seed "
                         "elsewhere)")
    args = ap.parse_args()

    if args.scan and args.paths:
        ap.error("--scan and positional paths are mutually exclusive")
    if not args.scan and not args.paths:
        ap.error("either --scan PARENT_DIR or positional paths are required")

    # Preflight ssh once if we're going to mutate; skips the cost on dry-run.
    if args.apply:
        preflight_ssh()

    if args.scan:
        return scan_mode(args.scan, args)

    # Explicit-paths mode: verify all dirs belong to the same album.
    reports = []
    for p in args.paths:
        r = inspect_album(p)
        if "error" in r:
            print(f"{p}: {r['error']}", file=sys.stderr)
            return 1
        reports.append(r)

    album_keys = {(r["artist"], r["album"], r["year"]) for r in reports}
    if len(album_keys) > 1:
        print(f"ERROR: provided dirs span multiple albums: {sorted(album_keys)}",
              file=sys.stderr)
        return 1

    ok = process_album(list(args.paths), reports, args)
    if not args.apply and ok:
        print("\n(re-run with --apply to execute)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
