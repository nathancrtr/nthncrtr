#!/usr/bin/env python3
"""
Build a ratio-aware recovery plan for the Orpheus snatched manifest.

Enriches each torrent with freeleech status from `action=torrent`, then
categorizes:
  free     freeTorrent != "Normal"          → recover, 0 ratio cost
  token    largest N non-FL torrents        → recover, 1 token *slot* each
  recover  smallest non-FL fitting budget   → recover, costs ratio
  abandon  beyond budget                    → not recovered

Note: the "token" classifier assumes 1 token per torrent. As of mid-2026
Orpheus actually charges tokens proportionally to torrent size (~1 token per
~313 MiB observed). The categorization remains useful as a "biggest torrents
to consider tokening" list, but the *count* in the token bucket should be
compared against your actual token balance and per-torrent cost.

Checkpoints every 25 enrichments so re-running picks up where it stopped.
Resumable: an already-enriched manifest skips the network entirely.

Inputs:
  ORPHEUS_API_KEY from /srv/qbittorrent/secrets.env (or env var)
  --manifest      manifest-snatched.json produced by orpheus-restore.py

Outputs:
  --enriched      enriched manifest (incremental checkpoints + final)
  --csv           sortable plan: torrentId,name,sizeGiB,freeTorrent,seeders,
                  recommendation,cumulativeRatioCostGiB

Rate limit: 2.5s sleep between API calls (Orpheus allows 5 req / 10s).
"""
import argparse, csv, json, os, pathlib, sys, time
import urllib.request

API = "https://orpheus.network/ajax.php"
SLEEP = 2.5

def load_token():
    for p in [pathlib.Path("/srv/qbittorrent/secrets.env"), pathlib.Path("./secrets.env")]:
        if p.is_file():
            try:
                for line in p.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"): continue
                    k, _, v = line.partition("=")
                    if k.strip() == "ORPHEUS_API_KEY":
                        return v.strip().strip('"').strip("'")
            except PermissionError:
                continue
    if os.environ.get("ORPHEUS_API_KEY"):
        return os.environ["ORPHEUS_API_KEY"]
    sys.exit("ORPHEUS_API_KEY not found")

