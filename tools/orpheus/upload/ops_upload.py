#!/usr/bin/env python3
"""Upload a normalized album to Orpheus via action=upload.

Reads per-album manifest (image_url, torrent_path) and album metadata (via
inspect_album). For the first format of a given album, creates a new OPS group
and captures groupid. For subsequent formats of the same album, looks up the
groupid from a sibling manifest (matched by album key — dir name with
'[WEB FORMAT]' suffix stripped) and posts as an add-to-group upload.

Default is dry-run; --apply makes the actual POST. The most recent OPS request
+ response (sans binary) is dumped to state/<dirname>.last_ops_exchange.json
for debugging.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from album_inspect import inspect_album  # noqa: E402

STATE_DIR = Path(__file__).parent / "state"
SECRETS_PATH = Path(__file__).parent.parent / "secrets.env"
OPS_AJAX = "https://orpheus.network/ajax.php"

FORMAT_SUFFIX_RE = re.compile(r"\s*\[WEB [^\]]+\]\s*$")

# OPS release type ints — defaults to Album (1). Operator overrides with --releasetype.
RELEASETYPE_DEFAULT = 1


def load_env(env_file: Path) -> dict[str, str]:
    if not env_file.exists():
        return {}
    out: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def load_manifest(dirname: str) -> dict:
    p = STATE_DIR / f"{dirname}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_manifest(dirname: str, manifest: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / f"{dirname}.json").write_text(json.dumps(manifest, indent=2) + "\n")


def album_key(dirname: str) -> str:
    """e.g. 'A Weather - Cove (2008) [WEB FLAC]' → 'A Weather - Cove (2008)'"""
    return FORMAT_SUFFIX_RE.sub("", dirname).strip()


def find_existing_groupid(key: str) -> int | None:
    """Look for a sibling manifest (same album, different format) that already
    captured an ops_group_id from a prior upload."""
    if not STATE_DIR.exists():
        return None
    for p in STATE_DIR.glob("*.json"):
        if album_key(p.stem) != key:
            continue
        m = json.loads(p.read_text())
        if m.get("ops_group_id"):
            return int(m["ops_group_id"])
    return None


def build_tracklist_bbcode(report: dict) -> str:
    lines = ["[b]Tracklist[/b]", "[pre]"]
    for t in report["tracks"]:
        mins, secs = divmod(int(t["length_sec"]), 60)
        num = f"{t['num']:02d}" if t["num"] else "??"
        lines.append(f"{num}. {t['title']}  [{mins:02d}:{secs:02d}]")
    total_min, total_sec = divmod(int(report["total_length_sec"]), 60)
    lines.append("")
    lines.append(f"Total: {total_min:02d}:{total_sec:02d}  "
                 f"({report['total_size_bytes']/1024/1024:.1f} MB)")
    lines.append("[/pre]")
    return "\n".join(lines)


def build_new_group_form(report: dict, manifest: dict, tags: str,
                         releasetype: int, album_desc: str,
                         release_desc: str, edition_year: str,
                         edition_label: str, edition_catnum: str,
                         media: str, edition_title: str) -> list[tuple]:
    """Multipart form fields for a new-group upload. Returned as a list of
    tuples so we can have duplicate keys (artists[]/importance[]).

    OPS requires the edition_year (Gazelle's remaster_year) even for original
    releases; label and catnum are 'highly encouraged' but not strictly required.
    """
    fields: list[tuple] = [
        ("type", "0"),  # 0 = Music
        ("artists[]", report["artist"]),
        ("importance[]", "1"),  # 1 = main artist
        ("title", report["album"]),
        ("year", report["year"]),
        ("releasetype", str(releasetype)),
        ("format", report["format"]),
        ("bitrate", report["encoding"]),
        ("media", media),
        ("tags", tags),
        ("image", manifest["image_url"]),
        ("album_desc", album_desc),
        ("release_desc", release_desc),
        ("remaster_year", edition_year),
        ("remaster_title", edition_title),
    ]
    if edition_label:
        fields.append(("remaster_record_label", edition_label))
    if edition_catnum:
        fields.append(("remaster_catalogue_number", edition_catnum))
    return fields


def build_add_to_group_form(report: dict, groupid: int, release_desc: str,
                            edition_year: str, edition_label: str,
                            edition_catnum: str, media: str,
                            edition_title: str) -> list[tuple]:
    """Each torrent in a Gazelle group also carries edition info — OPS matches
    on (remaster_year, remaster_title, remaster_record_label, remaster_catalogue_number,
    media) to decide whether this is a new edition or attaches to an existing one
    within the group. Passing the same edition fields used for the new-group upload
    attaches our additional formats to the same edition."""
    fields: list[tuple] = [
        ("type", "0"),  # 0 = Music; required even when groupid is set
        ("groupid", str(groupid)),
        ("format", report["format"]),
        ("bitrate", report["encoding"]),
        ("media", media),
        ("release_desc", release_desc),
        ("remaster_year", edition_year),
        ("remaster_title", edition_title),
    ]
    if edition_label:
        fields.append(("remaster_record_label", edition_label))
    if edition_catnum:
        fields.append(("remaster_catalogue_number", edition_catnum))
    return fields


def post_upload(api_key: str, fields: list[tuple], torrent_path: Path) -> dict:
    headers = {
        "Authorization": f"token {api_key}",
        "Accept": "application/json",
    }
    with torrent_path.open("rb") as torrent_fh:
        files = {"file_input": (torrent_path.name, torrent_fh,
                                "application/x-bittorrent")}
        r = requests.post(
            OPS_AJAX,
            params={"action": "upload"},
            data=fields,
            files=files,
            headers=headers,
            timeout=120,
        )
    # OPS sometimes returns non-JSON on error pages; defend.
    try:
        return r.json()
    except ValueError:
        return {"status": "non_json", "http_status": r.status_code,
                "body_snippet": r.text[:500]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="normalized album directory")
    ap.add_argument("--tags", type=str,
                    help="comma-separated OPS tags (required for new groups; "
                         "ignored on add-to-group)")
    ap.add_argument("--releasetype", type=int, default=RELEASETYPE_DEFAULT,
                    help=f"OPS release type integer (default {RELEASETYPE_DEFAULT} = Album)")
    ap.add_argument("--album-desc", type=str, default="",
                    help="album-level BBCode description shown on the group page. "
                         "Defaults to the auto-generated tracklist (the OPS convention).")
    ap.add_argument("--release-desc", type=str,
                    default="Sourced from Bandcamp.",
                    help="per-torrent BBCode description for source/encoder notes")
    ap.add_argument("--edition-year", type=str, default="",
                    help="edition year for this specific release "
                         "(defaults to the album's release year)")
    ap.add_argument("--label", type=str, default="",
                    help="record label for this edition (e.g., 'Self-Released' for Bandcamp)")
    ap.add_argument("--catnum", type=str, default="",
                    help="catalogue number for this edition (often blank for Bandcamp)")
    ap.add_argument("--groupid", type=int, default=0,
                    help="attach to this existing OPS group instead of creating a new "
                         "one (or looking one up via sibling manifests). Used by "
                         "fill_gap.py to upload a transcoded format into an existing "
                         "group whose FLAC is already on OPS.")
    ap.add_argument("--media", default="",
                    help="override the report's media value (default: 'WEB' from "
                         "album_inspect). Gap-fill uses this to match the existing "
                         "edition's media (CD/Vinyl/etc.) so the new torrent attaches "
                         "to the right edition row instead of spawning a duplicate.")
    ap.add_argument("--edition-title", default="",
                    help="override remaster_title (default: '' = original edition). "
                         "Gap-fill passes this when attaching to a non-original edition "
                         "(e.g., 'Deluxe Edition') so we land on the right edition row.")
    ap.add_argument("--apply", action="store_true",
                    help="actually POST to OPS; default is dry-run")
    args = ap.parse_args()

    api_key = load_env(SECRETS_PATH).get("OPS_API_KEY", "")
    if not api_key:
        print("OPS_API_KEY missing from secrets.env", file=sys.stderr)
        return 1

    if not args.path.is_dir():
        print(f"{args.path} is not a directory", file=sys.stderr)
        return 1

    dirname = args.path.name
    manifest = load_manifest(dirname)
    # image_url is only required for new-group uploads (it's the cover art
    # for the group page); add-to-group uploads don't accept an image field,
    # so the check is deferred to the new-group branch below.
    if "torrent_path" not in manifest:
        print(f"manifest missing torrent_path; run make_torrent.py first", file=sys.stderr)
        return 1
    if manifest.get("ops_torrent_id"):
        print(f"already uploaded as OPS torrent {manifest['ops_torrent_id']} "
              f"(group {manifest.get('ops_group_id')}); aborting")
        return 0

    report = inspect_album(args.path)
    # album_inspect returns {"error": "..."} for non-album dirs (no audio files,
    # not a directory, etc.). Surface that as a clear failure here instead of
    # crashing on KeyError further down. Caught one in the wild: a multi-disc
    # transcode dir whose ffmpeg loop matched zero flat-level FLACs and so
    # produced an empty MP3 dir; the older fill_gap.py glob has since been
    # fixed to recurse, but this guard catches the class of failure cleanly.
    if "error" in report or not report.get("tracks"):
        print(f"inspect_album found no usable audio in {args.path}: "
              f"{report.get('error', 'no tracks')}", file=sys.stderr)
        return 1
    if report.get("issues"):
        print(f"WARNING — inspect reports issues:\n  - " +
              "\n  - ".join(report["issues"]), file=sys.stderr)

    # OPS convention: tracklist goes in album_desc (group page); release_desc is
    # for source/encoder notes (per-torrent). Both have a 10-char minimum.
    album_desc = args.album_desc or build_tracklist_bbcode(report)
    release_desc = args.release_desc

    key = album_key(dirname)
    # Explicit --groupid wins; otherwise look for a sibling manifest. The
    # explicit path is what gap-fill uploads use (they're attaching to a group
    # that someone else's FLAC already created, so there's no sibling locally).
    groupid = args.groupid or find_existing_groupid(key)

    # Honor explicit overrides; otherwise fall back to album_inspect's defaults
    # (media defaults to WEB, which is right for the run_pipeline.py / Bandcamp
    # flow but wrong for gap-fill against a CD/Vinyl/etc. edition — fill_gap.py
    # passes --media explicitly for that case).
    media = args.media or report["media"]
    edition_title = args.edition_title

    if groupid is None:
        if not args.tags:
            print("--tags is required for a new-group upload "
                  "(no sibling format has been uploaded yet for this album)",
                  file=sys.stderr)
            return 1
        if "image_url" not in manifest:
            print("manifest missing image_url; run art_upload.py first "
                  "(required for new-group uploads — the group page cover)",
                  file=sys.stderr)
            return 1
        edition_year = args.edition_year or report["year"]
        fields = build_new_group_form(report, manifest, args.tags,
                                      args.releasetype, album_desc,
                                      release_desc, edition_year,
                                      args.label, args.catnum,
                                      media, edition_title)
        mode = f"NEW GROUP (album key: {key!r})"
    else:
        edition_year = args.edition_year or report["year"]
        fields = build_add_to_group_form(report, groupid, release_desc,
                                         edition_year, args.label, args.catnum,
                                         media, edition_title)
        mode = f"ADD TO EXISTING GROUP {groupid} (album key: {key!r})"

    print(f"  mode:    {mode}")
    print(f"  torrent: {manifest['torrent_path']}")
    if "image_url" in manifest:
        print(f"  image:   {manifest['image_url']}")
    print(f"  fields:")
    for k, v in fields:
        # Long fields like release_desc truncated for readability.
        vs = str(v).replace("\n", "\\n")
        if len(vs) > 80:
            vs = vs[:77] + "..."
        print(f"    {k}: {vs}")
    if not args.apply:
        print("\n(dry-run; re-run with --apply to POST to OPS)")
        return 0

    print("\n  → POST to OPS action=upload...")
    response = post_upload(api_key, fields, Path(manifest["torrent_path"]))

    # Persist the exchange for debug regardless of outcome.
    debug_path = STATE_DIR / f"{dirname}.last_ops_exchange.json"
    debug_path.write_text(json.dumps({
        "request_fields": [list(t) for t in fields],
        "torrent_filename": Path(manifest["torrent_path"]).name,
        "response": response,
    }, indent=2) + "\n")

    if response.get("status") != "success":
        print(f"  FAILED: {response}", file=sys.stderr)
        return 2

    resp = response.get("response", {}) or {}
    # OPS returns camelCase keys.
    tid = resp.get("torrentId")
    gid = resp.get("groupId")
    new_group = resp.get("newgroup")
    print(f"  OK: torrent {tid} in group {gid} (new_group={new_group})")
    if resp.get("warnings"):
        for w in resp["warnings"]:
            print(f"  WARNING from OPS: {w}")

    manifest["ops_group_id"] = gid
    manifest["ops_torrent_id"] = tid
    manifest["ops_new_group"] = new_group
    save_manifest(dirname, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
