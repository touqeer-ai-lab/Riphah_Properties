"""Lead record assembly, enrichment, deduplication and scoring (spec stage 7).

The creation trigger is deliberately low: **one contact route plus one
qualification field**. Waiting for a complete record means never creating one —
most conversations end mid-qualification, and a lead with a phone number and a
stated timeline is worth calling even with six fields empty.

Enrichment is monotonic. A stored value is never replaced by one with lower
confidence, so a clear statement early ("my budget is 2.5 crore") cannot be
clobbered by a vague inference later. A human correction from the dashboard
overrides everything, because a person looking at the transcript is the highest
authority there is.

Deduplication runs on normalised email and phone. A returning visitor enriches the
existing lead and attaches a new session to its history rather than creating a
second record — which is the difference between the CRM showing one interested
buyer and showing four.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from core import db, security
from leads import scoring
from portals import registry

CONTACT_KEYS = ("name", "email", "phone")


def _next_lead_ref(conn: Any) -> str:
    """`LD-2026-00041`. Sequential within the year, readable over the phone."""
    year = _dt.datetime.now(_dt.timezone.utc).year
    row = conn.execute(
        "SELECT lead_ref FROM leads WHERE lead_ref LIKE ? "
        " ORDER BY lead_ref DESC LIMIT 1",
        (f"LD-{year}-%",),
    ).fetchone()
    sequence = 1
    if row:
        try:
            sequence = int(row["lead_ref"].rsplit("-", 1)[1]) + 1
        except (IndexError, ValueError):
            sequence = 1
    return f"LD-{year}-{sequence:05d}"


# ------------------------------------------------------------------ reading state

def captured_for_session(session_id: str) -> dict[str, Any]:
    """Everything known about the visitor in this session, for the prompt.

    Merges two sources, session-staged first then lead values on top:

      * `session_field_values` — everything extracted in this conversation, whether
        or not a lead exists yet. This is what makes "never re-ask" hold from the
        very first statement rather than only after a contact route arrives.
      * the lead, when one exists — which extends the same guarantee across a
        returning visitor's *second* conversation.

    Lead values win on conflict because they have survived deduplication and any
    human correction.
    """
    out: dict[str, Any] = {}

    # A signed-in visitor's account details count as already-known. Without this the
    # assistant asks someone who signed up two minutes ago for the phone number
    # they typed into the signup form — which is precisely the "never re-ask"
    # failure the whole capture design exists to avoid.
    session = db.one("SELECT user_id FROM chat_sessions WHERE id = ?", (session_id,))
    account = _account_contact(session["user_id"]) if session else None
    if account:
        for key in CONTACT_KEYS:
            if account.get(key):
                out[key] = account[key]

    for row in db.query(
        "SELECT field_key, value FROM session_field_values WHERE session_id = ?",
        (session_id,),
    ):
        if row["value"] not in (None, ""):
            out[row["field_key"]] = row["value"]

    lead = lead_for_session(session_id)
    if lead:
        for key in CONTACT_KEYS:
            if lead.get(key):
                out[key] = lead[key]
        for row in db.query(
            "SELECT field_key, value FROM lead_field_values WHERE lead_id = ?",
            (lead["id"],),
        ):
            if row["value"] not in (None, ""):
                out[row["field_key"]] = row["value"]
    return out


def stage_session_fields(session_id: str, extracted: dict[str, dict[str, Any]], *,
                         source_message_id: int | None = None) -> list[str]:
    """Record extracted fields against the session, monotonic in confidence.

    Always called, before any decision about whether a lead exists. Cheap, and it
    is the only reason a visitor who states a budget on turn 3 is not asked for it
    again on turn 4.
    """
    if not extracted:
        return []

    changed: list[str] = []
    stamp = db.now()
    with db.tx() as conn:
        for field_key, payload in extracted.items():
            existing = conn.execute(
                "SELECT value, confidence FROM session_field_values "
                " WHERE session_id = ? AND field_key = ?",
                (session_id, field_key),
            ).fetchone()
            if existing:
                if payload["confidence"] < existing["confidence"]:
                    continue
                if str(existing["value"]) == str(payload["value"]):
                    continue
            conn.execute(
                """
                INSERT INTO session_field_values (session_id, field_key, value,
                                                  value_raw, confidence,
                                                  source_message_id, updated_at)
                     VALUES (?,?,?,?,?,?,?)
                ON CONFLICT (session_id, field_key) DO UPDATE SET
                     value = excluded.value,
                     value_raw = excluded.value_raw,
                     confidence = excluded.confidence,
                     source_message_id = excluded.source_message_id,
                     promoted = 0,
                     updated_at = excluded.updated_at
                """,
                (session_id, field_key, str(payload["value"]),
                 payload.get("value_raw"), payload["confidence"],
                 source_message_id, stamp),
            )
            changed.append(field_key)
    return changed


def _promote_session_fields(conn: Any, session_id: str, lead_id: int) -> list[str]:
    """Move staged session fields onto a newly created or newly linked lead.

    Runs inside the caller's transaction. Contact fields are skipped — they live
    in columns on `leads`, and were written when the lead was created.
    """
    rows = conn.execute(
        "SELECT field_key, value, value_raw, confidence, source_message_id "
        "  FROM session_field_values WHERE session_id = ?",
        (session_id,),
    ).fetchall()

    promoted: list[str] = []
    for row in rows:
        if row["field_key"] in CONTACT_KEYS:
            continue
        existing = conn.execute(
            "SELECT confidence, source FROM lead_field_values "
            " WHERE lead_id = ? AND field_key = ?",
            (lead_id, row["field_key"]),
        ).fetchone()
        # A human correction on the lead outranks anything staged in a session.
        if existing and (existing["source"] == "human"
                         or existing["confidence"] > row["confidence"]):
            continue
        conn.execute(
            """
            INSERT INTO lead_field_values (lead_id, field_key, value, value_raw,
                                           confidence, source_message_id, source,
                                           updated_at)
                 VALUES (?,?,?,?,?,?, 'assistant', ?)
            ON CONFLICT (lead_id, field_key) DO UPDATE SET
                 value = excluded.value, value_raw = excluded.value_raw,
                 confidence = excluded.confidence,
                 source_message_id = excluded.source_message_id,
                 updated_at = excluded.updated_at
            """,
            (lead_id, row["field_key"], row["value"], row["value_raw"],
             row["confidence"], row["source_message_id"], db.now()),
        )
        promoted.append(row["field_key"])

    conn.execute(
        "UPDATE session_field_values SET promoted = 1 WHERE session_id = ?",
        (session_id,),
    )
    return promoted


def outstanding_for(portal_key: str, captured: dict[str, Any]) -> list[dict[str, Any]]:
    """Fields still wanted, in ask order — required ones first.

    The prompt asks for at most the top item, but the whole ordered list is passed
    so the model can pick a different one when the conversation makes a lower
    priority field the natural question.
    """
    fields = registry.get(portal_key)["fields"]
    missing = [
        f for f in fields
        if captured.get(f["field_key"]) in (None, "")
    ]
    missing.sort(key=lambda f: (not f["required"], f["sort_order"]))
    return missing


def has_contact(captured: dict[str, Any]) -> bool:
    return bool(
        security.normalise_email(captured.get("email"))
        or security.normalise_phone(captured.get("phone"))
    )


def _account_contact(user_id: int | None) -> dict[str, Any] | None:
    """Contact details from a signed-in account, usable as a lead's contact route.

    Why this is legitimate, and where the limit is:

    Responding to an active enquiry is not marketing. Someone who created an
    account, stated a budget and a timeline, and asked for floor plans has an open
    conversation with the company — a consultant calling them back is the thing
    they asked for. So the account's phone or email counts as a contact route.

    Adding that person to a nurture sequence *is* marketing, and needs
    `marketing_opt_in`. That flag travels with the lead so the CRM can honour the
    distinction rather than guessing, and `contact_source` records that the details
    came from the account rather than being volunteered mid-conversation — which is
    a genuinely weaker buying signal and shouldn't look like the stronger one.
    """
    if not user_id:
        return None
    row = db.one(
        "SELECT name, email, phone, marketing_opt_in FROM users "
        " WHERE id = ? AND disabled_at IS NULL", (user_id,)
    )
    if not row:
        return None
    return {
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "marketing_opt_in": bool(row["marketing_opt_in"]),
    }


def lead_for_session(session_id: str) -> dict[str, Any] | None:
    return db.one(
        """
        SELECT l.* FROM leads l
          JOIN lead_sessions ls ON ls.lead_id = l.id
         WHERE ls.session_id = ?
         ORDER BY l.id DESC LIMIT 1
        """,
        (session_id,),
    )


# ---------------------------------------------------------------- deduplication

def find_duplicate(conn: Any, *, portal_key: str, email: str | None,
                   phone: str | None) -> dict[str, Any] | None:
    """Existing lead for the same person on the same portal.

    Scoped to the portal on purpose: the same human enquiring about a property and
    about a degree programme is two different sales conversations owned by two
    different teams, and merging them would put a property lead in an admissions
    queue.
    """
    email_norm = security.normalise_email(email)
    phone_norm = security.normalise_phone(phone)
    if not (email_norm or phone_norm):
        return None

    clauses, params = [], [portal_key]
    if email_norm:
        clauses.append("email_norm = ?")
        params.append(email_norm)
    if phone_norm:
        clauses.append("phone_norm = ?")
        params.append(phone_norm)

    row = conn.execute(
        f"SELECT * FROM leads WHERE portal_key = ? AND ({' OR '.join(clauses)}) "
        f" ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------- assembly

def apply_extraction(*, session_id: str, portal_key: str,
                     extracted: dict[str, dict[str, Any]],
                     source_message_id: int | None = None) -> dict[str, Any] | None:
    """Fold one turn's extracted fields into a lead. The core of stage 7.

    Returns a summary dict when a lead exists or was created, else None. The
    summary reports `created` and `changed_fields` so the caller knows whether to
    fire `lead.created` or `lead.updated`.
    """
    if not extracted:
        return _rescore_existing(session_id, portal_key)

    session = db.one(
        "SELECT visitor_id, user_id, language, turn_count FROM chat_sessions "
        " WHERE id = ?", (session_id,)
    )
    if not session:
        return None

    # A signed-in visitor has already given the company their contact details, so
    # requiring them to retype an email into the chat before a lead exists loses
    # leads for no reason. Before this, someone could sign up with name, email and
    # phone, state a full set of requirements, ask for the floor plans — and
    # produce no lead at all, because nothing was *typed* in the conversation.
    account = _account_contact(session["user_id"])

    # Stage everything against the session first, unconditionally. This happens
    # before any decision about whether the lead trigger is met, so nothing said
    # early in a conversation is lost while waiting for a contact route — see the
    # session_field_values comment in schema.sql.
    stage_session_fields(session_id, extracted,
                         source_message_id=source_message_id)

    contact = {k: extracted[k]["value"] for k in CONTACT_KEYS if k in extracted}
    qualification_fields = {k: v for k, v in extracted.items() if k not in CONTACT_KEYS}

    # A contact route may have arrived this turn while the qualification fields
    # arrived earlier, so the trigger is evaluated against the merged session
    # state rather than against this turn alone.
    staged = {
        row["field_key"]: row["value"]
        for row in db.query(
            "SELECT field_key, value FROM session_field_values WHERE session_id = ?",
            (session_id,),
        )
    }
    for key in CONTACT_KEYS:
        if not contact.get(key) and staged.get(key):
            contact[key] = staged[key]

    # Precedence: what the visitor typed beats what the account holds. Someone who
    # says "actually call me on this other number" is telling you something, and the
    # account value must not overwrite it.
    #
    # Only email and phone count towards the source — a name is not a contact
    # route, and letting it vote made a lead whose details came entirely from the
    # account report itself as 'mixed'.
    ROUTE_KEYS = ("email", "phone")
    from_conversation = any(contact.get(k) for k in ROUTE_KEYS)
    from_account = False
    if account:
        for key in CONTACT_KEYS:
            if not contact.get(key) and account.get(key):
                contact[key] = account[key]
                from_account = from_account or key in ROUTE_KEYS
    contact_source = ("mixed" if from_conversation and from_account
                      else "account" if from_account
                      else "conversation")

    created = False
    changed: list[str] = []

    with db.tx() as conn:
        lead_row = conn.execute(
            "SELECT l.* FROM leads l JOIN lead_sessions ls ON ls.lead_id = l.id "
            " WHERE ls.session_id = ? ORDER BY l.id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        lead = dict(lead_row) if lead_row else None

        if not lead:
            lead = find_duplicate(conn, portal_key=portal_key,
                                  email=contact.get("email"),
                                  phone=contact.get("phone"))
            if lead:
                # Returning visitor: attach this session to the existing lead and
                # fold anything already staged in this conversation onto it.
                conn.execute(
                    "INSERT OR IGNORE INTO lead_sessions (lead_id, session_id, "
                    "linked_at) VALUES (?,?,?)",
                    (lead["id"], session_id, db.now()),
                )
                changed.append("session_linked")
                changed.extend(_promote_session_fields(conn, session_id, lead["id"]))

        if not lead:
            # Creation trigger: one contact route plus at least one qualification
            # field. Both halves matter — a bare email with no stated requirement
            # is not yet a lead a consultant can work.
            reachable = bool(
                security.normalise_email(contact.get("email"))
                or security.normalise_phone(contact.get("phone"))
            )
            # Counted from the merged session state, not this turn: the budget may
            # have been given four turns before the phone number.
            has_qualification = bool(qualification_fields) or any(
                key not in CONTACT_KEYS and value not in (None, "")
                for key, value in staged.items()
            )
            if not (reachable and has_qualification):
                return None

            lead_ref = _next_lead_ref(conn)
            stamp = db.now()
            cur = conn.execute(
                """
                INSERT INTO leads (lead_ref, portal_key, session_id, user_id, name,
                                   email, phone, email_norm, phone_norm, language,
                                   contact_source, marketing_opt_in,
                                   created_at, updated_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (lead_ref, portal_key, session_id, session["user_id"],
                 contact.get("name"), contact.get("email"), contact.get("phone"),
                 security.normalise_email(contact.get("email")),
                 security.normalise_phone(contact.get("phone")),
                 session["language"], contact_source,
                 int(bool(account and account.get("marketing_opt_in"))),
                 stamp, stamp),
            )
            lead = {"id": cur.lastrowid, "lead_ref": lead_ref, "portal_key": portal_key}
            conn.execute(
                "INSERT OR IGNORE INTO lead_sessions (lead_id, session_id, linked_at) "
                "VALUES (?,?,?)",
                (lead["id"], session_id, stamp),
            )
            created = True
            changed.extend(sorted(contact))
            # Everything staged earlier in the conversation lands on the new lead.
            changed.extend(_promote_session_fields(conn, session_id, lead["id"]))
        else:
            # Enrich contact columns. Only fill blanks — a visitor who gave a
            # phone number last week and a different one today is a judgement
            # call for a human, not an overwrite.
            updates, params = [], []
            for key in CONTACT_KEYS:
                if key in contact and contact[key] and not lead.get(key):
                    updates.append(f"{key} = ?")
                    params.append(contact[key])
                    changed.append(key)
                    if key == "email":
                        updates.append("email_norm = ?")
                        params.append(security.normalise_email(contact[key]))
                    if key == "phone":
                        updates.append("phone_norm = ?")
                        params.append(security.normalise_phone(contact[key]))
            if updates:
                params.extend([db.now(), lead["id"]])
                conn.execute(
                    f"UPDATE leads SET {', '.join(updates)}, updated_at = ? WHERE id = ?",
                    params,
                )

        # Qualification fields, monotonic in confidence.
        for field_key, payload in qualification_fields.items():
            existing = conn.execute(
                "SELECT value, confidence, source FROM lead_field_values "
                " WHERE lead_id = ? AND field_key = ?",
                (lead["id"], field_key),
            ).fetchone()

            if existing:
                # A human correction is final; the extractor does not undo it.
                if existing["source"] == "human":
                    continue
                if payload["confidence"] < existing["confidence"]:
                    continue
                if str(existing["value"]) == str(payload["value"]):
                    continue

            conn.execute(
                """
                INSERT INTO lead_field_values (lead_id, field_key, value, value_raw,
                                               confidence, source_message_id, source,
                                               updated_at)
                     VALUES (?,?,?,?,?,?, 'assistant', ?)
                ON CONFLICT (lead_id, field_key) DO UPDATE SET
                     value = excluded.value,
                     value_raw = excluded.value_raw,
                     confidence = excluded.confidence,
                     source_message_id = excluded.source_message_id,
                     source = 'assistant',
                     updated_at = excluded.updated_at
                """,
                (lead["id"], field_key, str(payload["value"]), payload.get("value_raw"),
                 payload["confidence"], source_message_id, db.now()),
            )
            changed.append(field_key)

    result = rescore(lead["id"])
    result.update({"created": created, "changed_fields": sorted(set(changed)),
                   "lead_ref": lead.get("lead_ref") or result.get("lead_ref")})
    return result


