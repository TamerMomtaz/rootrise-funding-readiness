import os
import sqlite3

import main
import migrations
from fastapi.testclient import TestClient


def boot(monkeypatch, path):
    monkeypatch.setattr(main, "DATABASE_PATH", path)
    with TestClient(main.app) as c:
        return c.get("/api/opportunities").json(), c.get("/api/health").json()


def snapshot(path, table, cols):
    conn = sqlite3.connect(path)
    rows = conn.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY id").fetchall()
    conn.close()
    return rows


ORIGINAL_COLS = ["id", "name", "type", "deadline", "status", "fit_score", "url", "notes", "is_saved",
                 "created_at", "updated_at"]


def test_v32_db_migrates_additively(monkeypatch, v32_db):
    before = snapshot(v32_db, "opportunities", ORIGINAL_COLS)
    opps, health = boot(monkeypatch, v32_db)
    assert health["schema_version"] == migrations.SCHEMA_VERSION == 2
    # existing data untouched (additive only), no seeds added to a non-empty DB
    assert snapshot(v32_db, "opportunities", ORIGINAL_COLS) == before
    assert len(opps) == 5
    by_id = {o["id"]: o for o in opps}
    assert by_id[1]["category"] == "startup" and by_id[1]["source"] == "manual"
    assert by_id[1]["notes"] == "Up to €30K. URGENT"                  # utf-8 intact
    assert by_id[2]["category"] == "bd" and by_id[2]["source"] == "WebSearch" and by_id[2]["is_starred"]
    assert by_id[3]["needs_check"] is True and by_id[3]["access_status"] == "auth_required"
    assert by_id[4]["access_status"] == "blocked" and by_id[4]["needs_check"] is False
    assert by_id[4]["deadline"].startswith("2026-04-14T22:00:00")
    assert by_id[5]["category"] == "startup" and by_id[5]["url"] is None
    backups = [f for f in os.listdir(os.path.dirname(v32_db)) if f.startswith("v32.db.pre-v2-")]
    assert len(backups) == 1
    bak = os.path.join(os.path.dirname(v32_db), backups[0])
    assert snapshot(bak, "opportunities", ORIGINAL_COLS) == before


def test_migration_is_idempotent(monkeypatch, v32_db):
    boot(monkeypatch, v32_db)
    conn = sqlite3.connect(v32_db)
    conn.execute("UPDATE opportunities SET category='startup' WHERE id=2")   # user edit after migration
    conn.commit()
    # Force a re-run of every migration: must not re-backfill or fail on existing columns.
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.row_factory = sqlite3.Row
    migrations.run_migrations(conn, v32_db, backup=False)
    assert conn.execute("SELECT category FROM opportunities WHERE id=2").fetchone()[0] == "startup"
    assert migrations.get_version(conn) == 2
    conn.close()
    opps, _ = boot(monkeypatch, v32_db)
    assert len(opps) == 5


def test_fresh_install_seeds_current_dates(tmp_path, monkeypatch):
    opps, health = boot(monkeypatch, str(tmp_path / "fresh.db"))
    assert health["schema_version"] == 2
    assert opps and not any(o["is_expired"] for o in opps)
    assert not [f for f in os.listdir(tmp_path) if ".bak" in f]   # nothing to back up on a fresh DB


def test_build_db_from_export_roundtrip(tmp_path, monkeypatch):
    import json
    from build_db_from_export import main as build
    export = {"opportunities": [{"id": 7, "name": "A", "type": "grant", "deadline": None, "status": "researching",
                                 "fit_score": 50, "url": None, "notes": None, "funding_amount": None,
                                 "requirements": None, "contact_info": None, "priority": 3, "is_saved": False,
                                 "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"}],
              "readiness_items": [], "activity_log": []}
    src = tmp_path / "export.json"
    src.write_text(json.dumps(export), encoding="utf-8")
    build(str(src), str(tmp_path / "restored.db"))
    opps, _ = boot(monkeypatch, str(tmp_path / "restored.db"))
    assert [o["id"] for o in opps] == [7]
