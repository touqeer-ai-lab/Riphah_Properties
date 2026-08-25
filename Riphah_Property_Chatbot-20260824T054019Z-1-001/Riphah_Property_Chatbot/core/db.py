"""SQLite access: one connection factory, one migration call, a few helpers.

SQLite is a deliberate choice for phase 1, not a shortcut. The whole working set
— a few thousand KB chunks and low-thousands of leads — fits comfortably, WAL
mode handles the read concurrency a chat widget generates, and it removes a
service from the deployment. `connect()` is the single seam to change if this
outgrows a file: everything above it speaks plain SQL and rows.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import config

SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"


def now() -> str:
    """UTC, second precision, ISO-8601. Every timestamp column uses this."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def minutes_ago(minutes: int) -> str:
    stamp = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes)
    return stamp.isoformat(timespec="seconds")


def seconds_from_now(seconds: float) -> str:
    stamp = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=seconds)
    return stamp.isoformat(timespec="seconds")


def days_from_now(days: int) -> str:
    stamp = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=days)
    return stamp.isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # Off by default in SQLite, and the schema leans on cascades for deletion.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` does nothing
# to a table that already exists, so a new column in schema.sql reaches fresh
# installs and silently misses every existing database. Each entry here is applied
# with an ALTER when absent, which keeps both paths in step.
#
# Keep the definitions identical to schema.sql, or a fresh install and a migrated
# one will disagree.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # Where the lead's contact route came from: typed into the conversation, or
    # taken from a signed-in account. Sales needs the distinction — a phone number
    # volunteered mid-enquiry is a warmer signal than one attached to an account.
    ("leads", "contact_source", "TEXT NOT NULL DEFAULT 'conversation'"),
    # Marketing consent, copied from the account at lead creation. Responding to an
    # active enquiry and adding someone to a nurture sequence are different things,
    # and only the second needs this.
    ("leads", "marketing_opt_in", "INTEGER NOT NULL DEFAULT 0"),
    # Gate the chat behind sign-in. Per portal, because it is a commercial
    # trade-off rather than a technical one: a gate raises the quality of every
    # captured lead and lowers the number of them. A property portal and an
    # admission portal can reasonably answer that differently.
    ("portals", "require_auth", "INTEGER NOT NULL DEFAULT 0"),
)


def ensure_column(conn: Any, table: str, column: str, ddl: str) -> bool:
    """Add a column if the table lacks it. Returns True when it was added."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


def migrate() -> None:
    """Idempotent: schema.sql is all CREATE ... IF NOT EXISTS, plus column adds."""
    config.ensure_dirs()
    with connect() as conn:
        conn.executescript(SCHEMA_PATH.read_text())
        for table, column, ddl in _ADDED_COLUMNS:
            if ensure_column(conn, table, column, ddl):
                print(f"[migrate] added {table}.{column}")


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Transaction that commits on success and rolls back on any exception.

    `with connect() as conn` commits but does not close, which leaks handles in a
    long-lived server process. This closes.
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def scalar(sql: str, params: tuple | list = ()) -> Any:
    conn = connect()
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def loads(value: Any, default: Any = None) -> Any:
    """Tolerant JSON read for TEXT columns holding JSON.

    Config columns are hand-editable (that is the point of storing them as data),
    so malformed JSON is an expected input. Returning the default keeps a bad
    edit to one portal from taking down the server.
    """
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def audit(actor: str, action: str, *, entity: str | None = None,
          entity_id: str | int | None = None, detail: Any = None) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO audit_log (actor, action, entity, entity_id, detail, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (actor, action, entity,
             None if entity_id is None else str(entity_id), dumps(detail), now()),
        )


def counts() -> dict[str, int]:
    """Readiness figures for /api/health and the CLI status command."""
    conn = connect()
    try:
        def n(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0])

        return {
            "portals": n("SELECT COUNT(*) FROM portals WHERE active = 1"),
            "portal_fields": n("SELECT COUNT(*) FROM portal_fields"),
            "documents": n("SELECT COUNT(*) FROM kb_documents WHERE retired_at IS NULL"),
            "documents_published": n(
                "SELECT COUNT(*) FROM kb_documents "
                "WHERE published = 1 AND retired_at IS NULL"),
            "chunks": n("SELECT COUNT(*) FROM kb_chunks"),
            "chunks_embedded": n("SELECT COUNT(*) FROM kb_chunks WHERE embedding IS NOT NULL"),
            "users": n("SELECT COUNT(*) FROM users"),
            "sessions": n("SELECT COUNT(*) FROM chat_sessions"),
            "messages": n("SELECT COUNT(*) FROM messages"),
            "leads": n("SELECT COUNT(*) FROM leads"),
            "leads_hot": n("SELECT COUNT(*) FROM leads WHERE qualification = 'hot'"),
            "webhooks_pending": n(
                "SELECT COUNT(*) FROM webhook_deliveries WHERE status = 'pending'"),
            "knowledge_gaps_open": n(
                "SELECT COUNT(*) FROM knowledge_gaps WHERE resolved_at IS NULL"),
        }
    finally:
        conn.close()