def _rescore_existing(session_id: str, portal_key: str) -> dict[str, Any] | None:
    """Nothing new extracted, but engagement depth grew — so the score may have moved."""
    lead = lead_for_session(session_id)
    if not lead:
        return None
    result = rescore(lead["id"])
    result.update({"created": False, "changed_fields": []})
    return result


# -------------------------------------------------------------------- scoring

def rescore(lead_id: int) -> dict[str, Any]:
    """Recompute tier and score from current state. Idempotent."""
    lead = db.one("SELECT * FROM leads WHERE id = ?", (lead_id,))
    if not lead:
        return {}

    portal = registry.get(lead["portal_key"])
    fields = {
        row["field_key"]: row["value"]
        for row in db.query(
            "SELECT field_key, value FROM lead_field_values WHERE lead_id = ?",
            (lead_id,),
        )
    }
    # Money fields are stored as strings; the scorer compares them numerically.
    for field in portal["fields"]:
        if field["field_type"] in ("money", "int") and fields.get(field["field_key"]):
            try:
                fields[field["field_key"]] = int(fields[field["field_key"]])
            except (TypeError, ValueError):
                pass
        if field["field_type"] == "bool" and fields.get(field["field_key"]) is not None:
            fields[field["field_key"]] = str(fields[field["field_key"]]).lower() in (
                "true", "1", "yes"
            )

    sessions = db.query(
        "SELECT session_id FROM lead_sessions WHERE lead_id = ?", (lead_id,)
    )
    session_ids = [s["session_id"] for s in sessions] or [lead["session_id"]]
    session_ids = [s for s in session_ids if s]

    turns = 0
    messages: list[dict[str, Any]] = []
    if session_ids:
        placeholders = ",".join("?" * len(session_ids))
        turns = int(db.scalar(
            f"SELECT COALESCE(SUM(turn_count),0) FROM chat_sessions "
            f" WHERE id IN ({placeholders})", session_ids
        ) or 0)
        messages = db.query(
            f"SELECT role, content FROM messages "
            f" WHERE session_id IN ({placeholders}) AND role = 'user' ORDER BY id",
            session_ids,
        )

    result = scoring.score(
        rules=portal["scoring_rules"],
        contact={k: lead.get(k) for k in CONTACT_KEYS},
        fields=fields,
        required_keys=[f["field_key"] for f in portal["fields"] if f["required"]],
        turn_count=turns,
        messages=messages,
    )

    previous_tier = lead["qualification"]
    with db.tx() as conn:
        # Spam classification also sets the business status, so a spam lead leaves
        # the sales queue rather than sitting in it as 'new'.
        if result["qualification"] == "spam":
            conn.execute(
                "UPDATE leads SET qualification = ?, score = ?, score_detail = ?, "
                "status = CASE WHEN status = 'new' THEN 'spam' ELSE status END, "
                "updated_at = ? WHERE id = ?",
                (result["qualification"], result["score"],
                 db.dumps(result["detail"]), db.now(), lead_id),
            )
        else:
            conn.execute(
                "UPDATE leads SET qualification = ?, score = ?, score_detail = ?, "
                "updated_at = ? WHERE id = ?",
                (result["qualification"], result["score"],
                 db.dumps(result["detail"]), db.now(), lead_id),
            )

    return {
        "lead_id": lead_id,
        "lead_ref": lead["lead_ref"],
        "qualification": result["qualification"],
        "previous_qualification": previous_tier,
        "tier_changed": previous_tier != result["qualification"],
        "score": result["score"],
        "action": scoring.tier_action(result["qualification"]),
    }


