# MIGRATION: v1.1/"v3.2" → v4.0.0

**Production:** Render web service `rootrise-funding-readiness` (Standard plan), SQLite at `DATABASE_PATH=/data/funding_readiness.db` on the persistent disk.
**What changes in the DB:** schema `user_version` goes 0 → 2. Five nullable columns are **added** to `opportunities` (`category`, `source`, `needs_check`, `access_status`, `url_normalized`), and two indexes are created. The new columns are backfilled from the Hunter's notes tags. Existing columns and values are never modified, and no rows are deleted.

> Rules: back up first, never call `/api/reset-database` (removed in v4), and don't deploy during the Hunter window (04:15 UTC ± 15 min).

---

## 0. Rehearse (done; repeat if production changed)

```bash
python scripts/backup_via_api.py --out-dir backups                      # read-only export
python scripts/build_db_from_export.py backups/frd-export-*.json /tmp/rehearsal.db
DATABASE_PATH=/tmp/rehearsal.db uvicorn backend.main:app --port 8765    # migrates the copy
curl -s localhost:8765/api/health                                      # → "schema_version": 2
python scripts/ui_smoke.py --base http://127.0.0.1:8765 --out /tmp/shots
```
Rehearsal result on the 2026-10-12 export: 125 opportunities migrated, with 106 startup, 19 BD, 2 needs-check and 26 expired. No existing values changed (asserted in `tests/test_migration.py`).

## 1. Back up (immediately before deploying)

**A. API export** (from any machine):
```bash
python scripts/backup_via_api.py --api https://rootrise-funding-readiness.onrender.com/api
```
**B. On-disk copy** (Render → service → **Shell**). This copies the full activity log too, which the API caps at 100 rows:
```bash
mkdir -p /data/backups
python -c "import sqlite3,datetime;s=sqlite3.connect('/data/funding_readiness.db');d=sqlite3.connect('/data/backups/frd-pre-v4-'+datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')+'.db');s.backup(d);d.close();print('ok')"
ls -la /data/backups
```
**C. Automatic:** on first v4 boot the app also writes `/data/funding_readiness.db.pre-v2-<timestamp>.bak` before migrating.

## 2. Deploy

1. Merge the PR `fix/frd-overhaul` → `main`. Render auto-deploys and runs `start.sh` (single uvicorn worker).
2. The migration runs on startup (FastAPI lifespan); nothing to run by hand. Logs show:
   `migration: backup written to …pre-v2-….bak` → `schema now at v2`.
3. Leave `AUTH_MODE` unset (`off`) for this deploy.

## 3. Verify

```bash
B=https://rootrise-funding-readiness.onrender.com/api
curl -s $B/health                                    # version 4.0.0, schema_version 2
curl -s "$B/opportunities" | python -c "import json,sys,collections as c;o=json.load(sys.stdin);print(len(o),c.Counter(x['category'] for x in o),sum(x['is_expired'] for x in o))"
#   expect: 125 Counter({'startup': 106, 'bd': 19}) 26   (plus anything the Hunter added since)
curl -s $B/dashboard-stats | python -m json.tool | head -20
python scripts/ui_smoke.py --base https://rootrise-funding-readiness.onrender.com --out /tmp/live-shots   # read-only
```

## 4. Hunter (`rootrise-opportunity-hunter`, branch `fix/hunter-contract`)

1. Merge the Hunter PR. (It works against both v1.1 and v4 dashboards: older backends ignore the extra fields.)
2. Optional: repository **variable** `HUNTER_MODEL` (default `claude-sonnet-5-5`).
3. Actions → *🎯 Opportunity Hunter v3.4* → **Run workflow** (`dry_run=false`).
4. Confirm that the run is green and that new rows have `source` set (not `manual`), the correct `category`, and `needs_check` for auth-walled pages:
   ```bash
   curl -s "$B/opportunities?sort_by=created_at&sort_order=desc" | python -c "import json,sys;[print(o['id'],o['category'],o['source'],o['needs_check'],o['name']) for o in json.load(sys.stdin)[:10]]"
   ```

## 5. Optional: merge existing duplicates (needs approval)

```bash
python scripts/merge_duplicates.py --api $B                 # dry run; compare with docs/dedup-dry-run.txt
python scripts/merge_duplicates.py --api $B --apply [--api-key …]   # only after sign-off
```

## 6. Turn on auth (after steps 2–4 are green)

1. Generate secrets: `python -c "import secrets; print(secrets.token_urlsafe(32))"` (run 2×: one for `API_KEY`, one for `AUTH_SECRET`).
2. **GitHub** (Hunter repo → Settings → Secrets and variables → Actions → Secrets): `DASHBOARD_API_KEY` = the `API_KEY` value.
3. **Render** (service → Environment): `API_KEY`, `AUTH_SECRET`, `TEAM_PASSCODE` (share with the team), then `AUTH_MODE=writes`. Saving triggers a restart.
4. Check: `curl -X POST $B/opportunities -H 'Content-Type: application/json' -d '{"name":"x","type":"grant"}'` → **401**. Run the Hunter manually → green, with rows added.
5. Flip `AUTH_MODE=full`. Check: `curl $B/opportunities` → 401, `curl -H "X-API-Key: …" $B/opportunities` → 200, and the browser shows the passcode screen. Run the Hunter once more.

---

## Rollback

**Code only (most cases).** Render → service → *Events/Deploys* → previous deploy → **Rollback**. The v1.1 code runs unchanged on the v4 schema: it ignores the extra columns, and its INSERTs leave them NULL. This was verified by running v1.1 `main.py` against a migrated copy (list, stats, create, update, toggle). When v4 is redeployed, rows written in the meantime are backfilled automatically on boot. If auth was on, set `AUTH_MODE=off` first. v1.1 has no auth, and the Hunter's extra header is harmless.

**Data (only if data is wrong).**
1. Render → set the service to suspended (or scale to 0) so nothing writes.
2. Shell:
   ```bash
   ls -la /data/*.bak /data/backups/
   cp /data/funding_readiness.db /data/backups/frd-broken-$(date -u +%Y%m%dT%H%M%SZ).db
   cp /data/backups/frd-pre-v4-<stamp>.db /data/funding_readiness.db       # or the .pre-v2-….bak
   rm -f /data/funding_readiness.db-wal /data/funding_readiness.db-shm
   ```
3. Resume the service. If the restored file is pre-v4 and v4 is still deployed, it re-migrates on boot (additive), which is expected.

**Disk lost entirely.** Rebuild from the API export: `python scripts/build_db_from_export.py backups/frd-export-<stamp>.json funding_readiness.db`, upload it to `/data/`, and restart. Only the newest 100 activity rows survive this path.
