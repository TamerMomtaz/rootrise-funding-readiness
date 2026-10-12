import os
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import main  # noqa: E402

V32_SCHEMA = """
CREATE TABLE opportunities (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, type TEXT NOT NULL,
    deadline TEXT, status TEXT NOT NULL DEFAULT 'researching', fit_score INTEGER DEFAULT 50, url TEXT,
    notes TEXT, funding_amount TEXT, requirements TEXT, contact_info TEXT, priority INTEGER DEFAULT 3,
    is_saved BOOLEAN DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE readiness_items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, category TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'not_started', owner TEXT, due_date TEXT, description TEXT,
    priority INTEGER DEFAULT 3, dependencies TEXT, completion_percentage INTEGER DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE activity_log (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
    entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, details TEXT, timestamp TEXT NOT NULL);
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("AUTH_MODE", "API_KEY", "TEAM_PASSCODE", "AUTH_SECRET"):
        monkeypatch.delenv(k, raising=False)


def _client(tmp_path, monkeypatch, seed: bool):
    db = str(tmp_path / "frd.db")
    monkeypatch.setattr(main, "DATABASE_PATH", db)
    if not seed:
        monkeypatch.setattr(main, "seed_initial_data", lambda cursor: None)
    return TestClient(main.app), db


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Empty database (no seeds)."""
    c, _ = _client(tmp_path, monkeypatch, seed=False)
    with c:
        yield c


@pytest.fixture
def seeded_client(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch, seed=True)
    with c:
        yield c


@pytest.fixture
def v32_db(tmp_path):
    """A database shaped exactly like production v1.1/v3.2 (user_version 0, no v4 columns)."""
    path = str(tmp_path / "v32.db")
    conn = sqlite3.connect(path)
    conn.executescript(V32_SCHEMA)
    rows = [
        # id, name, type, deadline, status, fit, url, notes, saved
        (1, "Google for Startups MENA", "accelerator", "2026-01-30T00:00:00", "preparing", 80,
         "https://startup.google.com/programs/accelerator/mena/", "Up to €30K. URGENT", 0),
        (2, "MERC USAID Program", "government", "2026-05-13T00:00:00", "researching", 70,
         "https://usaid.gov/merc", "[📋 BD | Auto: WebSearch] Strong fit as BD", 1),
        (3, "Some Portal Item", "accelerator", None, "researching", 65,
         "https://portal.example/item", "[🚀 STARTUP | 🔒 NEEDS CHECK | Auto: WebSearch] login wall", 0),
        (4, "UN Tender", "tender", "2026-04-14T22:00:00+00:00", "researching", 75,
         "https://ungm.org/t/1", "[📋 BD | ⚠️ BLOCKED | Auto: ReliefWeb] blocked page", 0),
        (5, "Manual Entry", "grant", None, "researching", 50, None, "typed by hand", 0),
    ]
    for (i, name, t, dl, st, fit, url, notes, saved) in rows:
        conn.execute("""INSERT INTO opportunities (id, name, type, deadline, status, fit_score, url, notes,
                        priority, is_saved, created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,2,?,'2026-04-08T15:01:12.338267','2026-04-08T15:01:12.338267')""",
                     (i, name, t, dl, st, fit, url, notes, saved))
    conn.execute("""INSERT INTO readiness_items (name, category, status, completion_percentage, priority,
                    created_at, updated_at) VALUES ('Deck','pitch','complete',0,1,'2026-01-01T00:00:00','2026-01-01T00:00:00')""")
    conn.execute("INSERT INTO activity_log (action, entity_type, entity_id, details, timestamp) "
                 "VALUES ('created','opportunity',1,'x','2026-04-08T15:01:12.338267')")
    conn.commit()
    conn.close()
    return path


def make_opp(client, **kw):
    body = {"name": "Test Opportunity", "type": "grant"}
    body.update(kw)
    r = client.post("/api/opportunities?allow_duplicate=true", json=body)
    assert r.status_code == 200, r.text
    return r.json()
