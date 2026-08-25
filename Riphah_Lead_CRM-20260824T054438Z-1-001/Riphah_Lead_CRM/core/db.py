"""SQLite access for the CRM.

Intentionally a near-twin of the chatbot's `core/db.py` rather than a shared
import. The two services are separately deployable — different host, different
release cadence, possibly different machine — and a shared library would couple
their deploys for the sake of eighty lines. The duplication is the cheaper of the
two costs, and it is bounded: this file is helpers over `sqlite3`, and it does not
grow.

Normalisation, by contrast, is duplicated *dangerously* — see the note in
`core/security.py`.
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
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def days_ago(days: int) -> str:
    return (_dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(days=days)).isoformat(timespec="seconds")


def days_from_now(days: int) -> str:
    return (_dt.datetime.now(_dt.timezone.utc)
            + _dt.timedelta(days=days)).isoformat(timespec="seconds")


def minutes_ago(minutes: int) -> str:
    return (_dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")


def hours_between(start: str | None, end: str | None) -> float | None:
    """Hours between two ISO timestamps, tolerant of a missing or malformed one.

    Used for response-time analytics over data that arrived from another service,
    where a null or a differently-formatted timestamp is a realistic input and
    must not take down a dashboard.
    """
    if not start or not end:
        return None
    try:
        a = _dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = _dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=_dt.timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=_dt.timezone.utc)
    return round((b - a).total_seconds() / 3600, 2)


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a no-op on
# an existing table, so a new column in schema.sql would reach fresh installs and
# silently miss every deployed database. Keep these identical to schema.sql.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # 'conversation' | 'account' | 'mixed'. A phone number volunteered mid-enquiry
    # is a stronger buying signal than one attached to a login.
    ("leads", "contact_source", "TEXT"),
    # Marketing consent from the source. Responding to an enquiry needs none;
    # adding the person to a nurture sequence does.
    ("leads", "marketing_opt_in", "INTEGER NOT NULL DEFAULT 0"),
    ("leads", "has_account", "INTEGER NOT NULL DEFAULT 0"),
)


def ensure_column(conn: Any, table: str, column: str, ddl: str) -> bool:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


def migrate() -> None:
    config.ensure_dirs()
    with connect() as conn:
        conn.executescript(SCHEMA_PATH.read_text())
        for table, column, ddl in _ADDED_COLUMNS:
            if ensure_column(conn, table, column, ddl):
                print(f"[migrate] added {table}.{column}")


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
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
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def log_activity(lead_id: int | None, actor: str, kind: str,
                 detail: Any = None) -> None:
    """Append to the activity trail.

    Called on every mutation. The response-time and SLA reports are derived
    entirely from this table, so an un-logged status change is a hole in the
    analytics rather than a cosmetic omission.
    """
    with tx() as conn:
        conn.execute(
            "INSERT INTO activity (lead_id, actor, kind, detail, created_at) "
            "VALUES (?,?,?,?,?)",
            (lead_id, actor, kind,
             detail if isinstance(detail, str) else dumps(detail), now()),
        )


def counts() -> dict[str, Any]:
    conn = connect()
    try:
        def n(sql: str, params: tuple = ()) -> int:
            return int(conn.execute(sql, params).fetchone()[0])

        return {
            "leads": n("SELECT COUNT(*) FROM leads"),
            "leads_actionable": n(
                "SELECT COUNT(*) FROM leads WHERE qualification != 'spam'"),
            "hot": n("SELECT COUNT(*) FROM leads WHERE qualification = 'hot'"),
            "warm": n("SELECT COUNT(*) FROM leads WHERE qualification = 'warm'"),
            "cold": n("SELECT COUNT(*) FROM leads WHERE qualification = 'cold'"),
            "spam": n("SELECT COUNT(*) FROM leads WHERE qualification = 'spam'"),
            "unassigned": n(
                "SELECT COUNT(*) FROM leads WHERE assigned_owner IS NULL "
                " AND qualification IN ('hot','warm')"),
            "new": n("SELECT COUNT(*) FROM leads WHERE status = 'new'"),
            "converted": n("SELECT COUNT(*) FROM leads WHERE status = 'converted'"),
            "staff": n("SELECT COUNT(*) FROM staff WHERE disabled_at IS NULL"),
            "sources_live": n("SELECT COUNT(*) FROM sources WHERE status = 'live'"),
            "webhooks_received": n("SELECT COUNT(*) FROM inbound_webhooks"),
            "webhooks_rejected": n(
                "SELECT COUNT(*) FROM inbound_webhooks WHERE signature_valid = 0"),
        }
    finally:
        conn.close()
