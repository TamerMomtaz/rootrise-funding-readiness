# RootRise FRD — Diagnosis Report (v3.2 → v4.0)

**Date:** 2026-10-12  ·  **Scope:** `rootrise-funding-readiness` (backend + frontend) and its contract with `rootrise-opportunity-hunter` v3.3
**Method:** source read of every file in both repos, read-only probes of the live API, a headless-Chromium pass over the live UI (1440px + 375px), and the last 12 Hunter GitHub Actions runs.

Live snapshot used as evidence (read-only, saved as a JSON backup before any work):

| Endpoint | Result |
|---|---|
| `GET /api/health` | `200 {"status":"healthy","version":"1.1.0"}` (code says v1.1, not "1.0.0" and not "v3.2") |
| `GET /api/opportunities` | 125 rows, ids 1–125, newest `created_at` **2026-06-15** |
| `GET /api/readiness-items` | 17 rows (the Jan-2026 seed set, untouched since) |
| `GET /api/activity-log?limit=100` | 99 `created`, 1 `updated`; newest entry 2026-08-08 |
| `GET /api/dashboard-stats` | `upcoming_deadlines: 0`, `active_opportunities: 125` (includes all 26 expired rows) |

Severity key: **P0** broken / data loss · **P1** wrong results or security exposure · **P2** robustness / tech debt / missing scoped features · **P3** cosmetic.
"✅ Verified" = reproduced with evidence; "❎ Not reproduced" = suspected issue that turned out not to exist as described.

---

## P0 — Broken or losing data

### P0-1 · The Hunter has added nothing for 4 months (model 404). ✅ Verified
- **Evidence:** run #281 (2026-10-11, conclusion `success`), job 114452159164:
  `⚠️ Batch eval error: Error code: 404 … 'message': 'model: claude-sonnet-4-20250514'` on all 5 batches → `Passed AI filter: 0 · Added to dashboard: 0`.
  The SDK's own warning in the same log: *"The model 'claude-sonnet-4-20250514' … will reach end-of-life on June 15th, 2026."* The newest opportunity in the DB was created **2026-06-15 10:35 UTC**, the same day.
- **Root cause:** `hunter.py:1206` and `hunter.py:1322` hard-code a retired model. `evaluate_batch` catches every exception and returns `None`s, so the workflow exits 0 and nobody is alerted.
- **Fix (Hunter repo, `fix/hunter-contract`):** take the model from `HUNTER_MODEL` (default: a current model), make text extraction robust to thinking blocks, handle `stop_reason == "refusal"`, and **exit non-zero when every AI batch fails**, so a dead model turns the Action red instead of green.

### P0-2 · Unauthenticated `POST /api/reset-database` deletes the production DB file. ✅ Verified (by reading code, *not* by calling it)
- **Evidence:** `backend/main.py:698-703` calls `os.remove(DATABASE_PATH)` and then reseeds. It is listed in the live `/openapi.json` path list. There is no auth on it.
- **Fix:** remove the route. Replace it with nothing; restores go through `MIGRATION.md`.

### P0-3 · Hunter payloads are rejected with 422 and silently dropped. ✅ Verified (contract analysis, reproduced locally in tests)
- **Evidence:** `hunter.py:1407` sends `"deadline": f"{opp.deadline}T00:00:00"`. The AI is asked for `YYYY-MM-DD or null`, but any free-text answer (`"Rolling"`, `"2026-03"`, `"TBD"`) becomes `"RollingT00:00:00"`, which fails `Optional[datetime]` (`main.py:87`) with a 422. The same happens with an unknown `type` (`main.py:86`, strict enum), `fit_score > 100`, `priority` outside 1–5, or a name longer than 200 characters. `hunter.py:1416-1424` only logs `❌ Failed to add`, so the opportunity is gone.
- **Fix:** lenient `mode="before"` validators. Unknown `type` becomes `other`, unknown `status` becomes `researching`, numbers are clamped, names are truncated, and unparseable deadlines become `null` with the raw text kept in `notes`. Every coercion is logged (`log.warning`). Nothing that the Hunter sends today is rejected.

### P0-4 · Editing an opportunity shifts its deadline by the browser's UTC offset on every save. ✅ Verified
- **Evidence:** live row id 3 "EBRD Star Venture (Cohort 5)". It was seeded as `2026-04-15T00:00:00` and is now `2026-04-14T22:00:00+00:00` after a single UI save on 2026-08-08 (activity id 118). `index.html:789` loads `deadline.slice(0,16)` into a `datetime-local` input, which the browser treats as *local* time. `index.html:820` then sends `new Date(deadline).toISOString()`, which converts it to UTC. In Cairo (UTC+2/+3) each save moves the deadline back 2–3 hours, and it can roll over to the previous day.
- **Fix:** the frontend uses a `date` input and sends `YYYY-MM-DD`. The backend stores deadlines as UTC midnight and reads naive values as UTC. Reload and save is now a no-op (covered by a test).