# ---------------------------------------------------------------- reading leads

def payload(lead_id: int | None = None, *, lead_ref: str | None = None,
            include_transcript_url: bool = True) -> dict[str, Any] | None:
    """The lead as the API and the webhook emit it (spec s9.3).

    One builder for both push and pull, so a CRM integration tested against the
    pull API cannot be surprised by a differently-shaped webhook.
    """
    if lead_ref:
        lead = db.one("SELECT * FROM leads WHERE lead_ref = ?", (lead_ref,))
    else:
        lead = db.one("SELECT * FROM leads WHERE id = ?", (lead_id,))
    if not lead:
        return None

    portal = registry.get(lead["portal_key"])
    field_rows = db.query(
        "SELECT field_key, value, value_raw, confidence, source, updated_at "
        "  FROM lead_field_values WHERE lead_id = ?", (lead["id"],)
    )
    types = {f["field_key"]: f["field_type"] for f in portal["fields"]}

    portal_fields: dict[str, Any] = {}
    low_confidence: list[str] = []
    for row in field_rows:
        value: Any = row["value"]
        kind = types.get(row["field_key"])
        if kind in ("money", "int") and value is not None:
            try:
                value = int(value)
            except (TypeError, ValueError):
                pass
        elif kind == "bool" and value is not None:
            value = str(value).lower() in ("true", "1", "yes")
        portal_fields[row["field_key"]] = value
        # Surfaced explicitly so the dashboard can prompt a human to confirm,
        # rather than presenting a guess with the same authority as a statement.
        if row["confidence"] < 0.7 and row["source"] != "human":
            low_confidence.append(row["field_key"])

    sessions = db.query(
        """
        SELECT s.id, s.status, s.channel, s.turn_count, s.started_at,
               s.last_activity_at, s.landing_url, s.referrer, s.utm_source,
               s.utm_medium, s.utm_campaign, s.device, s.region,
               s.consent_given, s.consent_version, s.consent_at
          FROM lead_sessions ls JOIN chat_sessions s ON s.id = ls.session_id
         WHERE ls.lead_id = ? ORDER BY s.started_at
        """,
        (lead["id"],),
    )
    primary = sessions[-1] if sessions else {}

    message_count = 0
    if sessions:
        ids = [s["id"] for s in sessions]
        message_count = int(db.scalar(
            f"SELECT COUNT(*) FROM messages WHERE session_id IN "
            f"({','.join('?' * len(ids))}) AND role IN ('user','assistant')", ids
        ) or 0)

    out: dict[str, Any] = {
        "lead_id": lead["lead_ref"],
        "portal": lead["portal_key"],
        "session_id": primary.get("id") or lead["session_id"],
        "status": primary.get("status") or "unknown",
        "business_status": lead["status"],
        "qualification": lead["qualification"],
        "score": lead["score"],
        "assigned_owner": lead["assigned_owner"],
        "captured_at": lead["created_at"],
        "updated_at": lead["updated_at"],
        "language": lead["language"],
        "contact": {
            "name": lead["name"],
            "email": lead["email"],
            "phone": lead["phone"],
            # Normalised copies travel with the payload so the CRM dedupes on the
            # same values this system did, rather than re-deriving them differently.
            "email_normalised": lead["email_norm"],
            "phone_normalised": lead["phone_norm"],
            # 'conversation' | 'account' | 'mixed'. A number volunteered mid-enquiry
            # is a stronger buying signal than one attached to a login, so the CRM
            # gets to tell them apart rather than treating both as the same thing.
            "source": lead["contact_source"],
            "has_account": lead["user_id"] is not None,
        },
        "portal_fields": portal_fields,
        "fields_needing_confirmation": low_confidence,
        "source": {
            "landing_url": primary.get("landing_url"),
            "referrer": primary.get("referrer"),
            "utm_source": primary.get("utm_source"),
            "utm_medium": primary.get("utm_medium"),
            "utm_campaign": primary.get("utm_campaign"),
            "device": primary.get("device"),
            "region": primary.get("region"),
            "channel": primary.get("channel"),
            # Static for this system. The CRM keys its source adapters off it, so
            # a Meta lead and a chatbot lead are distinguishable downstream.
            "origin": "chatbot",
        },
        "consent": {
            # Per-session consent to the chat notice.
            "given": bool(primary.get("consent_given")),
            "version": primary.get("consent_version"),
            "recorded_at": primary.get("consent_at"),
            # A separate legal basis, and deliberately not conflated with the
            # above: responding to this enquiry needs no marketing consent, but
            # putting the person in a nurture sequence does.
            "marketing_opt_in": bool(lead["marketing_opt_in"]),
        },
        "session_count": len(sessions),
        "message_count": message_count,
        "sessions": [
            {"session_id": s["id"], "started_at": s["started_at"],
             "turns": s["turn_count"], "channel": s["channel"]}
            for s in sessions
        ],
        "action": scoring.tier_action(lead["qualification"]),
        "score_detail": db.loads(lead["score_detail"], {}),
    }
    if include_transcript_url and out["session_id"]:
        out["transcript_url"] = f"/api/v1/chats/{out['session_id']}"
    return out


