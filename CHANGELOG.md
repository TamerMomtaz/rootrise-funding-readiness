# Changelog

## v4.0.0 (2026-10-12)

The overhaul release: it makes the dashboard correct again, catches it up with Hunter v3.3/v3.4, and locks it down. Evidence for every item is in [DIAGNOSIS.md](DIAGNOSIS.md).

### Fixed: data loss and wrong data
- **Hunter payloads are no longer dropped with 422.** Unknown `type` becomes `other` and unknown `status` becomes `researching`, with aliases (`fellowship`, `tor`, `applied`, `won`, …). Free-text deadlines (`"RollingT00:00:00"`) become `null` and the original text is kept in notes. Out-of-range `fit_score`/`priority` are clamped and over-long names truncated. Every coercion is logged.
- **Deadlines no longer drift** by the browser's UTC offset on each save (date-only inputs; the backend stores UTC midnight).
- **Pipelines are real data.** `category`, `source`, `needs_check` and `access_status` are columns, backfilled from the Hunter's notes tags. BD-tagged `government`/`other` rows are now in BD, not Startup.
- **Fields can be cleared.** An explicit `null` clears deadline, url, notes, owner and the other optional fields; required fields reject `null`.
- `min_fit_score=0` is honoured, and deadline sorting puts rolling (no-deadline) programmes last.
- **Expired opportunities** (past deadline, still researching/preparing) are flagged `is_expired`, excluded from active/upcoming/priority, and kept in an Expired archive. No row is modified.
- **Readiness score** counts `complete` items as 100% even when their % says 0. Status and % are kept consistent in both directions. The formula is documented and returned by the API.
- **Duplicates:** `POST /api/opportunities` returns the existing record (200, `X-Duplicate-Of`) for the same programme and fills its empty fields. `scripts/merge_duplicates.py` (dry-run by default) cleans up existing duplicates.
- Datetimes are timezone-aware UTC everywhere. Mixed legacy formats are parsed rather than compared as strings.

### Security
- Removed the public `POST /api/reset-database`, which deleted the DB file.
- `AUTH_MODE=off|writes|full` (default `off`). The Hunter and scripts use `X-API-Key`; the team uses a shared passcode that issues a signed 30-day token. Failed logins are throttled. The public default key from `env.example` is never accepted.
- CORS is restricted to `CORS_ORIGINS` and no longer combines `*` with credentials.
- SQLite runs in WAL mode with `busy_timeout`. `ORDER BY` comes from a whitelist.

### Added
- `GET /api/opportunities`: `category`, `needs_check`, `is_starred`, `source`, `q` (search) and `expired=include|exclude|only` filters; `days_left`, `is_expired` and `is_starred` fields.
- Dashboard stats: `expired_opportunities`, `needs_check_count`, `starred_count`, `opportunities_by_category`, `upcoming_opportunities` and `readiness_formula`. `counts-by-type` gains `by_category`, `expired_count` and `needs_check_count`.
- `GET /api/health` reports `version` and `schema_version`.
- Readiness category `technical`; alias `pitch_materials` maps to `pitch`.
- Versioned, additive migrations (`PRAGMA user_version`) with an automatic `.bak` copy of the DB before any pending migration.
- Scripts: `backup_via_api.py`, `build_db_from_export.py`, `merge_duplicates.py`, `ui_smoke.py` and `hunter_e2e.py`.
- `tests/`: 63 pytest tests, plus a GitHub Actions workflow.

### Frontend (rewritten)
- DEVONEERS design system (Inter; navy, gold and cream); RootRise identity and tagline kept.
- Mobile-first: a top bar and bottom tab bar replace the sidebar that was unreachable on phones. No horizontal scroll at 375px.
- Live-count filter tabs for every type present, plus Starred, 🔒 Needs check and Expired views.
- Search, sort, and deadline countdown badges (red <7d, amber <30d, grey when expired).
- Loading, empty and error/retry states for every panel; the race that blanked the dashboard breakdown is fixed.
- Passcode screen for `AUTH_MODE`. PWA service worker plus manifest fixes (the missing 120px icon is added).

### Changed
- Pydantic v2, FastAPI lifespan, pinned dependencies (`fastapi 0.118`, `pydantic 2.11`, `uvicorn 0.37`).
- Seeds use install-relative dates and only run on an empty DB.
- `gitignore` renamed to `.gitignore` (it was inactive).
- The API version moves from `1.1.0` to `4.0.0`.

### Compatibility
- Every field Hunter v3.3 sends is still accepted. Its notes tags are still parsed, and `GET /api/opportunities` with no parameters still returns every row, expired ones included, for the Hunter's own dedup.
- v1.x response fields are unchanged (`is_saved`, `pipeline=` and `counts-by-type` keys are all kept).
- A rollback to v1.1 code runs against the migrated database (verified).
