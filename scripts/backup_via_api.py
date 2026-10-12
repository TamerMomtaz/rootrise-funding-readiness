"""
Export every opportunity, readiness item and the latest activity rows through the API.

Read-only. Use before any deploy or migration (in addition to the on-disk copy, see MIGRATION.md).

    python scripts/backup_via_api.py --api https://rootrise-funding-readiness.onrender.com/api [--api-key KEY]

Writes backups/frd-export-<UTC timestamp>.json (backups/ is git-ignored).
Note: /api/activity-log returns at most the newest 100 rows; the full log lives in the DB file.
"""
import argparse
import json
import os
import urllib.request
from datetime import datetime, timezone


def fetch(url, key):
    req = urllib.request.Request(url)
    if key:
        req.add_header("X-API-Key", key)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("DASHBOARD_API_URL", "https://rootrise-funding-readiness.onrender.com/api"))
    ap.add_argument("--api-key", default=os.environ.get("DASHBOARD_API_KEY"))
    ap.add_argument("--out-dir", default="backups")
    a = ap.parse_args()
    base = a.api.rstrip("/")
    data = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source": base,
        "health": fetch(f"{base}/health", a.api_key),
        "opportunities": fetch(f"{base}/opportunities", a.api_key),
        "readiness_items": fetch(f"{base}/readiness-items", a.api_key),
        "activity_log": fetch(f"{base}/activity-log?limit=100", a.api_key),
    }
    os.makedirs(a.out_dir, exist_ok=True)
    path = os.path.join(a.out_dir, f"frd-export-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"{path}: {len(data['opportunities'])} opportunities, {len(data['readiness_items'])} readiness items, "
          f"{len(data['activity_log'])} activity rows")


if __name__ == "__main__":
    main()
