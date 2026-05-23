#!/usr/bin/env python3
"""Upload an album's cover.jpg to catbox.moe; record URL in per-album manifest.

Per-album state lives at tools/orpheus/upload/state/<dirname>.json. Each pipeline
stage from here on reads/writes its slice of that manifest, so re-running a stage
is a no-op when its outputs are already recorded.

Catbox.moe is anonymous (no auth, no signup). For other hosts (manual upload,
ptpimg, etc.), pass --image-url and the upload step is skipped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import requests

STATE_DIR = Path(__file__).parent / "state"
CATBOX_URL = "https://catbox.moe/user/api.php"


def upload_to_catbox(image_path: Path) -> str:
    with image_path.open("rb") as f:
        r = requests.post(
            CATBOX_URL,
            data={"reqtype": "fileupload"},
            files={"fileToUpload": (image_path.name, f, "image/jpeg")},
            timeout=120,
        )
    r.raise_for_status()
    url = r.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"catbox returned non-URL: {url!r}")
    return url


def load_manifest(dirname: str) -> dict:
    p = STATE_DIR / f"{dirname}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_manifest(dirname: str, manifest: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / f"{dirname}.json").write_text(json.dumps(manifest, indent=2) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", type=Path, nargs="+",
                    help="one or more album directories (each must contain cover.jpg)")
    ap.add_argument("--image-url", type=str,
                    help="record this URL for every provided dir; skip catbox upload")
    ap.add_argument("--force", action="store_true",
                    help="re-upload even if manifest already has image_url")
    args = ap.parse_args()

    # md5 → url across this run; albums with identical cover.jpg (same album,
    # different format dirs) share one catbox upload.
    md5_to_url: dict[str, str] = {}

    for path in args.paths:
        dirname = path.name
        cover = path / "cover.jpg"
        if not cover.exists():
            print(f"  {dirname}: no cover.jpg; skipping", file=sys.stderr)
            continue

        manifest = load_manifest(dirname)
        if "image_url" in manifest and not args.force:
            print(f"  {dirname}: already has image_url={manifest['image_url']}; skipping "
                  f"(use --force to re-upload)")
            continue

        if args.image_url:
            url = args.image_url
            print(f"  {dirname}: recording provided URL: {url}")
        else:
            md5 = hashlib.md5(cover.read_bytes()).hexdigest()
            if md5 in md5_to_url:
                url = md5_to_url[md5]
                print(f"  {dirname}: same cover as earlier album (md5={md5[:8]}); reusing {url}")
            else:
                print(f"  {dirname}: uploading {cover.stat().st_size//1024}KB cover to catbox.moe...")
                url = upload_to_catbox(cover)
                md5_to_url[md5] = url
                print(f"    → {url}")

        manifest["image_url"] = url
        manifest["cover_md5"] = hashlib.md5(cover.read_bytes()).hexdigest()
        save_manifest(dirname, manifest)

    return 0


if __name__ == "__main__":
    sys.exit(main())