def update_field(lead_id: int, field_key: str, value: Any, *,
                 actor: str = "agent") -> bool:
    """Human correction from the dashboard. Marked `source='human'`, which makes it
    permanent against further extraction (see apply_extraction)."""
    portal_key = db.scalar("SELECT portal_key FROM leads WHERE id = ?", (lead_id,))
    if not portal_key:
        return False

    if field_key in CONTACT_KEYS:
        extra, params = "", []
        if field_key == "email":
            extra = ", email_norm = ?"
            params.append(security.normalise_email(value))
        elif field_key == "phone":
            extra = ", phone_norm = ?"
            params.append(security.normalise_phone(value))
        with db.tx() as conn:
            conn.execute(
                f"UPDATE leads SET {field_key} = ?{extra}, updated_at = ? WHERE id = ?",
                (value, *params, db.now(), lead_id),
            )
    else:
        field = registry.field(portal_key, field_key)
        if not field:
            return False
        from agent import extraction

        normalised, ok = extraction.normalise(field, value)
        if not ok:
            return False
        with db.tx() as conn:
            conn.execute(
                """
                INSERT INTO lead_field_values (lead_id, field_key, value, value_raw,
                                               confidence, source, updated_at)
                     VALUES (?,?,?,?,1.0,'human',?)
                ON CONFLICT (lead_id, field_key) DO UPDATE SET
                     value = excluded.value, value_raw = excluded.value_raw,
                     confidence = 1.0, source = 'human',
                     updated_at = excluded.updated_at
                """,
                (lead_id, field_key, str(normalised), str(value)[:300], db.now()),
            )

    db.audit(actor, "lead.field_edited", entity="lead", entity_id=lead_id,
             detail={"field": field_key, "value": str(value)[:200]})
    rescore(lead_id)
    return True