---

## P1 — Wrong results or security exposure

### P1-1 · Pipeline (Startup/BD) is inferred from `type` first and the notes tag second, so BD items are misfiled. ✅ Verified
- **Evidence:** 3 live rows are tagged `[📋 BD …]` by the Hunter but have a startup-group type: id 73 and id 120 (`government`) and id 72 (`other`). `index.html:500-506` (`getCategory`) checks the type lists first, so ids 73 and 120 appear under 🚀 Startup. The backend's `pipeline=bd` filter (`main.py:404-407`) and `counts-by-type` use type only. Because of this, the BD tab badge says **"All BD 16"** while the BD view renders **17** cards (headless capture).
- **Fix:** real columns `category`, `source`, `needs_check` and `access_status`, backfilled from the notes tags by a versioned migration (`PRAGMA user_version`). Only rows without a tag fall back to type. The Hunter will send these fields explicitly, and keeps the tag for backward compatibility.

### P1-2 · "🔒 Needs Check" works only by string-matching notes, and its count ignores the pipeline. ✅ Verified
- **Evidence:** `index.html:507` (`isAuthWall` checks `notes.includes('🔒 NEEDS CHECK')`) and `main.py:440` (`notes LIKE '%NEEDS CHECK%'`). In the BD view the tab shows **"🔒 Needs Check 2"** even though only 1 of those 2 rows is BD (`index.html:661` counts across *all* opportunities). Editing the notes in the UI silently clears the flag.
- **Fix:** the `needs_check` column, a `?needs_check=` filter, and per-pipeline counts from the server.

### P1-3 · Writes are completely open; `API_KEY` is never checked. ✅ Verified
- **Evidence:** `main.py:29` defines `API_KEY` with a public default (`rootrise-dev-key-2024`, also printed in `env.example`). No dependency reads it. Anyone can POST, PUT or DELETE.
- **Fix:** see the **Auth proposal** at the end of this report. It is implemented behind `AUTH_MODE`, so nothing changes until you set the env vars.

### P1-4 · CORS is `*` combined with `allow_credentials=True`. ✅ Verified
- **Evidence:** `main.py:352-358`. A live response to `Origin: https://evil.example` carries `access-control-allow-origin: *` and `access-control-allow-credentials: true`. Browsers refuse that combination for credentialed requests, so it is misconfigured as well as over-broad.
- **Fix:** an allowlist from `CORS_ORIGINS` (default: the Render origin plus localhost), `allow_credentials=False`, and only the methods and headers the app needs.

### P1-5 · All 26 expired opportunities still count as "active". ✅ Verified
- **Evidence:** live stats show `upcoming_deadlines: 0` and `active_opportunities: 125`. **All 26** rows that have a deadline are past it (the latest is TiE Women MENA, 2026-06-25; others include Techstars Boston 2025-06-11 and Google MENA 2026-01-30), and all 26 are still `researching`/`preparing`. "0 upcoming" is correct; "125 active" is not. The UI's "Priority Opportunities" shows "EBRD … 180d overdue" as the top card.
- **Fix:** a computed `is_expired` (past deadline while still pre-submission) on every row. Active, upcoming, top-priority and default list views exclude expired rows, and there is an **Expired** archive view. No row is modified.

### P1-6 · `sort_by=deadline asc` puts NULL deadlines first. ✅ Verified
- **Evidence:** `main.py:408`. The default sort is `deadline ASC`, and 99 of 125 rows have NULL deadlines (rolling programs), so they sort first.
- **Fix:** a whitelist dict mapping each sort key to SQL with `(col IS NULL) ASC, col` (NULLS LAST) and an `id` tiebreaker.

### P1-7 · Fields can never be cleared. ✅ Verified
- **Evidence:** `main.py:504` and `main.py:603` (`if value is not None:`). Sending `{"deadline": null}`, `{"url": null}` or `{"owner": null}` is silently ignored. The UI sends `null` for empty inputs, so a user who blanks a field sees the old value come back.
- **Fix:** use `model_dump(exclude_unset=True)`, so an explicit `null` clears nullable fields. Required fields (name, type, status, scores) reject `null` with a 422.

### P1-8 · `min_fit_score=0` is ignored. ✅ Verified
- **Evidence:** `main.py:394` (`if min_fit_score:`). **Fix:** `is not None`.

