"""Chat sessions and message persistence.

Two properties the rest of the system depends on:

**A message is persisted before it is processed.** If retrieval, the model call,
or the extraction pass fails, the visitor's message is already stored. Losing the
message as well as the answer turns one bad turn into a lost conversation.

**One store for text and voice.** A visitor can speak, reload the page, and carry
on by typing with the thread intact. That matters more than it sounds: the
Realtime API is stateless, so a dropped call reconnects into a blank session
unless the transcript lives server-side and gets replayed into it.

Sessions also carry the lifecycle status from the scope document (s9) — active
while messages flow, idle after a short silence, inactive after a longer one — and
the source metadata (UTM, referrer, device) that the CRM attributes leads by.
"""
from __future__ import annotations

import uuid
from typing import Any

import config
from core import db, security
from portals import registry

TITLE_MAX = 70


# ------------------------------------------------------------------- creation

def create(*, portal_key: str, visitor_id: str | None = None,
           user_id: int | None = None, channel: str = "text",
           landing_url: str | None = None, referrer: str | None = None,
           utm: dict[str, str] | None = None, device: str | None = None,
           region: str | None = None, ip: str | None = None,
           session_id: str | None = None) -> dict[str, Any]:
    """Mint a session. The id is generated server-side (spec stage 2).

    A client-supplied id is accepted so the browser can reuse its localStorage id
    across a reload without a round-trip first — but it is validated as a UUID, or
    a caller could pass a guessable id and read someone else's transcript.
    """
    if session_id:
        try:
            uuid.UUID(session_id)
        except (ValueError, AttributeError, TypeError):
            session_id = None

    sid = session_id or str(uuid.uuid4())
    utm = utm or {}
    stamp = db.now()

    with db.tx() as conn:
        existing = conn.execute(
            "SELECT id FROM chat_sessions WHERE id = ?", (sid,)
        ).fetchone()
        if existing:
            return get(sid) or {}
        conn.execute(
            """
            INSERT INTO chat_sessions
                (id, portal_key, visitor_id, user_id, status, channel,
                 landing_url, referrer, utm_source, utm_medium, utm_campaign,
                 device, region, ip_hash, started_at, last_activity_at)
            VALUES (?,?,?,?,'active',?,?,?,?,?,?,?,?,?,?,?)
            """,
            (sid, portal_key, visitor_id, user_id, channel,
             (landing_url or "")[:500] or None, (referrer or "")[:300] or None,
             utm.get("utm_source"), utm.get("utm_medium"), utm.get("utm_campaign"),
             device, region, security.hash_ip(ip), stamp, stamp),
        )
    return get(sid) or {}


def ensure(session_id: str | None, *, portal_key: str, **kwargs: Any) -> str:
    """Return a usable session id, creating one when needed."""
    if session_id:
        row = db.one("SELECT id, sealed FROM chat_sessions WHERE id = ?", (session_id,))
        if row and not row["sealed"]:
            return session_id
        if row and row["sealed"]:
            # A sealed transcript is immutable (spec stage 9). A visitor returning
            # to a finalised conversation starts a new session rather than
            # reopening a lead that has already gone to the CRM.
            session_id = None
    return create(portal_key=portal_key, session_id=session_id, **kwargs)["id"]


