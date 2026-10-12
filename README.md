# RootRise Funding Readiness Dashboard (FRD) · v4.0

Tracks funding opportunities (🚀 startup) and business-development tenders (📋 BD) for DEVONEERS / RootRise, plus the investor-readiness checklist. The [Opportunity Hunter](https://github.com/TamerMomtaz/rootrise-opportunity-hunter) fills it every morning.

*"When 'I's rise with &I, they transform"*

**Live:** https://rootrise-funding-readiness.onrender.com · **API:** `/api` · **OpenAPI:** `/docs`
**Docs:** [CHANGELOG](CHANGELOG.md) · [MIGRATION / runbook](MIGRATION.md) · [DIAGNOSIS (v4 audit)](DIAGNOSIS.md)

---

## Architecture

```
GitHub Actions (cron 04:15 UTC)                 Render web service (Standard, 1 instance)
┌────────────────────────────┐   X-API-Key    ┌──────────────────────────────────────────┐
│ hunter.py v3.4             │ ─────────────▶ │ start.sh → uvicorn backend.main:app (1w) │
│ search/RSS/scrape → dedup  │  GET/POST /api │   FastAPI + Pydantic v2                   │
│ → Claude batch eval → POST │                │   ├─ backend/main.py   routes, models     │
└────────────────────────────┘                │   ├─ backend/auth.py   AUTH_MODE gate     │
                                              │   ├─ backend/migrations.py  user_version  │
Browser / installed PWA                        │   ├─ backend/normalize.py   dates, dedup │
┌────────────────────────────┐  Bearer token  │   └─ frontend/ index.html + static/app.* │
│ frontend (vanilla JS, no   │ ─────────────▶ │ SQLite (WAL) /data/funding_readiness.db   │
│ build step), sw.js         │                │   on the persistent disk                  │
└────────────────────────────┘                └──────────────────────────────────────────┘
```

Stack constraints, kept deliberately: FastAPI, SQLite, vanilla JS, Render and GitHub Actions. No framework migration and no Postgres.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
DATABASE_PATH=./data/frd.db uvicorn backend.main:app --reload --port 8000   # mkdir data first
open http://localhost:8000
python -m pytest                       # 63 tests, temp DBs, ~2 s
```
A fresh DB is seeded with demo rows whose dates are relative to today. Seeds never run on a DB that already has data.

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `DATABASE_PATH` | `funding_readiness.db` | Production: `/data/funding_readiness.db` |
| `CORS_ORIGINS` | Render origin + localhost | Comma-separated. The UI is same-origin and doesn't need it |
| `AUTH_MODE` | `off` | `off` · `writes` · `full` (see Auth) |
| `API_KEY` | — | Machine key (Hunter/scripts) sent as `X-API-Key`. The old public default is rejected |
| `TEAM_PASSCODE` | — | Shared browser passcode |
| `AUTH_SECRET` | derived | HMAC key for browser tokens. Rotating it signs everyone out |
| `AUTH_TOKEN_DAYS` | `30` | Browser token lifetime |
| `LOG_LEVEL` | `INFO` | Coercions, dedups and migrations are logged |

## Auth

| `AUTH_MODE` | Reads (`GET /api/*`) | Writes | UI |
|---|---|---|---|
| `off` | open | open | no prompt |
| `writes` | open | `X-API-Key` **or** passcode token | asks for the passcode on the first save |
| `full` | `X-API-Key` **or** token (`/api/health` stays public) | same | passcode screen on load; `/docs` is locked too |

`POST /api/auth/login {"passcode": "…"}` returns `{token, expires_at}`. Send the token as `Authorization: Bearer …`. Failed logins are limited to 10 per IP per 10 minutes. `GET /api/auth/status` returns `{mode, authenticated}`. The rollout steps are in [MIGRATION.md §6](MIGRATION.md#6-turn-on-auth-after-steps-24-are-green).

## API

### Opportunities
| Method | Path | Notes |
|---|---|---|
| GET | `/api/opportunities` | Filters: `status`, `type`, `category` (`startup`/`bd`; `pipeline` is the v1 alias), `min_fit_score`, `is_starred`/`is_saved`, `needs_check`, `source`, `q` (search name/notes/requirements/funding), `expired=include` (default) / `exclude` / `only`. Sort: `sort_by=deadline·fit_score·priority·created_at·updated_at·name`, `sort_order=asc·desc`; empty values always sort last |
| GET | `/api/opportunities/counts-by-type` | `?pipeline=&expired=` returns `{counts{type:n}, total, starred_count, needs_check_count, expired_count, by_category{all,startup,bd}}` (v1 keys `saved_count` and `auth_count` kept) |
| GET | `/api/opportunities/{id}` | |
| POST | `/api/opportunities` | Lenient (see Hunter contract). **Dedup:** a match returns the existing row with `200` and `X-Duplicate-Of: <id>`; `?allow_duplicate=true` bypasses it |
| PUT | `/api/opportunities/{id}` | Partial. Absent fields are untouched; an explicit `null` clears optional fields |
| PUT | `/api/opportunities/{id}/toggle-save` | Star/unstar |
| DELETE | `/api/opportunities/{id}` | |

**Opportunity fields:** `id, name, type, category, status, deadline, fit_score, priority, url, notes, funding_amount, requirements, contact_info, source, needs_check, access_status, is_starred (= is_saved), is_expired, days_left, created_at, updated_at`. Datetimes are ISO 8601 UTC (`…Z`).

- `type`: accelerator, grant, vc_fund, angel, corporate, government, competition, cloud_credits, rfp, tender, consulting, procurement, other
- `status` (UI label): researching, preparing, submitted (*Applied*), in_review (*Interview*), accepted (*Won*), rejected (*Lost*), closed
- `is_expired`: the deadline has passed while the status is still researching/preparing. It is computed on read and never stored.

### Readiness, dashboard, misc
| Method | Path | Notes |
|---|---|---|
| GET/POST | `/api/readiness-items` | Filters `status`, `category`, `owner`; categories legal, financial, pitch, technical, team, product, market, operations, documentation |
| GET/PUT/DELETE | `/api/readiness-items/{id}` | Status and % are reconciled (see below); `is_overdue` is computed |
| GET | `/api/dashboard-stats` | Active (not expired, rejected or closed), upcoming within 30 days, expired, needs-check, starred, by category/status/type, readiness, urgent items, recent activity |
| GET | `/api/activity-log?limit=1..100` | Includes `deduplicated` entries |
| GET | `/api/health` | `{status, version, schema_version}` (always public) |

### Hunter contract
Everything Hunter v3.3 sends is accepted, and nothing it sends is rejected:
- Unknown `type` becomes `other`, unknown `status` becomes `researching`, with aliases (`fellowship`→grant, `tor`→consulting, `applied`→submitted, `won`→accepted, …)
- `deadline` accepts `YYYY-MM-DD`, ISO strings with any offset, `YYYY-MM`, or Hunter's `…T00:00:00` suffix. Anything else becomes `null` and the text is appended to notes as *(Deadline as published: …)*
- `fit_score` is clamped to 0–100, `priority` to 1–5, and `name` is truncated at 200 characters
- Notes tags `[📋 BD | 🔒 NEEDS CHECK | Auto: Source]` fill `category`, `needs_check`, `access_status` and `source` when those fields aren't sent. Hunter v3.4 sends them explicitly and keeps the tag.
- `GET /api/opportunities` with no parameters returns **all** rows, expired included, for the Hunter's own dedup.

The suite in `tests/test_contract.py` replays Hunter's exact payload builder; `scripts/hunter_e2e.py` runs the real `hunter.py` against a server.

## Readiness score

```
score = Σ(wᵢ · pᵢ) / Σ wᵢ        wᵢ = 6 − priority   (P1 = 5 … P5 = 1)
pᵢ   = 100 if status = complete, else completion_percentage
```
**Consistency on write:** an explicitly sent status wins. `complete` sets 100%, `not_started` sets 0%, and `in_progress` caps at 99%. Otherwise a sent % drives the status: 100 → complete, 1–99 → in_progress (blocked stays blocked), 0 on a complete item → not_started. Category bars show the unweighted average of pᵢ.

## Dedup rules (server and merge script)

Two opportunities are the same programme when any of these hold:
1. **Same normalized URL** (lower-case, no `www.`, query or fragment) **and** canonical-name word overlap ≥ 0.5
2. **Same canonical name** (Hunter's `canonicalize_name`: years and cohort numbers dropped, "for startups" normalized), longer than 10 characters
3. **Title similarity ≥ 0.92 with the same type**

The thresholds are deliberately conservative: "Techstars Boston" vs "Techstars London" scores 0.89, and "Seedstars Open Calls" vs "Seedstars Demo Days" share one URL.

## Database & migrations

SQLite in WAL mode, `busy_timeout=5000`, one uvicorn worker. The schema version lives in `PRAGMA user_version`; migrations are in `backend/migrations.py` and are **additive and idempotent**. Before any pending migration the app writes `<db>.pre-v<N>-<ts>.bak`. To add a migration, append `(N, fn)` to `MIGRATIONS`, only `ADD COLUMN` / `CREATE INDEX IF NOT EXISTS`, and write a test in `tests/test_migration.py`.

## Runbook

| Situation | Do this |
|---|---|
| Deploy / migrate / roll back | [MIGRATION.md](MIGRATION.md) |
| Back up now | `python scripts/backup_via_api.py --api …/api [--api-key …]` plus the Render Shell copy (MIGRATION §1B) |
| Hunter Action is **red** | Read the last log lines. *"All N AI calls failed"*: the model was retired, so set the `HUNTER_MODEL` variable. *"All N dashboard POSTs failed"*: check `DASHBOARD_API_URL`/`DASHBOARD_API_KEY` against Render's `API_KEY` |
| Hunter is green but nothing is new | Normal on quiet days. Check the "New to evaluate" and "Passed AI filter" counts in the log |
| Duplicates crept in | `python scripts/merge_duplicates.py --api …` (dry run), review, then `--apply` |
| Team locked out | Render → set `AUTH_MODE=writes` (or `off`) → restart. Rotate `TEAM_PASSCODE` and `AUTH_SECRET` if the passcode leaked |
| UI looks broken | `python scripts/ui_smoke.py --base https://rootrise-funding-readiness.onrender.com` (read-only) |
| "database is locked" | Make sure only one instance/worker runs (`start.sh` uses `--workers 1`) |

## Project layout

```
backend/   main.py · auth.py · migrations.py · normalize.py
frontend/  index.html · sw.js · static/{app.js, app.css, manifest.json, icons}
scripts/   backup_via_api.py · build_db_from_export.py · merge_duplicates.py · ui_smoke.py · hunter_e2e.py
tests/     pytest suite (CRUD, clearing, contract, dedup, migration, stats, auth)
docs/      screenshots/before · screenshots/after · dedup-dry-run.txt
```

Proprietary · DEVONEERS © 2026