### P1-9 · Datetimes are naive strings in mixed formats and compared as strings. ✅ Verified
- **Evidence:** live deadlines appear both as `2026-04-14T22:00:00+00:00` (1 row) and as `YYYY-MM-DDTHH:MM:SS` (25 rows). `created_at` has microseconds. `main.py:651-656` compares `deadline <= ?` lexically against `datetime.utcnow().isoformat()`.
- **Fix:** a single `to_utc()` parser. Every write is stored as `YYYY-MM-DDTHH:MM:SS+00:00`. Every comparison happens on parsed aware datetimes in Python (the data set is small). API output is tz-aware ISO 8601. Existing rows are **not** rewritten (additive migration only), because reads normalize them.

### P1-10 · Duplicate opportunities (Google accelerator ×6, TechCrunch Battlefield ×5, Microsoft Founders Hub ×3, …). ✅ Verified
- **Evidence:** the dry-run of `scripts/merge_duplicates.py` against the live API (output in `docs/dedup-dry-run.txt`). There is no server-side uniqueness. The Hunter only dedups against titles it fetched itself (`hunter.py:1375-1393`), and it compares the *raw* scraped title rather than the AI-cleaned name it later posts.
- **Fix:** dedup on `POST /api/opportunities`. The match is the same normalized URL, **or** the same canonical name (Hunter v3.3 `canonicalize_name`), **or** a fuzzy title ratio of at least 0.92 with the same type. On a match the server returns **200 with the existing record** plus the header `X-Duplicate-Of: <id>`. The Hunter treats any 2xx as success, so the contract is unchanged, and the existing row's empty fields are filled from the new payload. The fuzzy threshold is deliberately high, because at the Hunter's 0.75 "Techstars Boston" ≈ "Techstars London" (0.89) and "Flat6Labs Cairo" ≈ "Flat6Labs KSA" (0.79) would wrongly merge. There is a one-off merge script with a dry-run default. **I have not run it in apply mode** and will only do so with your approval.

---

## P2 — Robustness, tech debt and missing scoped features

| # | Finding | Evidence | Fix |
|---|---|---|---|
| P2-1 | Dashboard race: stats render before opportunities load, so the breakdown shows 🚀 0 / 📋 0 and "Priority Opportunities" is empty | 375px live screenshot `docs/screenshots/before/mobile-dashboard.png`; `index.html:865` runs the loaders in parallel and `updateDashboardUI` reads the `opportunities` global (`index.html:583-605`) | Render after all loaders settle; per-panel loading, empty and error states |
| P2-2 | **No navigation at all on mobile**: the sidebar is translated off-screen at ≤768px and there is no toggle | `index.html:285` (`.sidebar{transform:translateX(-100%)}`), no `.open` toggle anywhere in JS; headless check `sidebar_toggle_exists: false` | Mobile top bar plus bottom tab bar |
| P2-3 | "All" filter tabs omit Angel, Corporate, Government and Other, so their 11 rows are unreachable by tab | `index.html:626-634`; live type counts angel 1, corporate 2, government 5, other 3 | Tabs are generated from server counts, so every type with rows gets a tab |
| P2-4 | Readiness score treats `status=complete` with `completion_percentage=0` as 0% | `main.py:664-666` uses only `completion_percentage` | Effective progress = 100 when complete. On write, status and % are kept consistent both ways. The formula is documented |
| P2-5 | Enum drift: the UI says "Pitch Materials" but stores `pitch`; there is no `technical` readiness category | `index.html:469`, `main.py:65-73` | Add `technical`. Accept the aliases `pitch_materials`→`pitch`, `applied`→`submitted`, `interview`→`in_review`, `won`→`accepted` and `lost`→`rejected`. Display labels are Applied, Interview, Won and Lost. See note ❎ below |
| P2-6 | SQLite has no WAL and no busy_timeout | `main.py:175-178` | `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON` per connection. `start.sh` runs a **single** uvicorn worker (no gunicorn), which is right for SQLite. Keep it that way |
| P2-7 | f-string `ORDER BY` (regex-guarded) | `main.py:408`, `main.py:557` | Whitelist dict |
| P2-8 | Deprecated APIs: `@app.on_event`, `datetime.utcnow()`, Pydantic v1 (`validator`, `.dict()`, `Query(regex=)`) | `main.py:15,365,380,502,601`; `requirements.txt` pins `pydantic==1.10.13`, `fastapi==0.103.2` | Lifespan, `datetime.now(timezone.utc)`, Pydantic v2 (`field_validator`, `model_dump`, `pattern=`); pinned, current versions |
| P2-9 | Version reporting is inconsistent (`1.1.0` in code, "v3.2" in conversation, "1.0.0" in the brief) | `main.py:349,696` | `APP_VERSION = "4.0.0"`, exposed in `/api/health` together with `schema_version` |
| P2-10 | `.gitignore` is committed as `gitignore` (no dot), so it is inactive and a local `*.db` or `.env` could be committed | repo root | Rename to `.gitignore` |
| P2-11 | Frontend HTML read without an explicit encoding | `main.py:709` (`open(frontend_path, "r")`) | `encoding="utf-8"` everywhere; served via `FileResponse` |
| P2-12 | Seed data is from Jan 2026 and long dead (Google MENA 2026-01-30, Web Summit Qatar 2026-01-27, …) | `main.py:275-328` | Seeds use dates relative to install time and run only on an **empty** DB. Live rows are not touched |
| P2-13 | PWA: `icon-120x120.png` is missing (404 live) and there is no service worker | `curl /static/icon-120x120.png` → 404; `/sw.js` → 404 | Remove the dead reference, add a minimal network-first SW and `/manifest.webmanifest` scope |
| P2-14 | No tests and no CI | — | `tests/` (pytest + TestClient) and a GitHub Actions workflow |
| P2-15 | Hunter: DuckDuckGo queries return junk (Reddit/Pornhub threads were the first batch on 2026-10-11), 13 of 33 RSS feeds fail, and `duckduckgo_search` has been renamed to `ddgs` | run #281 log | Out of dashboard scope; logged as follow-up in the summary |
| P2-16 | No "starred" UX beyond a raw toggle; no search, sort, countdown or archive | brief / UI | Built in v4.0 (starred view, live-count tabs, search, sort, countdown badges, Expired view) |

