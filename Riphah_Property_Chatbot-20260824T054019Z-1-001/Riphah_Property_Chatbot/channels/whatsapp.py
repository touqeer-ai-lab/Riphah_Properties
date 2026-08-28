"""WhatsApp as an inbound channel, via Meta's WhatsApp Business Cloud API.

The design rule: **WhatsApp is a transport, not a second bot.** An inbound
message becomes a turn in an ordinary `chat_sessions` row and runs through
`agent.chat.answer()` — the same retrieval, guardrails, extraction, scoring and
CRM delivery as the website widget. Everything the sales team sees for a web
lead (transcript, captured fields, tier, consent) exists for a WhatsApp lead
with no extra code, and `channel = 'whatsapp'` tells them where it came from.

Three things are channel-specific and live here:

1. **Identity.** The customer's phone number is the WhatsApp id itself, so it is
   staged onto the session before the first turn. The visitor already has a
   contact route; the assistant only has to qualify, and the lead is created
   the moment a qualification field appears. Their WhatsApp display name is
   staged at low confidence — profiles say "Dr. A" and "🏠 Home" — so the
   extractor overrides it with a name the visitor actually states.

2. **Continuity.** One `whatsapp_contacts` row per phone maps to the current
   session. A visitor writing again tomorrow lands in the same conversation;
   once a session has been sealed (lead finalised and sent), the next message
   starts a fresh session under the same `visitor_id = "whatsapp:<wa_id>"`, and
   lead deduplication by phone keeps it on the same lead rather than a second one.

3. **Idempotency.** Meta redelivers any webhook that did not get a 200 within
   its timeout, and the model call takes longer than that. So the HTTP handler
   records the delivery, returns 200 immediately, and processes in the
   background; `whatsapp_messages` is keyed by Meta's message id so a redelivery
   is a no-op rather than a second answer.

Signature verification is not optional. Every POST must carry a valid
`X-Hub-Signature-256` over the raw body, or it is discarded and logged. With no
app secret configured the webhook accepts nothing — an unverified inbound
message is an unauthenticated write into the lead pipeline.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx

import config
from agent import chat, conversations
from core import db
from leads import delivery, store

GRAPH = "https://graph.facebook.com"

# What we send back for content the text pipeline cannot read. Kept short and
# in both languages because the visitor's language is not yet known on turn 1.
_UNSUPPORTED_REPLY = (
    "Shukriya! Main abhi text aur voice messages parh sakti hoon — image ya file "
    "nahi. Aap apna sawal likh kar ya voice note mein bhej dein.\n\n"
    "Thanks! I can read text and voice notes but not images or files yet — "
    "please type your question or send a voice note."
)


# ------------------------------------------------------------------ status

def missing_config() -> list[str]:
    return [name for name, value in (
        ("WHATSAPP_VERIFY_TOKEN", config.WHATSAPP_VERIFY_TOKEN),
        ("WHATSAPP_APP_SECRET", config.WHATSAPP_APP_SECRET),
        ("WHATSAPP_ACCESS_TOKEN", config.WHATSAPP_ACCESS_TOKEN),
        ("WHATSAPP_PHONE_NUMBER_ID", config.WHATSAPP_PHONE_NUMBER_ID),
    ) if not value]


def status() -> dict[str, Any]:
    missing = missing_config()
    contacts = int(db.scalar("SELECT COUNT(*) FROM whatsapp_contacts") or 0)
    inbound = int(db.scalar(
        "SELECT COUNT(*) FROM whatsapp_messages WHERE direction = 'in'") or 0)
    return {
        "channel": "whatsapp",
        "status": "pending" if missing else "live",
        "missing_config": missing,
        "webhook_path": "/api/webhooks/whatsapp",
        "portal": config.WHATSAPP_PORTAL,
        "api_version": config.WHATSAPP_API_VERSION,
        "phone_number_id": config.WHATSAPP_PHONE_NUMBER_ID or None,
        "contacts": contacts,
        "inbound_messages": inbound,
        "detail": (
            "Webhook code, session mapping, dedupe and replies are built. "
            "Blocked on the Meta app values: " + ", ".join(missing)
            if missing else
            "Receiving and answering WhatsApp messages."
        ),
    }


# ---------------------------------------------------------------- security

def verify_signature(raw_body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256 is 'sha256=' + HMAC-SHA256(app_secret, raw body).

    Over the *raw bytes*, never a re-serialised body: Meta's JSON key order and
    unicode escaping differ from Python's, and the hash would never match.
    """
    if not config.WHATSAPP_APP_SECRET or not header:
        return False
    scheme, _, provided = header.partition("=")
    if scheme != "sha256" or not provided:
        return False
    expected = hmac.new(config.WHATSAPP_APP_SECRET.encode(), raw_body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.strip())


