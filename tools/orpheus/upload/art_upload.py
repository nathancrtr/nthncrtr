#!/usr/bin/env python3
"""Upload an album's cover sidecar to Cloudflare R2; record URL in per-album manifest.

Keys in R2 are content-addressed: `<md5>.<ext>`. Re-uploading the same image
(same md5) is a no-op overwrite. The public URL is therefore stable and safe
to cache forever (we set Cache-Control: public, max-age=31536000, immutable).

Why R2 over catbox.moe (the previous host): catbox URLs rendered as broken
images on OPS group pages roughly 75% of the time. catbox's TOS also
discourages tracker cover-art hosting. R2 + a custom domain we control
(covers.nthncrtr.com) is permanent, free at our scale, and bypasses any
third-party reliability concerns.

For other hosts (manual upload elsewhere, ptpimg if it ever comes back), pass
--image-url and the R2 upload is skipped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).parent))
from inspect import find_cover  # noqa: E402

STATE_DIR = Path(__file__).parent / "state"
SECRETS_PATH = Path(__file__).parent.parent / "secrets.env"

MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

R2_REQUIRED_KEYS = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_PUBLIC_BASE",
)


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


def make_r2_client(env: dict[str, str]):
    missing = [k for k in R2_REQUIRED_KEYS if not env.get(k)]
    if missing:
        raise RuntimeError(
            f"missing R2 config in {SECRETS_PATH}: {', '.join(missing)}")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        region_name="auto",
    )


def upload_to_r2(client, bucket: str, public_base: str,
                 image_path: Path, md5: str) -> str:
    """PUT the image with a content-addressed key; return the public URL.

    Keys are `<md5>.<ext>`. Uploading the same content twice is harmless
    (overwrite with identical bytes), and the URL never changes for a
    given image, so this doubles as cross-album dedup (e.g., compilation
    album reused across formats).
    """
    ext = image_path.suffix.lower()
    if ext not in MIME_BY_EXT:
        raise ValueError(f"unsupported image extension {ext!r}; "
                         f"want one of {sorted(MIME_BY_EXT)}")
    key = f"{md5}{ext}"
    with image_path.open("rb") as fh:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=fh,
            ContentType=MIME_BY_EXT[ext],
            CacheControl="public, max-age=31536000, immutable",
        )
    return f"{public_base.rstrip('/')}/{key}"


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
                    help="one or more album directories (each must contain a cover "
                         "sidecar — cover.jpg / .jpeg / .png / .webp / folder.*)")
    ap.add_argument("--image-url", type=str,
                    help="record this URL for every provided dir; skip the R2 upload")
    ap.add_argument("--force", action="store_true",
                    help="re-upload to R2 even if manifest already has image_url")
    args = ap.parse_args()

    env = load_env(SECRETS_PATH)

    # Per-run md5 cache so albums with identical cover bytes (same album,
    # different format dirs in the same pipeline run) share one R2 PUT.
    md5_to_url: dict[str, str] = {}
    client = None  # lazy: don't init boto3 if --image-url skips it for everything

    for path in args.paths:
        dirname = path.name
        cover = find_cover(path)
        if cover is None:
            print(f"  {dirname}: no cover sidecar found; skipping", file=sys.stderr)
            continue

        manifest = load_manifest(dirname)
        if "image_url" in manifest and not args.force:
            print(f"  {dirname}: already has image_url={manifest['image_url']}; "
                  f"skipping (use --force to re-upload)")
            continue

        md5 = hashlib.md5(cover.read_bytes()).hexdigest()

        if args.image_url:
            url = args.image_url
            print(f"  {dirname}: recording provided URL: {url}")
        elif md5 in md5_to_url:
            url = md5_to_url[md5]
            print(f"  {dirname}: same cover as earlier album in this run "
                  f"(md5={md5[:8]}); reusing {url}")
        else:
            if client is None:
                try:
                    client = make_r2_client(env)
                except RuntimeError as e:
                    print(f"  ERROR: {e}", file=sys.stderr)
                    return 1
            kb = cover.stat().st_size // 1024
            print(f"  {dirname}: uploading {kb} KB cover to R2 "
                  f"({env['R2_BUCKET']})...")
            try:
                url = upload_to_r2(client, env["R2_BUCKET"],
                                   env["R2_PUBLIC_BASE"], cover, md5)
            except (BotoCoreError, ClientError) as e:
                print(f"  ERROR: R2 upload failed: {e}", file=sys.stderr)
                return 2
            md5_to_url[md5] = url
            print(f"    → {url}")

        manifest["image_url"] = url
        manifest["cover_md5"] = md5
        save_manifest(dirname, manifest)

    return 0


if __name__ == "__main__":
    sys.exit(main())
