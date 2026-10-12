"""
One-off: find and merge duplicate opportunities through the API.

DRY-RUN BY DEFAULT — it only prints the plan. Nothing is changed unless you pass --apply.

Matching uses the same rules as the server-side dedup on POST (backend/normalize.py):
same normalized URL with similar titles, OR same canonical name (Hunter v3.3 canonicalize_name, >10 chars),
OR title similarity >= 0.92 with the same type. Matches are grouped transitively.

Keeper per group: starred first, then furthest-along status, then most filled-in
fields, then the oldest id. Empty fields on the keeper are filled from the duplicates
(first non-empty wins), notes are concatenated, the best fit_score / highest priority
are kept, then the duplicates are deleted.

    python scripts/merge_duplicates.py --api https://rootrise-funding-readiness.onrender.com/api
    python scripts/merge_duplicates.py --api ... --api-key $API_KEY --apply
"""
import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from normalize import is_duplicate  # noqa: E402

STATUS_RANK = {"researching": 0, "preparing": 1, "submitted": 2, "in_review": 3,
               "accepted": 4, "rejected": 1, "closed": 0}
FILLABLE = ["deadline", "url", "funding_amount", "requirements", "contact_info"]


def http(method, url, api_key=None, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("X-API-Key", api_key)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def group_duplicates(opps):
    parent = {o["id"]: o["id"] for o in opps}
    reasons = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(opps):
        for b in opps[i + 1:]:
            reason = is_duplicate(a, b)
            if reason:
                ra, rb = find(a["id"]), find(b["id"])
                if ra != rb:
                    parent[rb] = ra
                reasons[(a["id"], b["id"])] = reasons[(b["id"], a["id"])] = reason
    groups = {}
    for o in opps:
        groups.setdefault(find(o["id"]), []).append(o)
    return [g for g in groups.values() if len(g) > 1], reasons


def keeper_key(o):
    filled = sum(1 for f in FILLABLE + ["notes"] if o.get(f))
    return (-int(bool(o.get("is_saved"))), -STATUS_RANK.get(o["status"], 0), -filled, o["id"])


def plan_group(group):
    group = sorted(group, key=keeper_key)
    keeper, dupes = group[0], group[1:]
    patch = {}
    for f in FILLABLE:
        if not keeper.get(f):
            for d in dupes:
                if d.get(f):
                    patch[f] = d[f]
                    break
    best_fit = max(o["fit_score"] for o in group)
    if best_fit != keeper["fit_score"]:
        patch["fit_score"] = best_fit
    best_prio = min(o["priority"] for o in group)
    if best_prio != keeper["priority"]:
        patch["priority"] = best_prio
    extra_notes = [d["notes"] for d in dupes if d.get("notes") and d["notes"] not in (keeper.get("notes") or "")]
    if extra_notes:
        merged = "\n".join([keeper.get("notes") or ""] + [f"[merged #{d['id']}] {d['notes']}" for d in dupes
                                                          if d.get("notes") in extra_notes]).strip()
        patch["notes"] = merged
    return keeper, dupes, patch


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("DASHBOARD_API_URL", "http://localhost:8000/api"))
    ap.add_argument("--api-key", default=os.environ.get("DASHBOARD_API_KEY"))
    ap.add_argument("--from-json", help="Plan from an exported opportunities JSON instead of the API")
    ap.add_argument("--apply", action="store_true", help="Actually merge (default: dry run)")
    args = ap.parse_args()

    if args.from_json:
        data = json.load(open(args.from_json, encoding="utf-8"))
        opps = data["opportunities"] if isinstance(data, dict) else data
    else:
        opps = http("GET", f"{args.api.rstrip('/')}/opportunities", args.api_key)
    groups, reasons = group_duplicates(opps)
    total_dupes = sum(len(g) - 1 for g in groups)
    print(f"{len(opps)} opportunities · {len(groups)} duplicate groups · {total_dupes} rows would be removed")
    print("MODE: " + ("APPLY" if args.apply else "DRY RUN (no changes)") + "\n")

    for group in groups:
        keeper, dupes, patch = plan_group(group)
        print(f"KEEP  #{keeper['id']:<4} {keeper['name']}  [{keeper['type']}, {keeper['status']}]")
        for d in dupes:
            why = reasons.get((keeper['id'], d['id'])) or "transitive"
            print(f"  DROP #{d['id']:<4} {d['name']}  ({why})  {d.get('url') or ''}")
        if patch:
            shown = {k: (v[:60] + "…" if isinstance(v, str) and len(v) > 60 else v) for k, v in patch.items()}
            print(f"  FILL {shown}")
        print()
        if args.apply:
            base = args.api.rstrip('/')
            if patch:
                http("PUT", f"{base}/opportunities/{keeper['id']}", args.api_key, patch)
            for d in dupes:
                http("DELETE", f"{base}/opportunities/{d['id']}", args.api_key)
    if args.apply:
        print(f"Merged {len(groups)} groups, removed {total_dupes} rows.")


if __name__ == "__main__":
    main()
