#!/usr/bin/env python3
"""Inspect a Bandcamp-style album directory; report tags + completeness for OPS upload.

Reads every audio file under the directory, normalizes tags across FLAC/MP3 formats,
detects the source format/encoding, flags inconsistencies, and emits either a
human-readable summary or a JSON report consumed by later pipeline stages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

from mutagen import File as MutagenFile

# ID3 frame → vorbis-style tag name. We normalize across formats so downstream
# stages don't care whether the source was FLAC or MP3.
ID3_MAP = {
    "TIT2": "title",
    "TPE1": "artist",
    "TPE2": "albumartist",
    "TALB": "album",
    "TDRC": "date",
    "TRCK": "tracknumber",
    "TSRC": "isrc",
}
REQUIRED_TAGS = ("title", "artist", "album", "tracknumber", "date")

# Cover sidecar candidates, in priority order. Bandcamp ships cover.jpg, but
# some sources (e.g., labels uploading via Bandcamp) ship cover.png; both
# are acceptable for OPS as long as the URL we hand it resolves to an image.
COVER_CANDIDATES = ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp",
                    "folder.jpg", "folder.jpeg", "folder.png")


def find_cover(album_dir: Path) -> Path | None:
    """Return the first present cover sidecar, or None."""
    for name in COVER_CANDIDATES:
        p = album_dir / name
        if p.exists():
            return p
    return None


def detect_format(track: Path) -> tuple[str, str]:
    """Return (format, encoding) using OPS's vocabulary.

    OPS distinguishes Lossless (16-bit) from 24bit Lossless. For MP3 we want
    the exact OPS encoding label so the upload form can be auto-filled.
    """
    suffix = track.suffix.lower()
    f = MutagenFile(track)
    if suffix == ".flac":
        bps = getattr(f.info, "bits_per_sample", 16)
        return "FLAC", "24bit Lossless" if bps >= 24 else "Lossless"
    if suffix == ".mp3":
        # mutagen's BitrateMode renders as "BitrateMode.CBR" / "BitrateMode.VBR" / etc.;
        # .name isn't reliably available across versions.
        mode_name = str(getattr(f.info, "bitrate_mode", "")).rsplit(".", 1)[-1]
        kbps = f.info.bitrate // 1000
        if mode_name == "CBR" and kbps == 320:
            return "MP3", "320"
        if mode_name == "VBR":
            # Bandcamp's V0 averages ~245 kbps; V2 ~190. Anything else we label generically.
            if kbps >= 220:
                return "MP3", "V0 (VBR)"
            if kbps >= 170:
                return "MP3", "V2 (VBR)"
            return "MP3", f"VBR ({kbps}k)"
        return "MP3", f"{kbps}"
    return suffix.upper().lstrip("."), "?"


def read_tags(track: Path) -> dict[str, str]:
    f = MutagenFile(track)
    if f is None or f.tags is None:
        return {}
    if track.suffix.lower() == ".flac":
        # Vorbis comments come back as lists; flatten to first value.
        return {k: (v[0] if isinstance(v, list) and v else str(v))
                for k, v in f.tags.items()}
    if track.suffix.lower() == ".mp3":
        out: dict[str, str] = {}
        for frame, name in ID3_MAP.items():
            if frame in f.tags:
                out[name] = str(f.tags[frame])
        comms = f.tags.getall("COMM")
        if comms:
            out["comment"] = str(comms[0])
        return out
    return {}


def embedded_art_md5(track: Path) -> str | None:
    f = MutagenFile(track)
    if track.suffix.lower() == ".flac" and getattr(f, "pictures", None):
        return hashlib.md5(f.pictures[0].data).hexdigest()
    if track.suffix.lower() == ".mp3" and f.tags is not None:
        for k in f.tags.keys():
            if k.startswith("APIC"):
                return hashlib.md5(f.tags[k].data).hexdigest()
    return None


def first_int(s: str | None) -> int | None:
    if not s:
        return None
    m = re.match(r"(\d+)", str(s))
    return int(m.group(1)) if m else None


def inspect_album(album_dir: Path) -> dict:
    if not album_dir.is_dir():
        return {"path": str(album_dir), "error": "not a directory"}
    tracks = sorted(p for p in album_dir.iterdir()
                    if p.suffix.lower() in (".flac", ".mp3"))
    if not tracks:
        return {"path": str(album_dir), "error": "no audio files found"}

    fmt, encoding = detect_format(tracks[0])

    track_data = []
    issues: list[str] = []
    for t in tracks:
        tags = read_tags(t)
        info = MutagenFile(t).info
        td = {
            "filename": t.name,
            "tags": tags,
            "length_sec": float(info.length),
            "size_bytes": t.stat().st_size,
            "embedded_art_md5": embedded_art_md5(t),
        }
        track_data.append(td)
        for r in REQUIRED_TAGS:
            if not str(tags.get(r, "")).strip():
                issues.append(f"{t.name}: missing tag {r}")

    artists = Counter(td["tags"].get("artist", "") for td in track_data)
    albums = Counter(td["tags"].get("album", "") for td in track_data)
    years = Counter(str(td["tags"].get("date", "")).split("-")[0]
                    for td in track_data)
    if len(artists) > 1:
        issues.append(f"inconsistent artist across tracks: {dict(artists)}")
    if len(albums) > 1:
        issues.append(f"inconsistent album across tracks: {dict(albums)}")
    if len(years) > 1:
        issues.append(f"inconsistent year across tracks: {dict(years)}")

    nums = sorted(first_int(td["tags"].get("tracknumber"))
                  for td in track_data if td["tags"].get("tracknumber"))
    if nums and nums != list(range(1, len(tracks) + 1)):
        issues.append(f"track numbering not sequential 1..{len(tracks)}: {nums}")

    art_hashes = {td["embedded_art_md5"] for td in track_data
                  if td["embedded_art_md5"]}
    if len(art_hashes) > 1:
        issues.append(f"embedded art differs across tracks: {len(art_hashes)} distinct hashes")
    missing_art = [td["filename"] for td in track_data if not td["embedded_art_md5"]]
    if missing_art:
        issues.append(f"{len(missing_art)} tracks missing embedded art")

    cover_path = find_cover(album_dir)
    cover_info: dict | None = None
    if cover_path is not None:
        cover_md5 = hashlib.md5(cover_path.read_bytes()).hexdigest()
        cover_info = {
            "path": str(cover_path),
            "size_bytes": cover_path.stat().st_size,
            "md5": cover_md5,
            "matches_embedded": cover_md5 in art_hashes,
        }
        # Bandcamp embeds a smaller resized cover in audio files but ships the
        # full-res original as the sidecar; expected, not an issue. We'll use
        # the sidecar for the OPS upload and leave embedded art alone.
    else:
        issues.append(f"no cover sidecar (looked for {', '.join(COVER_CANDIDATES)})")

    bandcamp_signature = any(
        "bandcamp" in str(td["tags"].get("comment", "")).lower()
        for td in track_data
    )

    return {
        "path": str(album_dir),
        "format": fmt,
        "encoding": encoding,
        "media": "WEB",
        "artist": artists.most_common(1)[0][0] if artists else "",
        "album": albums.most_common(1)[0][0] if albums else "",
        "year": years.most_common(1)[0][0] if years else "",
        "track_count": len(tracks),
        "total_length_sec": sum(td["length_sec"] for td in track_data),
        "total_size_bytes": sum(td["size_bytes"] for td in track_data),
        "tracks": [{
            "num": first_int(td["tags"].get("tracknumber")),
            "title": td["tags"].get("title", ""),
            "length_sec": td["length_sec"],
            "size_bytes": td["size_bytes"],
        } for td in track_data],
        "cover": cover_info,
        "bandcamp_signature_in_comment": bandcamp_signature,
        "issues": issues,
    }


def fmt_human(report: dict) -> str:
    if "error" in report:
        return f"  ERROR: {report['error']}"
    lines = [
        f"  Artist:    {report['artist']}",
        f"  Album:     {report['album']} ({report['year']})",
        f"  Format:    {report['format']} {report['encoding']}, media={report['media']}",
        f"  Tracks:    {report['track_count']}  "
        f"({report['total_length_sec']/60:.1f} min, "
        f"{report['total_size_bytes']/1024/1024:.1f} MB)",
    ]
    if report["cover"]:
        lines.append(f"  Cover:     {report['cover']['size_bytes']//1024} KB sidecar "
                     f"(used for OPS upload; embedded art in tracks left as-is)")
    if report["bandcamp_signature_in_comment"]:
        lines.append("  Note:      Bandcamp 'Visit ...' in COMMENT tag — will strip on normalize")
    if report["issues"]:
        lines.append(f"  Issues ({len(report['issues'])}):")
        for i in report["issues"]:
            lines.append(f"    - {i}")
    else:
        lines.append("  Issues:    none")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", type=Path, nargs="+", help="one or more album directories")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of human-readable text")
    args = ap.parse_args()

    reports = [inspect_album(p) for p in args.paths]
    if args.json:
        print(json.dumps(reports, indent=2, default=str))
    else:
        for r in reports:
            print(f"\n=== {Path(r['path']).name} ===")
            print(fmt_human(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