def api_get(url, token):
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "User-Agent": "nthncrtr-orpheus-plan/1.0",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    if d.get("status") != "success":
        raise RuntimeError(f"API: {d}")
    return d

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", default="/srv/qbittorrent/restore/manifest-snatched.json")
    p.add_argument("--enriched", default="/srv/qbittorrent/restore/manifest-snatched-enriched.json")
    p.add_argument("--csv", default="/srv/qbittorrent/restore/recovery-plan.csv")
    p.add_argument("--budget-gib", type=float, default=300.0)
    p.add_argument("--tokens", type=int, default=0,
                   help="how many of the largest non-FL torrents to flag as 'token' (default: 0)")
    p.add_argument("--limit", type=int, default=0,
                   help="stop after enriching N new torrents (test mode)")
    a = p.parse_args()

    manifest = json.loads(pathlib.Path(a.manifest).read_text())
    print(f"[plan] manifest: {len(manifest)} entries", flush=True)

    enriched = {}
    ep = pathlib.Path(a.enriched)
    if ep.is_file():
        for e in json.loads(ep.read_text()):
            enriched[e["torrentId"]] = e
        print(f"[plan] resuming from {len(enriched)} already-enriched entries", flush=True)

    token = load_token()
    todo = [m for m in manifest if m["torrentId"] not in enriched]
    if a.limit:
        todo = todo[:a.limit]
    print(f"[plan] enriching {len(todo)} torrents (~{len(todo)*SLEEP/60:.1f} min)", flush=True)

    for i, m in enumerate(todo, start=1):
        tid = m["torrentId"]
        try:
            r = api_get(f"{API}?action=torrent&id={tid}", token)
            t = r["response"]["torrent"]
            enriched[tid] = {
                **m,
                "freeTorrent": t.get("freeTorrent", "Normal"),
                "freeReason":  t.get("freeReason", ""),
                "seeders":     t.get("seeders", 0),
                "leechers":    t.get("leechers", 0),
                "infoHash":    t.get("infoHash", ""),
                "size":        t.get("size", m.get("torrentSize", 0)),
            }
            if i % 25 == 0 or i == len(todo):
                ep.write_text(json.dumps(list(enriched.values()), indent=2))
                print(f"[plan] [{i:4}/{len(todo)}] checkpoint (last: tid={tid} fl={enriched[tid]['freeTorrent']})", flush=True)
        except Exception as e:
            print(f"[plan] [{i:4}/{len(todo)}] tid={tid}: ERROR {e}", file=sys.stderr, flush=True)
        time.sleep(SLEEP)

    ep.write_text(json.dumps(list(enriched.values()), indent=2))
    print(f"[plan] enriched: {len(enriched)} -> {ep}", flush=True)

    # ---- Build plan ----
    all_t = list(enriched.values())
    fl     = [t for t in all_t if t.get("freeTorrent","Normal") != "Normal"]
    non_fl = [t for t in all_t if t.get("freeTorrent","Normal") == "Normal"]
    non_fl_desc = sorted(non_fl, key=lambda x: x["size"], reverse=True)
    token_set = {t["torrentId"] for t in non_fl_desc[:a.tokens]}
    remaining = [t for t in non_fl if t["torrentId"] not in token_set]
    remaining_asc = sorted(remaining, key=lambda x: x["size"])
    budget = int(a.budget_gib * (1024**3))
    cum = 0; recover = []; abandon = []
    for t in remaining_asc:
        if cum + t["size"] <= budget:
            cum += t["size"]; recover.append(t)
        else:
            abandon.append(t)

    rows = []
    def push(t, action, c=None):
        rows.append({
            "torrentId": t["torrentId"],
            "name": (t.get("artistName","") + " - " + t.get("name","")).strip(" -"),
            "sizeGiB": round(t["size"]/2**30, 3),
            "freeTorrent": t.get("freeTorrent","Normal"),
            "seeders": t.get("seeders",0),
            "recommendation": action,
            "cumulativeRatioCostGiB": round(c/2**30,3) if c is not None else "",
        })
    for t in sorted(fl, key=lambda x: -x["size"]):           push(t, "free")
    for t in non_fl_desc[:a.tokens]:                          push(t, "token")
    c = 0
    for t in recover:
        c += t["size"]; push(t, "recover", c)
    for t in sorted(abandon, key=lambda x: -x["size"]):       push(t, "abandon")

    with open(a.csv, "w") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows: w.writerow(r)

    g = lambda b: b/2**30
    free_g     = sum(t["size"] for t in fl)
    token_g    = sum(t["size"] for t in non_fl_desc[:a.tokens])
    abandon_g  = sum(t["size"] for t in abandon)
    total_g    = sum(t["size"] for t in all_t)
    recovered  = len(fl) + min(a.tokens, len(non_fl)) + len(recover)

    print()
    print(f"=== recovery plan (budget={a.budget_gib} GiB, tokens={a.tokens}) ===")
    print(f"  free    : {len(fl):4} torrents, {g(free_g):7.1f} GiB  → recovered, 0 ratio cost")
    print(f"  token   : {min(a.tokens,len(non_fl)):4} torrents, {g(token_g):7.1f} GiB  → recovered, 1 token slot each")
    print(f"  recover : {len(recover):4} torrents, {g(cum):7.1f} GiB  → recovered, this counts against ratio")
    print(f"  abandon : {len(abandon):4} torrents, {g(abandon_g):7.1f} GiB  → not recovered")
    print(f"  TOTAL   : {len(all_t):4} torrents, {g(total_g):7.1f} GiB ({recovered} recovered, {100*recovered/len(all_t):.0f}%)")
    print()
    print(f"  CSV:               {a.csv}")
    print(f"  enriched manifest: {a.enriched}")

if __name__ == "__main__":
    main()