def verification_challenge(mode: str | None, token: str | None,
                           challenge: str | None) -> str | None:
    """The GET handshake Meta performs when the webhook URL is saved."""
    if mode == "subscribe" and token and challenge \
            and config.WHATSAPP_VERIFY_TOKEN \
            and hmac.compare_digest(token, config.WHATSAPP_VERIFY_TOKEN):
        return challenge
    return None


# ----------------------------------------------------------------- parsing

def parse(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Flatten Meta's envelope into (messages, statuses).

    entry[] → changes[] → value{ contacts[], messages[], statuses[] }. One
    webhook can carry several of each, and a status update arrives in the same
    shape as a message, so both are pulled out here and nothing else is.
    """
    messages: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if value.get("messaging_product") not in (None, "whatsapp"):
                continue
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                     for c in value.get("contacts") or []}
            for m in value.get("messages") or []:
                kind = m.get("type") or "unknown"
                body = None
                media_id = None
                caption = None
                if kind == "text":
                    body = (m.get("text") or {}).get("body")
                elif kind in ("audio", "voice"):
                    media_id = (m.get(kind) or {}).get("id")
                elif kind in ("image", "document", "video", "sticker"):
                    media = m.get(kind) or {}
                    media_id = media.get("id")
                    caption = media.get("caption")
                elif kind == "button":
                    body = (m.get("button") or {}).get("text")
                elif kind == "interactive":
                    inter = m.get("interactive") or {}
                    body = ((inter.get("button_reply") or inter.get("list_reply")
                             or {}).get("title"))
                elif kind == "location":
                    loc = m.get("location") or {}
                    body = f"[location] {loc.get('name') or ''} " \
                           f"{loc.get('latitude')},{loc.get('longitude')}".strip()
                messages.append({
                    "wa_id": m.get("from"),
                    "wa_message_id": m.get("id"),
                    "timestamp": m.get("timestamp"),
                    "type": kind,
                    "text": body,
                    "media_id": media_id,
                    "caption": caption,
                    "mime_type": (m.get(kind) or {}).get("mime_type")
                    if isinstance(m.get(kind), dict) else None,
                    "profile_name": names.get(m.get("from")),
                    "phone_number_id": (value.get("metadata") or {}).get("phone_number_id"),
                })
            for s in value.get("statuses") or []:
                statuses.append({
                    "wa_message_id": s.get("id"),
                    "wa_id": s.get("recipient_id"),
                    "status": s.get("status"),     # sent | delivered | read | failed
                    "timestamp": s.get("timestamp"),
                    "errors": s.get("errors"),
                })
    return messages, statuses


# --------------------------------------------------------------- graph api

def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.WHATSAPP_ACCESS_TOKEN}"}


def _messages_url() -> str:
    return (f"{GRAPH}/{config.WHATSAPP_API_VERSION}/"
            f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages")


def send_text(to: str, body: str, *, session_id: str | None = None,
              reply_to: str | None = None) -> dict[str, Any]:
    """Send a free-form text. Only valid inside the 24-hour customer window,
    which an inbound message always opens — so replying is always allowed."""
    if missing_config():
        raise RuntimeError("WhatsApp is not configured: " + ", ".join(missing_config()))
    # WhatsApp caps a text body at 4096 characters; the assistant rarely gets
    # near it but a table-heavy answer can.
    chunks = [body[i:i + 3900] for i in range(0, len(body), 3900)] or [""]
    last: dict[str, Any] = {}
    for chunk in chunks:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp", "recipient_type": "individual",
            "to": to, "type": "text",
            "text": {"preview_url": False, "body": chunk},
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
            reply_to = None
        with httpx.Client(timeout=20) as client:
            response = client.post(_messages_url(), json=payload, headers=_headers())
        if response.status_code >= 300:
            _record_out(None, to, session_id, status="failed",
                        error=response.text[:400])
            raise RuntimeError(f"WhatsApp send failed ({response.status_code}): "
                               f"{response.text[:300]}")
        last = response.json()
        wamid = ((last.get("messages") or [{}])[0]).get("id")
        _record_out(wamid, to, session_id, status="sent")
    return last


def mark_read(wa_message_id: str) -> None:
    """Blue ticks. Best-effort — a failure here must not cost the reply."""
    if missing_config():
        return
    try:
        with httpx.Client(timeout=10) as client:
            client.post(_messages_url(), headers=_headers(), json={
                "messaging_product": "whatsapp", "status": "read",
                "message_id": wa_message_id,
            })
    except Exception as exc:  # noqa: BLE001
        print(f"[whatsapp] mark_read failed: {exc}")


def download_media(media_id: str) -> tuple[bytes, str | None]:
    """Two hops: the media id resolves to a short-lived URL, then the bytes."""
    with httpx.Client(timeout=30) as client:
        meta = client.get(f"{GRAPH}/{config.WHATSAPP_API_VERSION}/{media_id}",
                          headers=_headers())
        meta.raise_for_status()
        info = meta.json()
        blob = client.get(info["url"], headers=_headers())
        blob.raise_for_status()
        return blob.content, info.get("mime_type")


# ------------------------------------------------------------- bookkeeping

def _record_out(wamid: str | None, wa_id: str, session_id: str | None, *,
                status: str, error: str | None = None) -> None:
    stamp = db.now()
    with db.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO whatsapp_messages (wa_message_id, wa_id, direction, "
            "session_id, message_type, status, error, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (wamid or f"local-{stamp}-{wa_id}", wa_id, "out", session_id, "text",
             status, error, stamp, stamp),
        )


def _claim_inbound(message: dict[str, Any]) -> bool:
    """Insert the inbound id; False if Meta already delivered this one."""
    stamp = db.now()
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO whatsapp_messages (wa_message_id, wa_id, direction, "
            "message_type, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (message["wa_message_id"], message["wa_id"], "in", message["type"],
             "received", stamp, stamp),
        )
    return cur.rowcount > 0


def apply_status(update: dict[str, Any]) -> None:
    """Delivery receipts for messages we sent: sent → delivered → read, or failed."""
    if not update.get("wa_message_id"):
        return
    error = None
    if update.get("errors"):
        first = update["errors"][0] or {}
        error = f"{first.get('code')}: {first.get('title') or first.get('message')}"
    with db.tx() as conn:
        conn.execute(
            "UPDATE whatsapp_messages SET status = ?, error = COALESCE(?, error), "
            "updated_at = ? WHERE wa_message_id = ?",
            (update.get("status"), error, db.now(), update["wa_message_id"]),
        )


def _session_for(wa_id: str, profile_name: str | None) -> str:
    """The contact's current session, creating one when there is none or the
    previous one has been sealed."""
    portal_key = config.WHATSAPP_PORTAL
    stamp = db.now()
    contact = db.one("SELECT * FROM whatsapp_contacts WHERE wa_id = ?", (wa_id,))
    session_id = contact["session_id"] if contact else None
    if session_id:
        session = conversations.get(session_id)
        if not session or session["sealed"]:
            session_id = None

    if not session_id:
        session = conversations.create(
            portal_key=portal_key, visitor_id=f"whatsapp:{wa_id}",
            channel="whatsapp", referrer="whatsapp",
            utm={"utm_source": "whatsapp", "utm_medium": "messaging"},
            device="mobile",
        )
        session_id = session["id"]
        # The number is the identity. Staged at full confidence so the lead has
        # its contact route from the first turn; the display name is a hint
        # only — a stated name overrides it.
        staged: dict[str, dict[str, Any]] = {
            "phone": {"value": f"+{wa_id}", "value_raw": wa_id, "confidence": 1.0},
        }
        if profile_name and profile_name.strip():
            staged["name"] = {"value": profile_name.strip()[:80],
                              "value_raw": profile_name, "confidence": 0.5}
        store.stage_session_fields(session_id, staged)

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO whatsapp_contacts (wa_id, profile_name, session_id, portal_key, "
            "first_seen_at, last_message_at, message_count) VALUES (?,?,?,?,?,?,1) "
            "ON CONFLICT (wa_id) DO UPDATE SET session_id = excluded.session_id, "
            "profile_name = COALESCE(excluded.profile_name, whatsapp_contacts.profile_name), "
            "last_message_at = excluded.last_message_at, "
            "message_count = whatsapp_contacts.message_count + 1",
            (wa_id, profile_name, session_id, portal_key, stamp, stamp),
        )
    return session_id


# -------------------------------------------------------------- processing

def _text_for(message: dict[str, Any]) -> tuple[str | None, str | None]:
    """The text the assistant should answer. Voice notes are transcribed; media
    the pipeline cannot read returns None so the caller sends the fallback."""
    if message["type"] in ("text", "button", "interactive", "location"):
        return message.get("text"), None
    if message["type"] in ("audio", "voice") and message.get("media_id"):
        from agent import voice

        audio, mime = download_media(message["media_id"])
        heard = voice.transcribe(audio, content_type=mime or "audio/ogg")
        return heard.get("text") or None, heard.get("language")
    if message.get("caption"):
        # An image with a caption: answer the caption, note the attachment.
        return f"{message['caption']} (sent with a {message['type']} I cannot view)", None
    return None, None


def handle_inbound(message: dict[str, Any]) -> dict[str, Any]:
    """One inbound message, end to end. Safe to call from a background task."""
    wa_id = message.get("wa_id")
    wamid = message.get("wa_message_id")
    if not wa_id or not wamid:
        return {"skipped": "malformed"}
    if not _claim_inbound(message):
        return {"skipped": "duplicate", "wa_message_id": wamid}

    session_id = _session_for(wa_id, message.get("profile_name"))
    mark_read(wamid)

    with db.tx() as conn:
        conn.execute("UPDATE whatsapp_messages SET session_id = ?, updated_at = ? "
                     "WHERE wa_message_id = ?", (session_id, db.now(), wamid))

    try:
        text, language = _text_for(message)
    except Exception as exc:  # noqa: BLE001
        print(f"[whatsapp] media handling failed: {type(exc).__name__}: {exc}")
        text, language = None, None

    if not text:
        conversations.add_message(session_id, "user",
                                  f"[{message['type']} message]", channel="whatsapp")
        send_text(wa_id, _UNSUPPORTED_REPLY, session_id=session_id, reply_to=wamid)
        return {"session_id": session_id, "answered": "unsupported_media"}

    try:
        result = chat.answer(text, session_id=session_id,
                             portal_key=config.WHATSAPP_PORTAL,
                             language=language, channel="whatsapp")
        answer = result["answer"]
    except Exception as exc:  # noqa: BLE001
        print(f"[whatsapp] answer failed: {type(exc).__name__}: {exc}")
        db.audit("system", "whatsapp.answer_failed", entity="chat_session",
                 entity_id=session_id, detail=str(exc)[:400])
        answer = ("Maazrat, abhi jawab dene mein masla aa raha hai. Thori der mein "
                  "dobara try karein, ya likh dein 'consultant' aur hamari team "
                  "aap se rabta karegi.")
        result = {}

    send_text(wa_id, answer, session_id=session_id, reply_to=wamid)
    # Give the lead a nudge towards the CRM now rather than waiting for the
    # 30-minute lifecycle sweep — WhatsApp visitors expect the callback sooner.
    try:
        delivery.flush()
    except Exception:  # noqa: BLE001
        pass
    return {
        "session_id": session_id, "answered": True,
        "lead": (result.get("lead") or {}).get("lead_ref"),
        "captured": result.get("captured"),
    }


def handle_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """Process one verified webhook body. Called from the background task."""
    messages, statuses = parse(payload)
    for update in statuses:
        apply_status(update)
    results = []
    for message in messages:
        try:
            results.append(handle_inbound(message))
        except Exception as exc:  # noqa: BLE001
            print(f"[whatsapp] inbound failed: {type(exc).__name__}: {exc}")
            db.audit("system", "whatsapp.inbound_failed", detail={
                "wa_message_id": message.get("wa_message_id"), "error": str(exc)[:400]})
            results.append({"error": str(exc)[:200]})
    return {"messages": len(messages), "statuses": len(statuses), "results": results}


def recent_conversations(limit: int = 50) -> list[dict[str, Any]]:
    """For the admin monitor: who has written, when, and which session/lead."""
    return db.query(
        """
        SELECT c.wa_id, c.profile_name, c.session_id, c.last_message_at,
               c.message_count, s.title, s.status AS session_status,
               (SELECT l.lead_ref FROM lead_sessions ls JOIN leads l ON l.id = ls.lead_id
                 WHERE ls.session_id = c.session_id LIMIT 1) AS lead_ref
          FROM whatsapp_contacts c
          LEFT JOIN chat_sessions s ON s.id = c.session_id
         ORDER BY c.last_message_at DESC LIMIT ?
        """,
        (limit,),
    )


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)
