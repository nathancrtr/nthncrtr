#!/usr/bin/env python3
"""Check Orpheus (orpheus.network) for each wishlist row's availability.

Reads a CSV produced by parse_wishlist.py and writes three CSVs to --output-dir:
  orpheus-available.csv  — confident match + at least one seeded FLAC torrent
  orpheus-novel.csv      — no group found, or group found but no seeded FLAC
  orpheus-review.csv     — group found but fuzzy match below threshold; operator triages

Auth: OPS_API_KEY from --env-file (default ./secrets.env).
Rate limit: ~2.1s between requests, under Gazelle's 5-per-10s cap.

FLAC selection rubric (within the chosen group):
  1. format == FLAC and seeders > 0
  2. prefer media=CD with logScore==100, then any CD, then WEB, then Vinyl, then other
  3. tiebreak by seeders desc
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE = "https://orpheus.network/ajax.php"
USER_AGENT = "nthncrtr-wishlist-tool/0.1"
MIN_INTERVAL = 3.0  # seconds between requests; OPS appears to silently empty responses when pushed close to its 5/10s cap


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


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def sanitize_for_search(s: str) -> str:
    """Looser cleanup for retry queries: ASCII-fold, drop punctuation, keep words."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"[^A-Za-z0-9'\-\s]", " ", s)
    return " ".join(s.split())


def text_similarity(a: str, b: str) -> float:
    """Sequence-ratio similarity on normalized strings — used for album titles."""
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


_ARTIST_STOPWORDS = {"and", "vs", "feat", "featuring", "with", "the"}


def artist_similarity(a: str, b: str) -> float:
    """Order/joiner-tolerant artist match.

    max of:
      - sequence ratio (handles small variants)
      - token Jaccard (handles 'A & B' vs 'B and A')
      - subset score: if one token-set is a subset of the other, 0.9
        (handles 'Eno' vs 'Brian Eno')
    """
    seq = SequenceMatcher(None, normalize(a), normalize(b)).ratio()
    ta = set(normalize(a).split()) - _ARTIST_STOPWORDS
    tb = set(normalize(b).split()) - _ARTIST_STOPWORDS
    if not ta or not tb:
        return seq
    jaccard = len(ta & tb) / len(ta | tb)
    subset = 0.9 if (ta <= tb or tb <= ta) else 0.0
    return max(seq, jaccard, subset)


class OPS:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._last = 0.0

    def _throttle(self) -> None:
        wait = MIN_INTERVAL - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)

    def call(self, **params) -> dict | None:
        self._throttle()
        url = BASE + "?" + urlencode(params)
        req = Request(url, headers={
            "Authorization": f"token {self.api_key}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        attempts = 0
        while True:
            attempts += 1
            try:
                with urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8")
                self._last = time.time()
                data = json.loads(body)
                break
            except HTTPError as e:
                self._last = time.time()
                if e.code == 429 and attempts < 4:
                    backoff = 5 * attempts
                    print(f"  429 — sleeping {backoff}s", file=sys.stderr)
                    time.sleep(backoff)
                    continue
                snippet = e.read()[:200] if hasattr(e, "read") else b""
                print(f"  HTTP {e.code} on {params}: {snippet!r}", file=sys.stderr)
                return None
            except (URLError, TimeoutError, json.JSONDecodeError) as e:
                self._last = time.time()
                print(f"  network/parse error on {params}: {e}", file=sys.stderr)
                return None
        if data.get("status") != "success":
            print(f"  api error on {params}: {data.get('error')!r}", file=sys.stderr)
            return None
        return data.get("response")


def pick_best_flac(torrents: list[dict]) -> dict | None:
    seeded = [t for t in torrents if t.get("format") == "FLAC" and t.get("seeders", 0) > 0]
    if not seeded:
        return None
    media_rank = {"CD": 0, "WEB": 1, "Vinyl": 2, "SACD": 3, "DVD": 4,
                  "Blu-Ray": 5, "Cassette": 6, "DAT": 7, "Soundboard": 8}
    def key(t: dict):
        m = t.get("media", "")
        log_perfect = (m == "CD" and t.get("logScore") == 100)
        return (media_rank.get(m, 9), 0 if log_perfect else 1, -t.get("seeders", 0))
    return min(seeded, key=key)


def score_group(g: dict, artist: str, album: str, release_type: str) -> float:
    """Per-group score in [0,1].

    For DJ mixes, VA inputs, and OPS groups attributed to "Various Artists",
    score on title alone — the curator name lives in the title, not the artist field.
    """
    t_sim = text_similarity(g.get("groupName", ""), album)
    va_input = normalize(artist) == "various artists"
    djmix_input = release_type == "djmix"
    ops_va = normalize(g.get("artist", "")) == "various artists"
    if va_input or djmix_input or ops_va:
        return t_sim
    return 0.4 * artist_similarity(g.get("artist", ""), artist) + 0.6 * t_sim


# Groups within this band of the top score are treated as equivalent matches
# (different editions of the same release); among them, prefer one with a seeded FLAC.
SCORE_TIE_BAND = 0.05


def best_match(
    results: list[dict], artist: str, album: str, release_type: str = ""
) -> tuple[dict | None, float, dict | None]:
    """Pick the best group + best FLAC torrent.

    Returns (group, score, torrent). If no group within the tie band has a seeded
    FLAC, returns (top_group, top_score, None) — the caller treats this as novel
    with diagnostic context.
    """
    if not results:
        return None, 0.0, None
    scored = sorted(
        ((score_group(g, artist, album, release_type), g) for g in results),
        key=lambda x: -x[0],
    )
    top_score = scored[0][0]
    for s, g in scored:
        if s < top_score - SCORE_TIE_BAND:
            break
        chosen = pick_best_flac(g.get("torrents", []))
        if chosen is not None:
            return g, s, chosen
    return scored[0][1], scored[0][0], None


def search(
    ops: OPS, artist: str, album: str, canonical_artists: list[str], release_type: str
) -> list[dict]:
    """Run a retry chain of searches, returning the first non-empty result set.

    Retry order:
      1. Combined searchstr ("artist album")
      2. Field-split (artistname / groupname)
      3. Sanitized combined (ASCII-folded, punctuation stripped) — if it differs from #1
      4. First canonical artist only — for collabs
      5. groupname only — for VA / djmix inputs
    """
    san_artist = sanitize_for_search(artist)
    san_album = sanitize_for_search(album)
    queries: list[dict] = [
        {"action": "browse", "searchstr": f"{artist} {album}"},
        {"action": "browse", "artistname": artist, "groupname": album},
    ]
    if (san_artist, san_album) != (artist, album):
        queries.append({"action": "browse", "searchstr": f"{san_artist} {san_album}"})
    if len(canonical_artists) > 1:
        first = sanitize_for_search(canonical_artists[0])
        queries.append({"action": "browse", "searchstr": f"{first} {san_album}"})
    if normalize(artist) == "various artists" or release_type == "djmix":
        queries.append({"action": "browse", "groupname": san_album})

    seen: set[int] = set()
    out: list[dict] = []
    for params in queries:
        resp = ops.call(**params)
        if not resp:
            continue
        for g in resp.get("results") or []:
            gid = g.get("groupId")
            if gid in seen:
                continue
            seen.add(gid)
            out.append(g)
        if out:
            break  # short-circuit on first non-empty query
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, default=Path("/tmp/wishlist.csv"))
    ap.add_argument("--env-file", type=Path,
                    default=Path(__file__).parent / "secrets.env")
    ap.add_argument("--output-dir", type=Path, default=Path("/tmp"))
    ap.add_argument("--limit", type=int, default=0,
                    help="only process the first N rows (for testing)")
    ap.add_argument("--match-threshold", type=float, default=0.85,
                    help="below this fuzzy score, hits go to review.csv instead of available.csv")
    ap.add_argument("--probe", action="store_true",
                    help="hit action=index and exit; use to verify auth")
    args = ap.parse_args()

    api_key = load_env(args.env_file).get("OPS_API_KEY", "")
    if not api_key:
        print(f"OPS_API_KEY missing in {args.env_file}", file=sys.stderr)
        return 1

    ops = OPS(api_key)

    if args.probe:
        info = ops.call(action="index")
        if not info:
            return 2
        stats = info.get("userstats", {})
        print(f"auth ok: id={info.get('id')} username={info.get('username')!r} "
              f"ratio={stats.get('ratio')} class={info.get('class')!r}")
        return 0

    rows = list(csv.DictReader(args.csv.open(encoding="utf-8")))
    if args.limit:
        rows = rows[:args.limit]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    avail_path = args.output_dir / "orpheus-available.csv"
    novel_path = args.output_dir / "orpheus-novel.csv"
    review_path = args.output_dir / "orpheus-review.csv"

    avail_fields = [
        "rym_id", "artist", "album", "release_type",
        "ops_group_id", "ops_group_name", "ops_artist", "ops_year",
        "ops_torrent_id", "format", "encoding", "media", "log_score",
        "seeders", "size_mb", "match_score", "rym_url",
    ]
    novel_fields = ["rym_id", "artist", "album", "release_type", "rym_url", "reason"]
    review_fields = list(avail_fields)

    with avail_path.open("w", encoding="utf-8", newline="") as af, \
         novel_path.open("w", encoding="utf-8", newline="") as nf, \
         review_path.open("w", encoding="utf-8", newline="") as rf:
        aw = csv.DictWriter(af, fieldnames=avail_fields); aw.writeheader()
        nw = csv.DictWriter(nf, fieldnames=novel_fields); nw.writeheader()
        rw = csv.DictWriter(rf, fieldnames=review_fields); rw.writeheader()

        n_avail = n_novel = n_review = 0
        for i, row in enumerate(rows, 1):
            artist, album = row["artist"], row["album"]
            release_type = row.get("release_type", "")
            canonical = [a for a in row.get("artists", "").split("|") if a]
            print(f"[{i}/{len(rows)}] {artist} — {album}", file=sys.stderr)
            groups = search(ops, artist, album, canonical, release_type)
            if not groups:
                nw.writerow({**{k: row.get(k, "") for k in
                                ("rym_id", "artist", "album", "release_type", "rym_url")},
                             "reason": "no groups returned"})
                n_novel += 1
                print("  → novel (no groups)", file=sys.stderr)
                continue
            group, score, chosen = best_match(groups, artist, album, release_type)
            if not chosen:
                nw.writerow({**{k: row.get(k, "") for k in
                                ("rym_id", "artist", "album", "release_type", "rym_url")},
                             "reason": f"matched group {group and group.get('groupId')} "
                                       f"(score {score:.2f}) but no seeded FLAC"})
                n_novel += 1
                print(f"  → novel (no seeded FLAC; best group score {score:.2f})",
                      file=sys.stderr)
                continue
            out = {
                "rym_id": row["rym_id"],
                "artist": artist,
                "album": album,
                "release_type": row["release_type"],
                "ops_group_id": group["groupId"],
                "ops_group_name": group.get("groupName", ""),
                "ops_artist": group.get("artist", ""),
                "ops_year": group.get("groupYear", ""),
                "ops_torrent_id": chosen["torrentId"],
                "format": chosen.get("format", ""),
                "encoding": chosen.get("encoding", ""),
                "media": chosen.get("media", ""),
                "log_score": chosen.get("logScore", ""),
                "seeders": chosen.get("seeders", 0),
                "size_mb": round((chosen.get("size", 0) or 0) / 1024 / 1024, 1),
                "match_score": round(score, 3),
                "rym_url": row.get("rym_url", ""),
            }
            if score >= args.match_threshold:
                aw.writerow(out)
                n_avail += 1
                print(f"  → available (group {out['ops_group_id']}, "
                      f"{out['encoding']} {out['media']}, seeders={out['seeders']}, "
                      f"score {score:.2f})", file=sys.stderr)
            else:
                rw.writerow(out)
                n_review += 1
                print(f"  → review (low confidence, score {score:.2f})", file=sys.stderr)

    print(f"\nsummary: {n_avail} available / {n_review} review / {n_novel} novel "
          f"(of {len(rows)})", file=sys.stderr)
    print(f"  {avail_path}\n  {review_path}\n  {novel_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