## P3 — Cosmetic

| # | Finding | Fix |
|---|---|---|
| P3-1 | Fonts are Cinzel/Raleway with bronze/teal; the brief asks for the DEVONEERS system (Inter, navy/gold/cream) | Restyled with Inter; navy `#0F1B2D`, gold `#C9A227`, cream `#F7F3EA`; the tagline is kept |
| P3-2 | Toast "Data refreshed!" on every 5-minute auto-refresh | Toast only on manual refresh |
| P3-3 | README describes Docker and Railway, which were deleted; Render is the host | Rewritten as a technical handoff |
| P3-4 | Priority badge and delete button are tightly packed on cards; tap targets are under 44px on mobile | New card layout |

---

## ❎ Suspected issues that did *not* reproduce as described

- **Docker/compose/nixpacks cruft:** already deleted upstream (commits `140d7bc`, `cd634e7`, `428ae52`). Nothing to remove.
- **`render.yaml` / `Procfile`:** they do not exist. Render runs `start.sh` (pip install plus a single uvicorn process). The service settings live in the Render dashboard. I left it that way and documented it.
- **Mojibake (`â‚¬`):** not present in the repo (file is UTF-8; `€` appears correctly in `main.py:294-295`) or in live data (`€30K` round-trips correctly). The real risk was the encoding-less `open()` (P2-11), which is fixed.
- **Applied/Interview/Won/Lost in docs or UI:** not found. The UI and backend both use `researching/preparing/submitted/in_review/accepted/rejected/closed`. I kept those as the canonical stored values (live data uses them) and added the human labels plus input aliases, so either vocabulary works.
- **API reports "1.0.0":** it reports `1.1.0`. Bumped to `4.0.0` regardless.
- **Saved/starred missing entirely:** a raw toggle (`is_saved`) already exists (v1.1). v4 exposes it as `is_starred` (alias, same column) with a proper Starred view.

---

## Auth proposal (needs your approval before it is switched on)

| Caller | Mechanism |
|---|---|
| **Hunter / scripts** | Header `X-API-Key: <API_KEY>`, compared in constant time. GitHub secret `DASHBOARD_API_KEY`. |
| **Browser (team)** | A shared **team passcode** (`TEAM_PASSCODE`). `POST /api/auth/login {passcode}` returns an HMAC-SHA256-signed token (`AUTH_SECRET`, 30-day expiry) kept in `localStorage` and sent as `Authorization: Bearer …`. No user table and no cookies, so there is no CSRF surface. Reads stay public (unchanged behaviour). Writes in the UI prompt for the passcode once per device. |
| **Rollout switch** | `AUTH_MODE=off` (default; current behaviour) → `AUTH_MODE=on`. Turn it on only after the Hunter secret is set, so no run is lost. |

Alternatives considered: Render basic-auth on the whole site (blocks the Hunter and makes reads private), and per-user accounts/JWT (overkill for a 4-person team).
