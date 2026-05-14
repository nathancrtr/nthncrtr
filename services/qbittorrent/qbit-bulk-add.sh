#!/usr/bin/env bash
# Bulk-add every *.torrent in a directory to the local qBittorrent.
#
# Runs as a one-shot curl container inside gluetun's network namespace, so
# qBit sees 127.0.0.1 as the client and its "localhost auth bypass" applies.
# No qBit password needed; nothing crosses the docker bridge.
#
# qBit dedupes by infohash, so re-running this script with the same input dir
# is a no-op for torrents already loaded.
#
# Usage:
#   sudo ./qbit-bulk-add.sh --dir /path/to/torrents
#   sudo ./qbit-bulk-add.sh --dir ./torrents --paused          # add but don't start
#   sudo ./qbit-bulk-add.sh --dir ./torrents --limit 1         # add only the first one
#
# Args:
#   --dir DIR        directory of .torrent files (default: ./torrents)
#   --savepath PATH  qBit save path (default: /mnt/media/music)
#   --category CAT   qBit category (default: music)
#   --paused         start torrents in paused state
#   --limit N        only add the first N files (default: 0 = all)

set -euo pipefail

DIR=./torrents
SAVE=/mnt/media/music
CATEGORY=music
PAUSED=false
LIMIT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR="$2"; shift 2;;
    --savepath) SAVE="$2"; shift 2;;
    --category) CATEGORY="$2"; shift 2;;
    --paused) PAUSED=true; shift;;
    --limit) LIMIT="$2"; shift 2;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '/^set -euo/d'; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

if [[ ! -d "$DIR" ]]; then
  echo "directory not found: $DIR" >&2
  exit 1
fi

# Resolve to an absolute path so the docker -v bind works regardless of cwd.
DIR_ABS=$(cd "$DIR" && pwd)
COUNT=$(find "$DIR_ABS" -maxdepth 1 -name '*.torrent' -type f | wc -l)
if [[ "$COUNT" -eq 0 ]]; then
  echo "no .torrent files in $DIR_ABS" >&2
  exit 1
fi

echo "[bulk-add] dir=$DIR_ABS files=$COUNT savepath=$SAVE category=$CATEGORY paused=$PAUSED limit=$LIMIT"

# Sanity-check gluetun is healthy before we trust its netns.
if ! docker ps --filter name=^gluetun$ --filter status=running --format '{{.Names}}' | grep -q '^gluetun$'; then
  echo "gluetun container is not running; aborting" >&2
  exit 1
fi

docker run --rm \
  --network "container:gluetun" \
  -v "$DIR_ABS":/torrents:ro \
  -e SAVE="$SAVE" \
  -e CATEGORY="$CATEGORY" \
  -e PAUSED="$PAUSED" \
  -e LIMIT="$LIMIT" \
  curlimages/curl:latest \
  sh -c '
    set -u
    ok=0; fail=0; skipped=0; i=0
    for f in /torrents/*.torrent; do
      i=$((i+1))
      if [ "$LIMIT" -gt 0 ] && [ "$i" -gt "$LIMIT" ]; then
        break
      fi
      name=$(basename "$f")
      body=$(curl -sS -o /dev/null -w "%{http_code}" \
        -X POST "http://127.0.0.1:8080/api/v2/torrents/add" \
        -F "torrents=@$f" \
        -F "savepath=$SAVE" \
        -F "category=$CATEGORY" \
        -F "paused=$PAUSED" \
        -F "skip_checking=false" 2>&1) || body="000"
      if [ "$body" = "200" ]; then
        ok=$((ok+1))
        echo "[ok ] $name"
      else
        fail=$((fail+1))
        echo "[err] $name (http=$body)" >&2
      fi
    done
    echo "[bulk-add] done: ok=$ok fail=$fail (of $i scanned)"
    [ "$fail" -eq 0 ]
  '