def set_status(lead_id: int, *, status: str | None = None,
               assigned_owner: str | None = None,
               actor: str = "agent") -> bool:
    """Business status and ownership (spec s9.1 PATCH endpoint)."""
    valid = ("new", "contacted", "qualified", "converted", "lost", "spam")
    updates, params = [], []
    if status:
        if status not in valid:
            raise ValueError(f"status must be one of {valid}")
        updates.append("status = ?")
        params.append(status)
        if status == "contacted":
            updates.append("last_contacted_at = ?")
            params.append(db.now())
    if assigned_owner is not None:
        updates.append("assigned_owner = ?")
        params.append(assigned_owner or None)
    if not updates:
        return False

    params.extend([db.now(), lead_id])
    with db.tx() as conn:
        cur = conn.execute(
            f"UPDATE leads SET {', '.join(updates)}, updated_at = ? WHERE id = ?",
            params,
        )
    if cur.rowcount:
        db.audit(actor, "lead.status_changed", entity="lead", entity_id=lead_id,
                 detail={"status": status, "owner": assigned_owner})
    return cur.rowcount > 0


def listing(*, portal_key: str | None = None, qualification: str | None = None,
            status: str | None = None, since: str | None = None,
            until: str | None = None, project: str | None = None,
            owner: str | None = None, search: str | None = None,
            cursor: int | None = None, limit: int = 50) -> dict[str, Any]:
    """Filtered, cursor-paginated lead list backing GET /api/v1/leads.

    Cursor pagination rather than offset: leads arrive continuously, so an
    offset-paged consumer walking pages would see duplicates and miss rows.
    """
    clauses, params = ["1 = 1"], []
    if portal_key:
        clauses.append("l.portal_key = ?")
        params.append(portal_key)
    if qualification:
        clauses.append("l.qualification = ?")
        params.append(qualification)
    if status:
        clauses.append("l.status = ?")
        params.append(status)
    if since:
        clauses.append("l.created_at >= ?")
        params.append(since)
    if until:
        clauses.append("l.created_at <= ?")
        params.append(until)
    if owner:
        clauses.append("l.assigned_owner = ?")
        params.append(owner)
    if search:
        clauses.append("(l.name LIKE ? OR l.email LIKE ? OR l.phone LIKE ? "
                       "OR l.lead_ref LIKE ?)")
        params.extend([f"%{search}%"] * 4)
    if project:
        clauses.append(
            "EXISTS (SELECT 1 FROM lead_field_values v WHERE v.lead_id = l.id "
            "        AND v.field_key = 'project' AND v.value = ?)"
        )
        params.append(project)
    if cursor:
        clauses.append("l.id < ?")
        params.append(cursor)

    rows = db.query(
        f"""
        SELECT l.id, l.lead_ref, l.portal_key, l.name, l.email, l.phone,
               l.qualification, l.score, l.status, l.assigned_owner, l.language,
               l.created_at, l.updated_at, l.session_id
          FROM leads l
         WHERE {' AND '.join(clauses)}
         ORDER BY l.id DESC
         LIMIT ?
        """,
        [*params, limit + 1],
    )

    has_more = len(rows) > limit
    page = rows[:limit]
    return {
        "leads": page,
        "next_cursor": page[-1]["id"] if has_more and page else None,
        "has_more": has_more,
    }
