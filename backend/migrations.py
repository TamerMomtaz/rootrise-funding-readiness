"""
Versioned, idempotent, additive SQLite migrations.

The schema version lives in `PRAGMA user_version`. A v1.x/v3.2 production DB
reports user_version = 0. Each migration:
  * only ADDs columns/indexes (never drops or rewrites user data),
  * checks before it acts, so re-running it is a no-op,
  * runs in its own transaction and bumps user_version on success.

Before any pending migration touches an existing database, a full copy is
written next to it (`<db>.pre-v<N>-<timestamp>.bak`) with SQLite's online
backup API.
"""

import logging
import sqlite3
from datetime import datetime, timezone

try:
    from .normalize import parse_notes_tags, category_for_type, normalize_url
except ImportError:  # running as `uvicorn main:app` from backend/
    from normalize import parse_notes_tags, category_for_type, normalize_url

log = logging.getLogger("frd.migrations")


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table, name, ddl):
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        log.info("migration: added %s.%s", table, name)


def m1_saved_flag(conn):
    """v1.1 baseline: starred/saved flag (was applied ad-hoc before versioning)."""
    _add_column(conn, "opportunities", "is_saved", "BOOLEAN DEFAULT 0")


def m2_structured_opportunity_fields(conn):
    """v4.0: real columns for what used to be parsed out of `notes` tags."""
    _add_column(conn, "opportunities", "category", "TEXT")
    _add_column(conn, "opportunities", "source", "TEXT")
    _add_column(conn, "opportunities", "needs_check", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "opportunities", "access_status", "TEXT")
    _add_column(conn, "opportunities", "url_normalized", "TEXT")
    backfill_structured_fields(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_opp_category ON opportunities(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_opp_url_norm ON opportunities(url_normalized)")


def backfill_structured_fields(conn) -> int:
    """Fill category/source/needs_check/access_status/url_normalized for rows that have
    none yet (category IS NULL). Runs inside m2 and on every boot, so rows written by an
    older build after a rollback are picked up on re-upgrade. Never touches filled rows."""
    if "category" not in _columns(conn, "opportunities"):
        return 0
    rows = conn.execute(
        "SELECT id, type, notes, url FROM opportunities WHERE category IS NULL"
    ).fetchall()
    for r in rows:
        tags = parse_notes_tags(r["notes"])
        conn.execute(
            """UPDATE opportunities
               SET category = ?, source = COALESCE(source, ?), needs_check = ?,
                   access_status = COALESCE(access_status, ?), url_normalized = ?
               WHERE id = ?""",
            (
                tags["category"] or category_for_type(r["type"]),
                tags["source"] or "manual",
                1 if tags["needs_check"] else 0,
                tags["access_status"],
                normalize_url(r["url"]) or None,
                r["id"],
            ),
        )
    if rows:
        log.info("backfilled category/source/needs_check for %d rows", len(rows))
    return len(rows)


MIGRATIONS = [
    (1, m1_saved_flag),
    (2, m2_structured_opportunity_fields),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def get_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def backup_database(conn, db_path: str, target_version: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_path = f"{db_path}.pre-v{target_version}-{stamp}.bak"
    dest = sqlite3.connect(dest_path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    log.warning("migration: backup written to %s", dest_path)
    return dest_path


def run_migrations(conn, db_path: str = None, backup: bool = True) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    current = get_version(conn)
    pending = [(v, fn) for v, fn in MIGRATIONS if v > current]
    if not pending:
        return current
    has_data = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] > 0
    if backup and has_data and db_path and db_path != ":memory:":
        conn.commit()
        backup_database(conn, db_path, pending[-1][0])
    for version, fn in pending:
        try:
            fn(conn)
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.commit()
            log.info("migration: schema now at v%d (%s)", version, fn.__name__)
        except Exception:
            conn.rollback()
            log.exception("migration v%d failed; database left at v%d", version, get_version(conn))
            raise
    return get_version(conn)
