"""Upsert normalised leads into the CRM, from any source.

Three properties this module is responsible for:

**Idempotent.** `(source_key, external_id)` is the key. A webhook redelivered
three times, or a lead that arrives by both push and pull, produces one row. This
is not a nicety — the chatbot retries failed deliveries with backoff, so
duplicates are the expected case, not an edge one.

**The CRM keeps its own columns.** An inbound `lead.updated` refreshes the
captured record — name, phone, budget, tier — because the source owns that. It
does **not** touch `status`, `assigned_owner`, or `first_response_at`, because the
CRM owns those. Getting this backwards is the classic two-way-sync bug: a
consultant marks a lead 'contacted', the visitor sends one more message, and the
lead reappears in the new queue.

**Cross-source identity.** `person_key` is set from the normalised phone or email,
so the same human arriving from the chatbot and from a Meta form is visibly one
person even though they are two source rows. The rows are not merged: each remains
the record of what that source captured.
"""
from __future__ import annotations

from typing import Any

from core import db, security
from sources.base import NormalisedLead

# Columns refreshed from the source on every upsert. Anything not in this list is
# CRM-owned and survives an update untouched.
SOURCE_OWNED = (
    "portal", "name", "email", "phone", "email_norm", "phone_norm", "person_key",
    "qualification", "score", "language", "utm_source", "utm_medium",
    "utm_campaign", "referrer", "device", "region", "landing_url", "channel",
    "consent_given", "consent_version", "contact_source", "marketing_opt_in",
    "has_account", "message_count", "session_count",
    "transcript_url", "captured_at", "source_updated_at", "raw_payload",
)


def register_source(key: str, display_name: str, *, status: str = "pending",
                    detail: str | None = None) -> None:
    stamp = db.now()
    with db.tx() as conn:
        conn.execute(
            """
            INSERT INTO sources (key, display_name, status, detail, created_at,
                                 updated_at)
                 VALUES (?,?,?,?,?,?)
            ON CONFLICT (key) DO UPDATE SET
                 display_name = excluded.display_name,
                 status = excluded.status,
                 detail = excluded.detail,
                 updated_at = excluded.updated_at
            """,
            (key, display_name, status, detail, stamp, stamp),
        )


def upsert(lead: NormalisedLead, *, actor: str = "system") -> dict[str, Any]:
    """Insert or refresh one lead. Returns what happened and to which row."""
    if not lead.external_id:
        return {"ok": False, "reason": "source payload had no id"}

    email_norm = security.normalise_email(lead.email)
    phone_norm = security.normalise_phone(lead.phone)
    person = security.person_key(lead.email, lead.phone)
    stamp = db.now()

    values: dict[str, Any] = {
        "portal": lead.portal,
        "name": lead.name,
        "email": lead.email,
        "phone": lead.phone,
        "email_norm": email_norm,
        "phone_norm": phone_norm,
        "person_key": person,
        "qualification": lead.qualification,
        "score": lead.score,
        "language": lead.language,
        "utm_source": lead.utm_source,
        "utm_medium": lead.utm_medium,
        "utm_campaign": lead.utm_campaign,
        "referrer": lead.referrer,
        "device": lead.device,
        "region": lead.region,
        "landing_url": lead.landing_url,
        "channel": lead.channel,
        "consent_given": int(lead.consent_given),
        "consent_version": lead.consent_version,
        "contact_source": lead.contact_source or "conversation",
        "marketing_opt_in": int(lead.marketing_opt_in),
        "has_account": int(lead.has_account),
        "message_count": lead.message_count,
        "session_count": lead.session_count,
        "transcript_url": lead.transcript_url,
        "captured_at": lead.captured_at or stamp,
        "source_updated_at": lead.source_updated_at,
        "raw_payload": db.dumps(lead.raw_payload),
    }

    with db.tx() as conn:
        existing = conn.execute(
            "SELECT id, qualification, status, assigned_owner FROM leads "
            " WHERE source_key = ? AND external_id = ?",
            (lead.source_key, lead.external_id),
        ).fetchone()

        if existing:
            lead_id = existing["id"]
            sets = ", ".join(f"{col} = ?" for col in values)
            conn.execute(
                f"UPDATE leads SET {sets}, updated_at = ? WHERE id = ?",
                (*values.values(), stamp, lead_id),
            )
            created = False
            tier_changed = existing["qualification"] != lead.qualification
        else:
            # `status` and `assigned_owner` are applied only here, on first insert.
            # After that they belong to the CRM.
            columns = ["source_key", "external_id", *values,
                       "status", "assigned_owner", "created_at", "updated_at"]
            conn.execute(
                f"INSERT INTO leads ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(columns))})",
                (lead.source_key, lead.external_id, *values.values(),
                 lead.status or "new", lead.assigned_owner, stamp, stamp),
            )
            lead_id = conn.execute(
                "SELECT id FROM leads WHERE source_key = ? AND external_id = ?",
                (lead.source_key, lead.external_id),
            ).fetchone()["id"]
            created = True
            tier_changed = True

        # Captured fields are replaced wholesale rather than merged. The source's
        # current view is authoritative, and a field the source has dropped should
        # disappear here too rather than linger as a stale value a consultant acts
        # on.
        conn.execute("DELETE FROM lead_fields WHERE lead_id = ?", (lead_id,))
        needs = set(lead.needs_confirmation or [])
        for field_key, value in (lead.fields or {}).items():
            conn.execute(
                "INSERT INTO lead_fields (lead_id, field_key, value, label, "
                "needs_confirmation) VALUES (?,?,?,?,?)",
                (lead_id, field_key,
                 value if isinstance(value, str) else db.dumps(value),
                 lead.field_labels.get(field_key), int(field_key in needs)),
            )

        conn.execute(
            "UPDATE sources SET leads_received = leads_received + ?, "
            "last_sync_at = ?, updated_at = ? WHERE key = ?",
            (1 if created else 0, stamp, stamp, lead.source_key),
        )

    db.log_activity(
        lead_id, actor, "ingested",
        {"source": lead.source_key, "external_id": lead.external_id,
         "created": created, "qualification": lead.qualification},
    )

    return {"ok": True, "lead_id": lead_id, "created": created,
            "tier_changed": tier_changed, "qualification": lead.qualification,
            "external_id": lead.external_id}


