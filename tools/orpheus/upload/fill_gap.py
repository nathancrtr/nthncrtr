#!/usr/bin/env python3
"""Fill a format gap in an existing OPS torrent group.

Inputs: a FLAC album dir on natto, an OPS group id, and the formats to fill.
Output: new MP3 torrent(s) attached to the existing group, seeding from natto.

  python fill_gap.py \\
      --natto-dir "Fela Kuti & Afrika 70 - Zombie (2001) [FLAC]" \\
      --groupid 491525 \\
      --formats 320

Pipeline (per requested format):
  1. Query OPS for the group to lift the existing FLAC torrent's edition
     fields (remasterYear/Title/Label/CatNum) — so our upload attaches to
     the SAME edition rather than spawning a duplicate edition row.
  2. SSH-execute ffmpeg on natto, transcoding the FLAC files into a new
     dir at /mnt/media/seed-only/<Artist> - <Album> (<Year>) [WEB <FMT>]/.
     The seed-only path is enforced (assertion) — we NEVER write under
     /mnt/media/music/, which is Navidrome's library and must not be
     polluted with duplicate-format MP3s.
  3. Rsync the new MP3 dir down to a workhorse temp ($HOME/Downloads/_fill_gap/)
     so the existing make_torrent.py can hash piece SHA1s locally.
  4. make_torrent.py builds the .torrent.
  5. ops_upload.py --groupid <N> --apply posts to OPS, attaching to the
     existing group + edition.
  6. qbit_add.py adds the torrent to qBit on natto (qBit hash-checks against
     the already-in-place files from step 2 and goes straight to seeding).
  7. Workhorse temp dir is cleaned up.

Default is dry-run; --apply is required for any mutating step.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

STAGE_DIR = Path(__file__).parent
ORPHEUS_DIR = STAGE_DIR.parent
sys.path.insert(0, str(STAGE_DIR))
sys.path.insert(0, str(ORPHEUS_DIR))
from normalize import safe  # noqa: E402
from check_availability import load_env  # noqa: E402

PY = sys.executable
NATTO_HOST = "natto"
NATTO_MUSIC = "/mnt/media/music"
NATTO_SEED_ONLY = "/mnt/media/seed-only"
WORKHORSE_TEMP_ROOT = Path.home() / "Downloads" / "_fill_gap"
SECRETS_PATH = ORPHEUS_DIR / "secrets.env"
OPS_AJAX = "https://orpheus.network/ajax.php"

# Per-format: how to invoke lame via ffmpeg, the OPS encoding label, and the
# format token used in the dir-name suffix (e.g. "[CD 320]"). The media half
# of that suffix comes from the OPS edition template, not from here.
FORMAT_SPECS = {
    "320": {
        "lame_args": ["-c:a", "libmp3lame", "-b:a", "320k"],
        "ops_encoding": "320",
        "format_token": "320",
    },
    "V0": {
        "lame_args": ["-c:a", "libmp3lame", "-q:a", "0"],
        "ops_encoding": "V0 (VBR)",
        "format_token": "V0",
    },
}


def preflight_ssh() -> None:
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         NATTO_HOST, "true"],
        capture_output=True,
    )
    if r.returncode != 0:
        print(f"ERROR: 'ssh {NATTO_HOST}' failed in non-interactive mode "
              f"(rc={r.returncode}).", file=sys.stderr)
        if r.stderr:
            print(f"  stderr: {r.stderr.decode(errors='replace').strip()}",
                  file=sys.stderr)
        print("\nFix:  ssh-add ~/.ssh/id_ed25519", file=sys.stderr)
        sys.exit(2)


def ops_get_group(api_key: str, groupid: int) -> dict:
    """Fetch action=torrentgroup&id=<N>; return the unwrapped response."""
    url = OPS_AJAX + "?" + urllib.parse.urlencode(
        {"action": "torrentgroup", "id": str(groupid)})
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {api_key}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("status") != "success":
        raise RuntimeError(f"OPS action=torrentgroup failed: {data}")
    return data["response"]


def pick_edition_template(group_resp: dict) -> dict:
    """Pick the existing FLAC torrent we'll attach to as the edition template.

    Preference: WEB FLAC (matches the gap-fill source convention), then any
    FLAC, then anything (last resort — the group must have *something* or
    this isn't a sane gap-fill).
    """
    torrents = group_resp.get("torrents", [])
    if not torrents:
        raise RuntimeError("group has no torrents — nothing to attach to")

    def rank(t):
        is_flac = (t.get("format") == "FLAC", )
        is_web = (t.get("media") == "WEB", )
        seeders = -int(t.get("seeders") or 0)
        return (not is_flac[0], not is_web[0], seeders)

    return min(torrents, key=rank)


def remote_dir_exists(path: str) -> bool:
    r = subprocess.run(
        ["ssh", NATTO_HOST, f"test -d {shlex.quote(path)}"],
        capture_output=True)
    return r.returncode == 0


def remote_list_flacs(album_dir: str) -> list[str]:
    """Return the absolute paths of *.flac files anywhere under album_dir.

    Recursive on purpose — multi-disc releases live in subdirs like
    'Album/Disc 1/01 Track.flac' and need to be counted alongside flat
    layouts.
    """
    r = subprocess.run(
        ["ssh", NATTO_HOST,
         f"find {shlex.quote(album_dir)} -name '*.flac' -type f -print"],
        capture_output=True, check=True)
    return sorted(l for l in r.stdout.decode().splitlines() if l.strip())


def build_target_name(artist: str, album: str, year: str | None,
                      media: str, format_label: str,
                      edition_title: str = "") -> str:
    """Match normalize.build_dirname() conventions so the rest of the pipeline
    recognizes the dir's format from its name.

    Includes the actual media (CD/WEB/Vinyl/etc.) so non-WEB sources don't
    end up labeled as WEB transcodes (which would either spawn a duplicate
    edition row on OPS or get flagged as misrepresentation).
    """
    year_part = f" ({year})" if year else ""
    edition_part = f" ({safe(edition_title)})" if edition_title else ""
    return (f"{safe(artist)} - {safe(album)}{year_part}{edition_part} "
            f"[{media} {format_label}]")


def remote_transcode(source_dir: str, target_dir: str,
                     lame_args: list[str]) -> None:
    """SSH-execute ffmpeg on natto: transcode every *.flac in source_dir
    into target_dir, plus copy any cover.* sidecar. Enforces that
    target_dir lives under /mnt/media/seed-only."""
    if not target_dir.startswith(NATTO_SEED_ONLY + "/"):
        raise ValueError(
            f"safety: target_dir must be under {NATTO_SEED_ONLY}/, got {target_dir!r}")

    # Bash-on-natto: recursive find of .flac files (handles multi-disc albums
    # like "Album/Disc 1/01 Track.flac"), mirroring the subdir structure in
    # the output. ffmpeg flags: -map 0 -c:v copy preserves embedded art;
    # -id3v2_version 3 gives broadest player compat; -map_metadata 0 carries
    # tags across; -nostdin so it doesn't try to grab a tty.
    lame = " ".join(shlex.quote(a) for a in lame_args)
    remote_script = f"""set -euo pipefail
src={shlex.quote(source_dir)}
dst={shlex.quote(target_dir)}
mkdir -p "$dst"
count=0
# Recursive find — supports both flat dirs and multi-disc layouts.
while IFS= read -r -d '' f; do
  rel="${{f#$src/}}"
  out="$dst/${{rel%.flac}}.mp3"
  mkdir -p "$(dirname "$out")"
  ffmpeg -hide_banner -loglevel error -nostdin \\
    -i "$f" \\
    -map 0 -map_metadata 0 -id3v2_version 3 \\
    -c:v copy {lame} \\
    "$out"
  count=$((count + 1))
done < <(find "$src" -type f -name '*.flac' -print0)
echo "transcoded $count tracks → $dst"
if [ "$count" -eq 0 ]; then
  echo "ERROR: no .flac files found under $src" >&2
  exit 2
fi
# Cover sidecars (jpg/jpeg/png/webp/folder.*): copy whatever's at the top level.
shopt -s nullglob
for c in "$src"/cover.* "$src"/folder.*; do
  cp -p "$c" "$dst/"
  echo "copied sidecar: $(basename "$c")"
done
"""
    r = subprocess.run(["ssh", NATTO_HOST, "bash", "-s"],
                       input=remote_script.encode(),
                       capture_output=False)
    if r.returncode != 0:
        raise RuntimeError(f"remote transcode failed (rc={r.returncode})")


def rsync_from_natto(remote_path: str, local_path: Path) -> None:
    """rsync the just-transcoded dir down to a local temp dir (for hashing).

    Plain `-ah` only — macOS still ships rsync 2.6.9, which doesn't understand
    --info or any of the newer progress flags. The transfers are small (MP3
    album = ~50–100MB) so progress output isn't needed.
    """
    local_path.parent.mkdir(parents=True, exist_ok=True)
    src = f"{NATTO_HOST}:{shlex.quote(remote_path + '/')}"
    r = subprocess.run(
        ["rsync", "-ah", src, f"{local_path}/"],
        capture_output=False)
    if r.returncode != 0:
        raise RuntimeError(f"rsync pull failed (rc={r.returncode})")


def run_stage(label: str, cmd: list) -> None:
    print(f"\n----- {label} -----")
    r = subprocess.run([str(c) for c in cmd])
    if r.returncode != 0:
        raise RuntimeError(f"{label} failed (rc={r.returncode})")


def fill_one_format(api_key: str,
                    natto_source_dir: str,
                    target_format: str,
                    groupid: int,
                    edition: dict,
                    artist: str, album: str, year: str | None,
                    apply: bool) -> None:
    spec = FORMAT_SPECS[target_format]
    # Lift everything we can from the existing OPS edition so the new
    # torrent attaches to the *same* edition row instead of spawning a
    # duplicate. Edition matching on OPS keys off
    # (remaster_year, remaster_title, remaster_label, remaster_catnum, media).
    media = edition.get("media") or "WEB"
    edition_title = edition.get("remasterTitle") or ""
    edition_year = str(edition.get("remasterYear") or year or "")
    edition_label = edition.get("remasterRecordLabel") or ""
    edition_catnum = edition.get("remasterCatalogueNumber") or ""

    target_name = build_target_name(artist, album, year, media,
                                    spec["format_token"], edition_title)
    natto_target_dir = f"{NATTO_SEED_ONLY}/{target_name}"
    workhorse_temp_dir = WORKHORSE_TEMP_ROOT / target_name

    print(f"\n=== Gap-fill: {target_format} → group {groupid} ===")
    print(f"  source (natto):    {natto_source_dir}")
    print(f"  target (natto):    {natto_target_dir}")
    print(f"  workhorse temp:    {workhorse_temp_dir}")
    print(f"  edition template:  media={media}  "
          f"remaster_year={edition_year or '(none)'}  "
          f"remaster_title={edition_title or '(none)'}  "
          f"label={edition_label or '(none)'}  "
          f"catnum={edition_catnum or '(none)'}")

    if not apply:
        print("\n(dry-run; --apply to execute)")
        return

    # Idempotency: if the target dir already exists on natto with the
    # right file count, skip the transcode and continue. Lets us re-run
    # after a partial failure without re-doing the slow step.
    if remote_dir_exists(natto_target_dir):
        existing_mp3s = subprocess.run(
            ["ssh", NATTO_HOST,
             f"find {shlex.quote(natto_target_dir)} -name '*.mp3' "
             f"-type f | wc -l"],
            capture_output=True, check=True).stdout.decode().strip()
        source_flacs = len(remote_list_flacs(natto_source_dir))
        if int(existing_mp3s) == source_flacs and source_flacs > 0:
            print(f"  transcode: already present ({existing_mp3s} MP3s); skipping")
        else:
            raise RuntimeError(
                f"natto target dir exists with {existing_mp3s} MP3s vs "
                f"{source_flacs} source FLACs — refusing to overwrite. "
                f"Inspect/clean manually then re-run.")
    else:
        print("\n----- remote transcode (natto) -----")
        remote_transcode(natto_source_dir, natto_target_dir, spec["lame_args"])

    # Pull to workhorse temp purely for hashing. The seeded files stay on natto.
    print("\n----- pull to workhorse temp (for hashing only) -----")
    if workhorse_temp_dir.exists():
        print(f"  {workhorse_temp_dir} already exists; rsync will update in place")
    rsync_from_natto(natto_target_dir, workhorse_temp_dir)

    try:
        run_stage("STAGE 5: make_torrent",
                  [PY, STAGE_DIR / "make_torrent.py", workhorse_temp_dir])

        run_stage(f"STAGE 6: ops_upload (--groupid {groupid})",
                  [PY, STAGE_DIR / "ops_upload.py", workhorse_temp_dir,
                   "--apply", "--groupid", str(groupid),
                   "--media", media,
                   "--edition-title", edition_title,
                   "--edition-year", edition_year,
                   "--label", edition_label,
                   "--catnum", edition_catnum,
                   "--release-desc",
                   f"Transcoded from FLAC with LAME via ffmpeg "
                   f"({'-b:a 320k' if target_format == '320' else '-q:a 0'})."])

        run_stage("STAGE 7: qbit_add",
                  [PY, STAGE_DIR / "qbit_add.py", workhorse_temp_dir])
    finally:
        # Always clean up the temp dir — files on natto are authoritative.
        if workhorse_temp_dir.exists():
            print(f"\n  cleaning up workhorse temp: {workhorse_temp_dir}")
            shutil.rmtree(workhorse_temp_dir)


def parse_formats_arg(s: str) -> list[str]:
    out = []
    for tok in s.split(","):
        tok = tok.strip().upper()
        if tok in FORMAT_SPECS:
            out.append(tok)
        else:
            raise argparse.ArgumentTypeError(
                f"unknown format {tok!r}; valid: {','.join(FORMAT_SPECS)}")
    if not out:
        raise argparse.ArgumentTypeError("at least one format required")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--natto-dir", required=True,
                    help="album dir name under /mnt/media/music on natto. "
                         "Just the basename (no /mnt/media/music prefix).")
    ap.add_argument("--groupid", type=int, required=True,
                    help="OPS group id to attach the new torrent(s) to")
    ap.add_argument("--formats", type=parse_formats_arg, default=["320", "V0"],
                    help="comma-separated formats to fill (default: 320,V0)")
    ap.add_argument("--artist", help="override artist (default: from --natto-dir)")
    ap.add_argument("--album", help="override album (default: from --natto-dir)")
    ap.add_argument("--year", help="override year (default: from --natto-dir)")
    ap.add_argument("--apply", action="store_true",
                    help="actually do the work; default is dry-run")
    args = ap.parse_args()

    natto_source_dir = f"{NATTO_MUSIC}/{args.natto_dir}"
    api_key = load_env(SECRETS_PATH).get("OPS_API_KEY", "")
    if not api_key:
        print(f"OPS_API_KEY missing in {SECRETS_PATH}", file=sys.stderr)
        return 1

    if args.apply:
        preflight_ssh()
        if not remote_dir_exists(natto_source_dir):
            print(f"ERROR: source dir not found on natto: {natto_source_dir}",
                  file=sys.stderr)
            return 1

    # Resolve artist/album/year. Prefer explicit overrides; otherwise parse the dir name.
    if args.artist and args.album:
        artist, album, year = args.artist, args.album, args.year
    else:
        # Reuse the scan_gaps parser since the dir names follow the same conventions.
        sys.path.insert(0, str(ORPHEUS_DIR))
        from scan_gaps import parse_dir  # noqa: E402
        parsed = parse_dir(args.natto_dir)
        if parsed is None:
            print(f"ERROR: couldn't parse artist/album/year from "
                  f"{args.natto_dir!r}. Pass --artist / --album / --year explicitly.",
                  file=sys.stderr)
            return 1
        artist, album, year = parsed

    print(f"=== gap-fill plan ===")
    print(f"  album:   {artist} — {album} ({year or 'no year'})")
    print(f"  source:  {natto_source_dir}")
    print(f"  group:   https://orpheus.network/torrents.php?id={args.groupid}")
    print(f"  formats: {', '.join(args.formats)}")

    # One OPS round-trip to lift edition fields from the group's existing FLAC.
    print("\n----- fetching group details from OPS -----")
    group_resp = ops_get_group(api_key, args.groupid)
    edition = pick_edition_template(group_resp)
    print(f"  using edition template from torrent {edition.get('id')}: "
          f"{edition.get('format')} {edition.get('encoding')} "
          f"({edition.get('media')})")

    # Sanity: if any of the requested formats *already* exists in this group,
    # warn so the operator can pick a different format or different group.
    have_combos = {(t.get("format"), t.get("encoding")) for t in group_resp["torrents"]}
    for fmt in args.formats:
        spec = FORMAT_SPECS[fmt]
        if ("MP3", spec["ops_encoding"]) in have_combos:
            print(f"  WARNING: group already has MP3 {spec['ops_encoding']} — "
                  f"a fresh upload may be flagged as a duplicate", file=sys.stderr)

    for fmt in args.formats:
        fill_one_format(
            api_key=api_key,
            natto_source_dir=natto_source_dir,
            target_format=fmt,
            groupid=args.groupid,
            edition=edition,
            artist=artist, album=album, year=year,
            apply=args.apply,
        )

    if not args.apply:
        print("\n(dry-run; re-run with --apply to execute)")
    else:
        print("\n=== DONE ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