def get(session_id: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM chat_sessions WHERE id = ?", (session_id,))


# -------------------------------------------------------------------- messages

def add_message(session_id: str, role: str, content: str | None, *,
                channel: str = "text", tool_name: str | None = None,
                tool_input: Any = None, tool_found: bool | None = None,
                citations: Any = None, language: str | None = None,
                tokens_in: int | None = None,
                tokens_out: int | None = None) -> int | None:
    """Append one turn and refresh the session's activity clock."""
    if role not in ("user", "assistant", "tool"):
        return None
    if not (content or "").strip() and not tool_name:
        return None

    stamp = db.now()
    with db.tx() as conn:
        session = conn.execute(
            "SELECT title, channel, sealed FROM chat_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not session:
            return None
        if session["sealed"]:
            return None

        cur = conn.execute(
            """
            INSERT INTO messages (session_id, role, content, tool_name, tool_input,
                                  tool_found, citations, channel, tokens_in,
                                  tokens_out, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (session_id, role, content, tool_name, db.dumps(tool_input),
             None if tool_found is None else int(tool_found), db.dumps(citations),
             channel, tokens_in, tokens_out, stamp),
        )

        updates = ["last_activity_at = ?", "status = 'active'"]
        params: list[Any] = [stamp]
        # The first visitor turn becomes the title shown in the history list.
        if role == "user" and content and not session["title"]:
            updates.append("title = ?")
            params.append(" ".join(content.split())[:TITLE_MAX])
        # A session that saw both channels is 'mixed' — the dashboard shows it, and
        # it is a genuinely useful signal about how visitors prefer to interact.
        if session["channel"] not in (channel, "mixed"):
            updates.append("channel = 'mixed'")
        if language:
            updates.append("language = ?")
            params.append(language)
        if role in ("user", "assistant"):
            updates.append("turn_count = turn_count + 1")

        params.append(session_id)
        conn.execute(
            f"UPDATE chat_sessions SET {', '.join(updates)} WHERE id = ?", params
        )
        return cur.lastrowid


def add_messages(session_id: str, turns: list[dict[str, Any]], *,
                 channel: str = "voice") -> int:
    """Batch append. The voice frontend flushes several turns at once, because a
    per-turn round-trip during a live call competes with the audio stream."""
    stored = 0
    for turn in turns:
        result = add_message(
            session_id, turn.get("role", "user"), turn.get("content"),
            channel=turn.get("channel") or channel,
            tool_name=turn.get("tool_name"), tool_input=turn.get("tool_input"),
            tool_found=turn.get("tool_found"), language=turn.get("language"),
        )
        stored += bool(result)
    return stored


def history(session_id: str, *, limit: int = config.MAX_HISTORY_TURNS,
            include_tools: bool = False) -> list[dict[str, Any]]:
    """Recent turns, oldest-first — the order a model expects."""
    roles = ("user", "assistant", "tool") if include_tools else ("user", "assistant")
    placeholders = ", ".join("?" * len(roles))
    rows = db.query(
        f"""
        SELECT id, role, content, tool_name, tool_input, tool_found, citations,
               channel, created_at
          FROM messages
         WHERE session_id = ? AND role IN ({placeholders})
         ORDER BY id DESC LIMIT ?
        """,
        (session_id, *roles, limit),
    )
    return list(reversed(rows))


def transcript(session_id: str) -> dict[str, Any]:
    """Everything needed to rehydrate the UI or render a dashboard detail view."""
    session = get(session_id)
    if not session:
        return {"found": False, "session_id": session_id}
    messages = db.query(
        "SELECT id, role, content, tool_name, tool_input, tool_found, citations, "
        "       channel, created_at "
        "  FROM messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    )
    for message in messages:
        message["citations"] = db.loads(message["citations"], [])
        message["tool_input"] = db.loads(message["tool_input"], None)
    return {"found": True, "session": session, "messages": messages}


def turn_count(session_id: str) -> int:
    return int(db.scalar(
        "SELECT turn_count FROM chat_sessions WHERE id = ?", (session_id,)
    ) or 0)


# --------------------------------------------------------------------- listing

def recent(*, where: str = "1 = 1", params: list[Any] | None = None,
           limit: int = 30, include_empty: bool = False) -> list[dict[str, Any]]:
    """Sessions for a history list, newest first.

    Empty sessions are hidden by default: a row is created the moment the widget
    loads, so listing them would fill the panel with untitled zero-turn entries.
    """
    clauses = [where]
    if not include_empty:
        clauses.append("turn_count > 0")
    return db.query(
        f"""
        SELECT s.id, s.title, s.status, s.channel, s.language, s.turn_count,
               s.started_at, s.last_activity_at, s.portal_key,
               (SELECT l.lead_ref FROM lead_sessions ls
                  JOIN leads l ON l.id = ls.lead_id
                 WHERE ls.session_id = s.id LIMIT 1) AS lead_ref
          FROM chat_sessions s
         WHERE {' AND '.join(clauses)}
         ORDER BY s.last_activity_at DESC
         LIMIT ?
        """,
        [*(params or []), limit],
    )


def admin_listing(*, q: str | None = None, portal: str | None = None,
                  limit: int = 100) -> list[dict[str, Any]]:
    """Every visitor conversation, newest activity first — the staff chat monitor.

    No ownership clause on purpose: the caller must sit behind require_admin.
    Each row carries enough for a message-list pane — who the visitor is (lead
    name, then account name/email, then the session title), the last message as
    a preview, and the lead tier when the conversation produced one.
    """
    clauses = ["s.turn_count > 0"]
    params: list[Any] = []
    if portal:
        clauses.append("s.portal_key = ?")
        params.append(portal)
    if q:
        like = f"%{q.strip()}%"
        clauses.append(
            "(s.title LIKE ? OR u.name LIKE ? OR u.email LIKE ? OR EXISTS "
            "(SELECT 1 FROM messages m WHERE m.session_id = s.id "
            "AND m.content LIKE ?))"
        )
        params.extend([like, like, like, like])
    return db.query(
        f"""
        SELECT s.id, s.title, s.status, s.channel, s.language, s.turn_count,
               s.started_at, s.last_activity_at, s.portal_key, s.sealed,
               u.name AS user_name, u.email AS user_email,
               (SELECT l.lead_ref FROM lead_sessions ls
                  JOIN leads l ON l.id = ls.lead_id
                 WHERE ls.session_id = s.id LIMIT 1) AS lead_ref,
               (SELECT l.qualification FROM lead_sessions ls
                  JOIN leads l ON l.id = ls.lead_id
                 WHERE ls.session_id = s.id LIMIT 1) AS lead_tier,
               (SELECT l.name FROM lead_sessions ls
                  JOIN leads l ON l.id = ls.lead_id
                 WHERE ls.session_id = s.id LIMIT 1) AS lead_name,
               (SELECT m.content FROM messages m
                 WHERE m.session_id = s.id AND m.role IN ('user', 'assistant')
                   AND m.content IS NOT NULL
                 ORDER BY m.id DESC LIMIT 1) AS last_message,
               (SELECT m.role FROM messages m
                 WHERE m.session_id = s.id AND m.role IN ('user', 'assistant')
                   AND m.content IS NOT NULL
                 ORDER BY m.id DESC LIMIT 1) AS last_role
          FROM chat_sessions s
          LEFT JOIN users u ON u.id = s.user_id
         WHERE {' AND '.join(clauses)}
         ORDER BY s.last_activity_at DESC
         LIMIT ?
        """,
        [*params, max(1, min(limit, 300))],
    )


def delete(session_id: str, *, where: str = "1 = 1",
           params: list[Any] | None = None) -> bool:
    """Delete a session and its messages. The `where` guard is the ownership check,
    passed in by the caller so this can't be used to delete someone else's chat."""
    with db.tx() as conn:
        cur = conn.execute(
            f"DELETE FROM chat_sessions WHERE id = ? AND {where}",
            (session_id, *(params or [])),
        )
    return cur.rowcount > 0


# --------------------------------------------------------------------- consent

def record_consent(session_id: str, *, portal_key: str,
                   granted: bool = True) -> dict[str, Any]:
    """Record consent with a timestamp and the exact notice version shown.

    The version matters: "the visitor consented" is not defensible six months
    later unless you can say what they were shown when they did.
    """
    portal = registry.get(portal_key)
    stamp = db.now()
    with db.tx() as conn:
        conn.execute(
            "UPDATE chat_sessions SET consent_given = ?, consent_version = ?, "
            "consent_at = ? WHERE id = ?",
            (int(granted), portal["consent_version"], stamp, session_id),
        )
    return {"consent_given": granted, "consent_version": portal["consent_version"],
            "consent_at": stamp}


def has_consent(session_id: str) -> bool:
    return bool(db.scalar(
        "SELECT consent_given FROM chat_sessions WHERE id = ?", (session_id,)
    ))


# ---------------------------------------------------------------- rate limiting

def rate_limit_exceeded(session_id: str) -> tuple[bool, int]:
    """Per-session message cap over a rolling window (spec stage 3).

    Counted from the messages table rather than an in-memory counter, so the limit
    survives a restart and applies across workers. At this traffic level the query
    is trivially cheap; a Redis counter would be the change if it stops being.
    """
    window_start = db.minutes_ago(
        max(1, config.RATE_LIMIT_WINDOW_SECONDS // 60)
    )
    count = int(db.scalar(
        "SELECT COUNT(*) FROM messages "
        " WHERE session_id = ? AND role = 'user' AND created_at > ?",
        (session_id, window_start),
    ) or 0)
    remaining = max(0, config.RATE_LIMIT_MESSAGES - count)
    return count >= config.RATE_LIMIT_MESSAGES, remaining


# ------------------------------------------------------------------- lifecycle

def close(session_id: str, *, seal: bool = True) -> bool:
    """Finalise a session: mark inactive, and seal the transcript (spec stage 9).

    Sealing makes the transcript immutable. Once the lead payload has gone to the
    CRM, the transcript that justified its score must not change underneath it.
    """
    stamp = db.now()
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE chat_sessions SET status = 'inactive', closed_at = ?, sealed = ? "
            " WHERE id = ? AND closed_at IS NULL",
            (stamp, int(seal), session_id),
        )
    return cur.rowcount > 0


def prune_empty(*, older_than_hours: int = 6) -> int:
    """Delete stale zero-turn sessions. Called on startup.

    The widget mints a session on load, so a portal with traffic accumulates rows
    for visitors who never typed anything.
    """
    with db.tx() as conn:
        cur = conn.execute(
            "DELETE FROM chat_sessions WHERE turn_count = 0 AND last_activity_at < ?",
            (db.minutes_ago(older_than_hours * 60),),
        )
    return cur.rowcount