# ------------------------------------------------------------------ CRM-owned

def set_status(lead_id: int, *, status: str | None = None,
               assigned_owner: str | None = None, actor: str = "agent",
               write_back: bool = True) -> dict[str, Any]:
    """Update the sales process. CRM-owned, optionally mirrored to the source.

    `first_response_at` is stamped the first time a lead leaves 'new'. That single
    timestamp is what every response-time and SLA figure in the analytics is built
    from, so it is set here rather than inferred later from the activity log.
    """
    lead = db.one("SELECT * FROM leads WHERE id = ?", (lead_id,))
    if not lead:
        return {"ok": False, "reason": "no such lead"}

    updates, params = [], []
    if status:
        from sources.base import VALID_STATUSES

        if status not in VALID_STATUSES:
            return {"ok": False, "reason": f"status must be one of {VALID_STATUSES}"}
        updates.append("status = ?")
        params.append(status)
        if status != "new" and not lead["first_response_at"]:
            updates.append("first_response_at = ?")
            params.append(db.now())
        if status in ("converted", "lost") and not lead["closed_at"]:
            updates.append("closed_at = ?")
            params.append(db.now())
    if assigned_owner is not None:
        updates.append("assigned_owner = ?")
        params.append(assigned_owner or None)

    if not updates:
        return {"ok": False, "reason": "nothing to update"}

    params.extend([db.now(), lead_id])
    with db.tx() as conn:
        conn.execute(
            f"UPDATE leads SET {', '.join(updates)}, updated_at = ? WHERE id = ?",
            params,
        )

    if status:
        db.log_activity(lead_id, actor, "status",
                        {"from": lead["status"], "to": status})
    if assigned_owner is not None:
        db.log_activity(lead_id, actor, "assigned", {"owner": assigned_owner})

    # Mirror to the originating system so both dashboards agree. Best-effort: the
    # CRM is the system of record for status, so a failure here is logged, not
    # fatal.
    mirrored = False
    if write_back and lead["source_key"] == "chatbot":
        from sources.chatbot import SOURCE as chatbot

        mirrored = chatbot.push_status(lead["external_id"], status=status,
                                       assigned_owner=assigned_owner)

    return {"ok": True, "lead_id": lead_id, "status": status,
            "assigned_owner": assigned_owner, "mirrored_to_source": mirrored}


def add_note(lead_id: int, body: str, *, author: str) -> dict[str, Any]:
    text = (body or "").strip()
    if not text:
        return {"ok": False, "reason": "empty note"}
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO notes (lead_id, author, body, created_at) VALUES (?,?,?,?)",
            (lead_id, author, text[:4000], db.now()),
        )
    db.log_activity(lead_id, author, "note", text[:200])
    return {"ok": True, "note_id": cur.lastrowid}


# ------------------------------------------------------------ reading a lead

