"""
Rebuild a v1.x/v3.2-shaped SQLite DB from an API JSON export (backup_via_api.py output).

Used to rehearse the v4 migration on a faithful copy of production, and as the
restore path if the Render disk is ever lost. Produces the *old* schema with
user_version = 0, exactly as production looks before v4.

    python scripts/build_db_from_export.py backups/frd-export-<stamp>.json /tmp/rehearsal.db
"""
import json
import sqlite3
import sys

OPP_COLS = ["id", "name", "type", "deadline", "status", "fit_score", "url", "notes", "funding_amount",
            "requirements", "contact_info", "priority", "is_saved", "created_at", "updated_at"]
ITEM_COLS = ["id", "name", "category", "status", "owner", "due_date", "description", "priority",
             "dependencies", "completion_percentage", "created_at", "updated_at"]
LOG_COLS = ["id", "action", "entity_type", "entity_id", "details", "timestamp"]


def main(src, dest):
    data = json.load(open(src, encoding="utf-8"))
    conn = sqlite3.connect(dest)
    conn.executescript("""
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
    """)
    for table, cols, rows in (("opportunities", OPP_COLS, data["opportunities"]),
                              ("readiness_items", ITEM_COLS, data["readiness_items"]),
                              ("activity_log", LOG_COLS, data["activity_log"])):
        for r in rows:
            vals = [r.get(c) for c in cols]
            conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    conn.commit()
    print({t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
           for t in ("opportunities", "readiness_items", "activity_log")})


if __name__ == "__main__":
    main(*sys.argv[1:3])
