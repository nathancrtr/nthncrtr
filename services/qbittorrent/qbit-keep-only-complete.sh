#!/usr/bin/env bash
# Keep ONLY torrents that hash-check at 100% in a given qBit category; delete
# anything below from qBit (deleteFiles=false — files on disk are NEVER
# touched). Intended for tracker-restore workflows where the recovered
# .torrent list is much larger than what's actually on disk (e.g. Beyond-HD's
# completed-snatches list, most of which is content the operator already
# watched and deleted).
#
# Runs as a one-shot python:3-alpine container inside gluetun's network
# namespace, so qBit's localhost auth-bypass applies — no password needed.
#
# The --category flag is the safety boundary: the script does nothing without
# one, and only inspects/acts on torrents in that category. Cannot affect
# music seeds, *arr-managed downloads, or anything else.
#
# Usage:
#   sudo ./qbit-keep-only-complete.sh --category bhd-movies             # dry-run (default)
#   sudo ./qbit-keep-only-complete.sh --category bhd-movies --yes       # act
#   sudo ./qbit-keep-only-complete.sh --category bhd-tv --max-wait 7200 # patient
#
# Args:
#   --category CAT       REQUIRED; scope. Refuses to run without one.
#   --yes                actually perform the keep/delete. Default is dry-run.
#   --dry-run            explicit no-op (same as default).
#   --poll-interval SEC  hash-check poll interval (default 5)
#   --max-wait SEC       abort if hash checks aren't done in this long
#                        (default 3600 = 1 hour)
#   --allow-active       bypass the "torrents are in active download state"
#                        abort. Only use if you've explicitly stopped them
#                        and qBit still reports them as such, or you actually
#                        want to delete in-flight downloads.

set -euo pipefail

CATEGORY=""
DRY_RUN=true
POLL=5
MAX_WAIT=3600
ALLOW_ACTIVE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --category) CATEGORY="$2"; shift 2;;
    --yes) DRY_RUN=false; shift;;
    --dry-run) DRY_RUN=true; shift;;
    --poll-interval) POLL="$2"; shift 2;;
    --max-wait) MAX_WAIT="$2"; shift 2;;
    --allow-active) ALLOW_ACTIVE=true; shift;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '/^set -euo/d'; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

if [[ -z "$CATEGORY" ]]; then
  echo "ERROR: --category is required (it is the safety boundary)" >&2
  exit 2
fi

# The wrapper does not need to know about qBit at all — the Python body talks
# to qBit. But we do need gluetun's netns to exist for that talk to work.
if ! docker ps --filter name=^gluetun$ --filter status=running --format '{{.Names}}' | grep -q '^gluetun$'; then
  echo "gluetun container is not running; aborting" >&2
  exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PY="$SCRIPT_DIR/qbit-keep-only-complete.py"
if [[ ! -f "$PY" ]]; then
  echo "missing companion: $PY" >&2
  exit 1
fi

docker run --rm \
  --network "container:gluetun" \
  -v "$PY":/cull.py:ro \
  -e CATEGORY="$CATEGORY" \
  -e DRY_RUN="$DRY_RUN" \
  -e POLL="$POLL" \
  -e MAX_WAIT="$MAX_WAIT" \
  -e ALLOW_ACTIVE="$ALLOW_ACTIVE" \
  python:3-alpine \
  python /cull.py