def detail(lead_id: int) -> dict[str, Any] | None:
    """One lead with fields, notes, activity, and any duplicate persons."""
    lead = db.one("SELECT * FROM leads WHERE id = ?", (lead_id,))
    if not lead:
        return None

    lead["raw_payload"] = db.loads(lead["raw_payload"], {})
    fields = db.query(
        "SELECT field_key, value, label, needs_confirmation FROM lead_fields "
        " WHERE lead_id = ? ORDER BY field_key", (lead_id,)
    )
    notes = db.query(
        "SELECT id, author, body, created_at FROM notes WHERE lead_id = ? "
        " ORDER BY id DESC", (lead_id,)
    )
    activity = db.query(
        "SELECT actor, kind, detail, created_at FROM activity WHERE lead_id = ? "
        " ORDER BY id DESC LIMIT 50", (lead_id,)
    )
    for row in activity:
        row["detail"] = db.loads(row["detail"], row["detail"])

    # Other rows for the same human. Shown rather than merged: a chatbot
    # conversation and a Meta form fill are two pieces of evidence about one
    # buyer, and collapsing them would lose which said what.
    duplicates = []
    if lead["person_key"]:
        duplicates = db.query(
            "SELECT id, source_key, external_id, qualification, status, captured_at "
            "  FROM leads WHERE person_key = ? AND id != ? ORDER BY captured_at DESC",
            (lead["person_key"], lead_id),
        )

    transcript = db.one(
        "SELECT session_id, body, fetched_at FROM transcripts WHERE lead_id = ?",
        (lead_id,),
    )
    if transcript:
        transcript["body"] = db.loads(transcript["body"], [])

    return {
        "lead": lead,
        "fields": fields,
        "notes": notes,
        "activity": activity,
        "duplicates": duplicates,
        "transcript": transcript,
        "response_hours": db.hours_between(lead["captured_at"],
                                           lead["first_response_at"]),
    }


def cache_transcript(lead_id: int, session_id: str,
                     messages: list[dict[str, Any]]) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO transcripts (lead_id, session_id, body, fetched_at) "
            "VALUES (?,?,?,?) ON CONFLICT (lead_id) DO UPDATE SET "
            "session_id = excluded.session_id, body = excluded.body, "
            "fetched_at = excluded.fetched_at",
            (lead_id, session_id, db.dumps(messages), db.now()),
        )


def listing(*, source: str | None = None, qualification: str | None = None,
            status: str | None = None, owner: str | None = None,
            portal: str | None = None, search: str | None = None,
            since: str | None = None, until: str | None = None,
            unassigned: bool = False, needs_confirmation: bool = False,
            include_spam: bool = False,
            sort: str = "captured_at", limit: int = 50,
            offset: int = 0) -> dict[str, Any]:
    """The filtered lead list behind the dashboard table."""
    clauses, params = ["1 = 1"], []
    if source:
        clauses.append("l.source_key = ?")
        params.append(source)
    if qualification:
        clauses.append("l.qualification = ?")
        params.append(qualification)
    elif not include_spam:
        # Spam is excluded from lead counts (scope document s7), so it is out of
        # the default view but reachable by filtering for it explicitly.
        clauses.append("l.qualification != 'spam'")
    if status:
        clauses.append("l.status = ?")
        params.append(status)
    if owner:
        clauses.append("l.assigned_owner = ?")
        params.append(owner)
    if unassigned:
        clauses.append("l.assigned_owner IS NULL")
    if portal:
        clauses.append("l.portal = ?")
        params.append(portal)
    if since:
        clauses.append("l.captured_at >= ?")
        params.append(since)
    if until:
        clauses.append("l.captured_at <= ?")
        params.append(until)
    if needs_confirmation:
        clauses.append(
            "EXISTS (SELECT 1 FROM lead_fields f WHERE f.lead_id = l.id "
            "        AND f.needs_confirmation = 1)"
        )
    if search:
        clauses.append("(l.name LIKE ? OR l.email LIKE ? OR l.phone LIKE ? "
                       "OR l.external_id LIKE ? OR l.utm_campaign LIKE ?)")
        params.extend([f"%{search}%"] * 5)

    # Whitelisted, because it is interpolated into the SQL.
    order = {
        "captured_at": "l.captured_at DESC",
        "score": "l.score DESC, l.captured_at DESC",
        "updated_at": "l.updated_at DESC",
        # Hot first, then warm, then cold — a sales queue, not an alphabet.
        "qualification": ("CASE l.qualification WHEN 'hot' THEN 0 WHEN 'warm' "
                          "THEN 1 WHEN 'cold' THEN 2 ELSE 3 END, "
                          "l.captured_at DESC"),
    }.get(sort, "l.captured_at DESC")

    where = " AND ".join(clauses)
    total = int(db.scalar(f"SELECT COUNT(*) FROM leads l WHERE {where}", params) or 0)
    rows = db.query(
        f"""
        SELECT l.id, l.source_key, l.external_id, l.portal, l.name, l.email,
               l.phone, l.qualification, l.score, l.status, l.assigned_owner,
               l.utm_campaign, l.channel, l.device, l.region, l.message_count,
               l.captured_at, l.first_response_at, l.updated_at,
               (SELECT COUNT(*) FROM lead_fields f
                 WHERE f.lead_id = l.id AND f.needs_confirmation = 1)
                 AS unconfirmed_fields,
               (SELECT COUNT(*) FROM notes n WHERE n.lead_id = l.id) AS note_count
          FROM leads l
         WHERE {where}
         ORDER BY {order}
         LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    for row in rows:
        row["response_hours"] = db.hours_between(row["captured_at"],
                                                 row["first_response_at"])
    return {"leads": rows, "total": total, "limit": limit, "offset": offset}
