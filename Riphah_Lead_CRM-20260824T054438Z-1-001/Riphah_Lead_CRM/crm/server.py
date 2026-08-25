"""FastAPI backend for the Riphah Lead CRM.

  /                            public landing page
  /app                         the dashboard
  /api/health                  readiness + integration state
  /api/auth/*                  staff login
  /api/leads[/{id}]            list, detail, status, notes, transcript
  /api/analytics/*             dashboard figures, export
  /api/sources                 integration status (chatbot live, Meta pending)
  /api/sync                    force a pull reconciliation
  /api/webhooks/riphah-chatbot inbound lead.created / lead.updated  (HMAC)
  /api/webhooks/meta           inbound Meta leadgen                 (X-Hub-Signature-256)

The webhook receivers read the **raw** body before any parsing. A signature is over
bytes, and re-serialising a parsed JSON body produces different bytes — different
key order, different float formatting, different unicode escaping — so verifying
against a re-serialised body fails intermittently in a way that is very hard to
diagnose. `await request.body()` first, verify, then parse.
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (APIRouter, Cookie, Depends, FastAPI, HTTPException, Query,
                     Request, Response)
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

import config
from core import db, security
from crm import analytics, auth, comms, ingest
from sources import chatbot as chatbot_source
from sources import meta as meta_source

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

_workers: list[Any] = []


@asynccontextmanager
async def lifespan(_: FastAPI):
    _startup()
    try:
        yield
    finally:
        for worker in _workers:
            worker.stop()


app = FastAPI(title="Riphah Lead CRM", version="1.0.0", lifespan=lifespan)


def _startup() -> None:
    db.migrate()

    # Register both sources so the dashboard can report Meta as pending rather
    # than absent — its state is information, not an omission.
    #
    # `probe=False` here on purpose: the assistant and this service are usually
    # started together, so a live probe at startup would block for the timeout and
    # then report a false failure. /api/health and /api/sources probe for real.
    chatbot_state = chatbot_source.SOURCE.status(probe=False)
    ingest.register_source(chatbot_source.KEY, chatbot_source.DISPLAY_NAME,
                           status=chatbot_state["status"],
                           detail=chatbot_state["detail"])
    meta_state = meta_source.SOURCE.status()
    ingest.register_source(meta_source.KEY, meta_source.DISPLAY_NAME,
                           status=meta_state["status"], detail=meta_state["detail"])
    ingest.register_source("manual", "Manually entered", status="live",
                           detail="Created by staff in the dashboard.")

    note = auth.bootstrap_admin()
    if note:
        print(f"[startup] {note}")

    print(f"[startup] chatbot source: {chatbot_state['status']} — "
          f"{chatbot_state['detail']}")
    print(f"[startup] meta source: {meta_state['status']} — {meta_state['detail']}")

    if config.PULL_ENABLED and config.CHATBOT_API_KEY:
        worker = PullWorker()
        worker.start()
        _workers.append(worker)
        print(f"[startup] pull reconciler every {config.PULL_INTERVAL_SECONDS}s")


# ------------------------------------------------------------------- identity

def current_user(riphah_crm: str | None = Cookie(default=None)) -> dict[str, Any] | None:
    return auth.current_user(riphah_crm)


def require_agent(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    try:
        return auth.require(user, "agent")
    except auth.AuthError as exc:
        raise HTTPException(401 if not user else 403, str(exc)) from exc


def require_manager(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    try:
        return auth.require(user, "manager")
    except auth.AuthError as exc:
        raise HTTPException(401 if not user else 403, str(exc)) from exc


def require_admin(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    try:
        return auth.require(user, "admin")
    except auth.AuthError as exc:
        raise HTTPException(401 if not user else 403, str(exc)) from exc


# --------------------------------------------------------------------- health

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "counts": db.counts(),
        "sources": [chatbot_source.SOURCE.status(), meta_source.SOURCE.status()],
        "webhook_secret_configured": bool(config.WEBHOOK_SECRET),
        "pull_enabled": bool(config.PULL_ENABLED and config.CHATBOT_API_KEY),
        "chatbot_base_url": config.CHATBOT_BASE_URL,
        "staff_count": db.scalar("SELECT COUNT(*) FROM staff"),
    }


# ----------------------------------------------------------------------- auth

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


@auth_router.post("/login")
def login(payload: LoginRequest, response: Response) -> dict[str, Any]:
    try:
        result = auth.login(email=payload.email, password=payload.password)
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    response.set_cookie(
        config.AUTH_COOKIE, result["token"],
        max_age=60 * 60 * 24 * config.SESSION_TTL_DAYS,
        httponly=True, samesite="lax",
    )
    return {"user": result["user"]}


@auth_router.post("/logout")
def logout(response: Response,
           riphah_crm: str | None = Cookie(default=None)) -> dict[str, Any]:
    if riphah_crm:
        auth.logout(riphah_crm)
    response.delete_cookie(config.AUTH_COOKIE)
    return {"ok": True}


@auth_router.get("/me")
def me(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    return {"user": user, "owners": auth.owners() if user else []}


class StaffRequest(BaseModel):
    email: str
    password: str
    name: str | None = None
    role: str = "agent"


@auth_router.post("/staff")
def add_staff(payload: StaffRequest,
              admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    try:
        return {"staff": auth.create_staff(
            email=payload.email, password=payload.password, name=payload.name,
            role=payload.role, actor=admin["email"],
        )}
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc


@auth_router.get("/staff")
def list_staff(admin: dict[str, Any] = Depends(require_manager)) -> dict[str, Any]:
    return {"staff": auth.listing()}


class StaffPatch(BaseModel):
    role: str | None = None
    disabled: bool | None = None


@auth_router.patch("/staff/{staff_id}")
def patch_staff(staff_id: int, payload: StaffPatch,
                admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Change a role, or disable/enable an account.

    Two self-protections, both learned the boring way: an admin cannot demote or
    disable themselves. Either one, done by the only admin, locks everyone out of
    the CRM with no way back in short of editing SQLite by hand.
    """
    target = db.one("SELECT * FROM staff WHERE id = ?", (staff_id,))
    if not target:
        raise HTTPException(404, "No such account.")
    if target["id"] == admin["id"] and (payload.disabled
                                        or (payload.role
                                            and payload.role != "admin")):
        raise HTTPException(
            422, "You cannot disable or demote your own admin account — that could "
                 "lock everyone out. Ask another admin, or use: python -m crm.manage")

    updates: list[str] = []
    params: list[Any] = []
    if payload.role is not None:
        if payload.role not in auth.ROLES:
            raise HTTPException(422, f"Role must be one of {auth.ROLES}.")
        updates.append("role = ?")
        params.append(payload.role)
    if payload.disabled is not None:
        updates.append("disabled_at = ?")
        params.append(db.now() if payload.disabled else None)
    if not updates:
        raise HTTPException(422, "Nothing to update.")

    params.append(staff_id)
    with db.tx() as conn:
        conn.execute(f"UPDATE staff SET {', '.join(updates)} WHERE id = ?", params)
        if payload.disabled:
            # Revoking live sessions is the point of disabling. Without this the
            # account keeps working until the cookie expires.
            conn.execute("UPDATE staff_sessions SET revoked_at = ? WHERE staff_id = ?",
                         (db.now(), staff_id))

    db.log_activity(None, admin["email"], "staff_updated",
                    {"target": target["email"], "role": payload.role,
                     "disabled": payload.disabled})
    return {"staff": auth.listing()}


app.include_router(auth_router)


# ---------------------------------------------------------------------- leads

@app.get("/api/leads")
def list_leads(
    user: dict[str, Any] = Depends(require_agent),
    source: str | None = None,
    qualification: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    portal: str | None = None,
    search: str | None = None,
    days: int | None = None,
    unassigned: bool = False,
    needs_confirmation: bool = False,
    include_spam: bool = False,
    sort: str = "captured_at",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    # An agent is scoped to their own leads plus the unassigned pool.
    scope, scope_params = auth.lead_scope(user)
    result = ingest.listing(
        source=source, qualification=qualification, status=status,
        owner=owner, portal=portal, search=search,
        since=db.days_ago(days) if days else None,
        unassigned=unassigned, needs_confirmation=needs_confirmation,
        include_spam=include_spam, sort=sort, limit=limit, offset=offset,
    )
    if scope != "1 = 1":
        allowed = {row["id"] for row in db.query(
            f"SELECT l.id FROM leads l WHERE {scope}", scope_params)}
        result["leads"] = [row for row in result["leads"] if row["id"] in allowed]
        result["scoped_to"] = user["email"]
    return result


@app.get("/api/leads/{lead_id}")
def get_lead(lead_id: int,
             user: dict[str, Any] = Depends(require_agent)) -> dict[str, Any]:
    data = ingest.detail(lead_id)
    if not data:
        raise HTTPException(404, "No such lead.")
    _guard_lead(user, data["lead"])
    return data


def _guard_lead(user: dict[str, Any], lead: dict[str, Any]) -> None:
    if user["role"] != "agent":
        return
    if lead["assigned_owner"] in (None, user["email"]):
        return
    raise HTTPException(403, "That lead is assigned to another consultant.")


class StatusRequest(BaseModel):
    status: str | None = None
    assigned_owner: str | None = None


@app.patch("/api/leads/{lead_id}")
def patch_lead(lead_id: int, payload: StatusRequest,
               user: dict[str, Any] = Depends(require_agent)) -> dict[str, Any]:
    lead = db.one("SELECT * FROM leads WHERE id = ?", (lead_id,))
    if not lead:
        raise HTTPException(404, "No such lead.")
    _guard_lead(user, lead)
    # Reassigning someone else's work is a manager decision.
    if payload.assigned_owner is not None and user["role"] == "agent" \
            and payload.assigned_owner not in (user["email"], None):
        raise HTTPException(403, "Only a manager can assign leads to someone else.")

    result = ingest.set_status(
        lead_id, status=payload.status, assigned_owner=payload.assigned_owner,
        actor=user["email"],
    )
    if not result["ok"]:
        raise HTTPException(422, result["reason"])
    return {**result, "lead": ingest.detail(lead_id)}


class NoteRequest(BaseModel):
    body: str


@app.post("/api/leads/{lead_id}/notes")
def post_note(lead_id: int, payload: NoteRequest,
              user: dict[str, Any] = Depends(require_agent)) -> dict[str, Any]:
    lead = db.one("SELECT * FROM leads WHERE id = ?", (lead_id,))
    if not lead:
        raise HTTPException(404, "No such lead.")
    _guard_lead(user, lead)
    result = ingest.add_note(lead_id, payload.body, author=user["email"])
    if not result["ok"]:
        raise HTTPException(422, result["reason"])
    return {**result, "notes": ingest.detail(lead_id)["notes"]}


@app.post("/api/leads/{lead_id}/transcript")
def fetch_transcript(lead_id: int,
                     user: dict[str, Any] = Depends(require_agent)) -> dict[str, Any]:
    """Pull the conversation transcript from the chatbot and cache it.

    On demand rather than shipped with every lead: most leads are never opened, and
    the transcript is the largest part of the payload.
    """
    lead = db.one("SELECT * FROM leads WHERE id = ?", (lead_id,))
    if not lead:
        raise HTTPException(404, "No such lead.")
    _guard_lead(user, lead)
    if lead["source_key"] != "chatbot":
        raise HTTPException(422, "Only chatbot leads have a conversation transcript.")

    payload = db.loads(lead["raw_payload"], {})
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(422, "This lead carries no session id.")

    data = chatbot_source.SOURCE.fetch_transcript(session_id)
    if not data:
        raise HTTPException(
            502,
            "Could not fetch the transcript. Check CHATBOT_API_KEY and that the "
            "assistant is reachable at " + config.CHATBOT_BASE_URL,
        )
    messages = data.get("messages") or []
    ingest.cache_transcript(lead_id, session_id, messages)
    return {"session_id": session_id, "messages": messages,
            "session": data.get("session")}


# ---------------------------------------------------------------------- comms

@app.get("/api/comms/status")
def comms_status(_: dict[str, Any] = Depends(require_agent)) -> dict[str, Any]:
    """WhatsApp + SIP channel state, in the same shape as /api/sources."""
    return {"channels": comms.status()}


class WhatsAppRequest(BaseModel):
    message: str


def _comms_lead(lead_id: int, user: dict[str, Any]) -> dict[str, Any]:
    lead = db.one("SELECT * FROM leads WHERE id = ?", (lead_id,))
    if not lead:
        raise HTTPException(404, "No such lead.")
    _guard_lead(user, lead)
    return lead


@app.post("/api/leads/{lead_id}/whatsapp")
def send_whatsapp(lead_id: int, payload: WhatsAppRequest,
                  user: dict[str, Any] = Depends(require_agent)) -> dict[str, Any]:
    """Send a WhatsApp message through the Business Cloud API.

    503 while pending, naming the missing variables — the frontend then falls
    back to a wa.me deep link and logs the attempt instead.
    """
    lead = _comms_lead(lead_id, user)
    if not payload.message.strip():
        raise HTTPException(422, "Nothing to send.")
    try:
        return comms.send_whatsapp(lead, payload.message.strip(),
                                   actor=user["email"])
    except comms.CommsPending as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/leads/{lead_id}/call")
def originate_call(lead_id: int,
                   user: dict[str, Any] = Depends(require_agent)) -> dict[str, Any]:
    """Click-to-call through the PBX. 503 while pending, naming what's missing."""
    lead = _comms_lead(lead_id, user)
    try:
        return comms.originate_call(lead, actor=user["email"])
    except comms.CommsPending as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


class ContactAttemptRequest(BaseModel):
    channel: str  # 'whatsapp' | 'call'


@app.post("/api/leads/{lead_id}/contact-attempt")
def log_contact_attempt(lead_id: int, payload: ContactAttemptRequest,
                        user: dict[str, Any] = Depends(require_agent)) -> dict[str, Any]:
    """Record a fallback-mode outreach (wa.me link, softphone sip:/tel: link).

    The transport ran on the consultant's own device, so the server never saw
    it — but the attempt still has to reach the activity trail and stamp
    first_response_at, or the response-time analytics under-report exactly the
    consultants who respond fastest.
    """
    lead = _comms_lead(lead_id, user)
    try:
        if payload.channel == "whatsapp":
            return comms.log_whatsapp_link(lead, actor=user["email"])
        if payload.channel == "call":
            return comms.log_call_link(lead, actor=user["email"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    raise HTTPException(422, "channel must be 'whatsapp' or 'call'.")


@app.get("/api/chats")
def list_chats(user: dict[str, Any] = Depends(require_agent),
               search: str | None = None,
               limit: int = Query(default=100, ge=1, le=300)) -> dict[str, Any]:
    """Chatbot conversations, one per lead — the chat-monitor sidebar.

    Listed from the CRM's own rows (every chatbot lead arrived from a
    conversation), so the list itself needs no network call. The preview comes
    from the cached transcript when one has been fetched; the full transcript
    is pulled from the assistant on demand via POST /api/leads/{id}/transcript.
    Agent scoping applies exactly as it does on the leads list.
    """
    scope, scope_params = auth.lead_scope(user)
    clauses = ["l.source_key = 'chatbot'", "l.qualification != 'spam'",
               f"({scope})"]
    params: list[Any] = [*scope_params]
    if search:
        like = f"%{search.strip()}%"
        clauses.append("(l.name LIKE ? OR l.email LIKE ? OR l.phone LIKE ? "
                       "OR l.external_id LIKE ?)")
        params.extend([like, like, like, like])
    rows = db.query(
        f"""
        SELECT l.id, l.external_id, l.name, l.email, l.phone, l.qualification,
               l.status, l.assigned_owner, l.message_count, l.channel,
               l.language, l.captured_at, l.updated_at,
               t.body AS cached_body, t.fetched_at AS transcript_fetched_at
          FROM leads l
          LEFT JOIN transcripts t ON t.lead_id = l.id
         WHERE {' AND '.join(clauses)}
         ORDER BY COALESCE(l.captured_at, l.created_at) DESC
         LIMIT ?
        """,
        [*params, limit],
    )
    chats = []
    for row in rows:
        preview = None
        for message in reversed(db.loads(row.pop("cached_body", None), []) or []):
            if message.get("role") in ("user", "assistant") and message.get("content"):
                prefix = "AI: " if message["role"] == "assistant" else ""
                preview = prefix + message["content"]
                break
        row["preview"] = (preview or "")[:160] or None
        chats.append(row)
    return {"chats": chats}


class ManualLeadRequest(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    portal: str = "riphah-property"
    qualification: str = "warm"
    fields: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None


@app.post("/api/leads")
def create_manual_lead(payload: ManualLeadRequest,
                       user: dict[str, Any] = Depends(require_agent)) -> dict[str, Any]:
    """Manually add a lead — a walk-in, or a phone enquiry.

    Goes through the same `upsert` as every other source, so it appears in the same
    analytics rather than being invisible to them.
    """
    from sources.base import NormalisedLead, clean_qualification

    if not (payload.email or payload.phone):
        raise HTTPException(422, "A manual lead needs an email or a phone number.")

    external_id = f"manual-{security.new_token(8)}"
    lead = NormalisedLead(
        source_key="manual", external_id=external_id, portal=payload.portal,
        name=payload.name, email=payload.email, phone=payload.phone,
        qualification=clean_qualification(payload.qualification),
        fields=payload.fields, channel="manual", captured_at=db.now(),
        raw_payload={"entered_by": user["email"], **payload.model_dump()},
    )
    result = ingest.upsert(lead, actor=user["email"])
    if not result["ok"]:
        raise HTTPException(422, result["reason"])
    if payload.note:
        ingest.add_note(result["lead_id"], payload.note, author=user["email"])
    return result


# ------------------------------------------------------------------ analytics

analytics_router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@analytics_router.get("/dashboard")
def dashboard(days: int = Query(default=config.DEFAULT_ANALYTICS_DAYS, ge=1, le=730),
              _: dict[str, Any] = Depends(require_agent)) -> dict[str, Any]:
    return analytics.dashboard(days=days)


@analytics_router.get("/field/{field_key}")
def field_distribution(field_key: str, days: int = 30,
                       _: dict[str, Any] = Depends(require_agent)) -> dict[str, Any]:
    """Distribution of any captured field, from any source, with no code change."""
    return {"field": field_key, "values": analytics.by_field(field_key, days=days)}


@analytics_router.get("/sla")
def sla(_: dict[str, Any] = Depends(require_agent)) -> dict[str, Any]:
    return analytics.sla_breaches()


@analytics_router.get("/export.csv")
def export(days: int = Query(default=90, ge=1, le=730),
           include_spam: bool = False,
           manager: dict[str, Any] = Depends(require_manager)) -> PlainTextResponse:
    """CSV export. Manager and above, and every export is logged.

    A spreadsheet of every lead with phone numbers is the most portable asset here,
    so who took it and how much is worth a permanent record.
    """
    body = analytics.export_csv(days=days, include_spam=include_spam)
    db.log_activity(None, manager["email"], "export",
                    {"days": days, "rows": max(0, body.count("\n") - 1),
                     "include_spam": include_spam})
    return PlainTextResponse(
        body, media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="riphah-leads-{days}d.csv"'},
    )


app.include_router(analytics_router)


# -------------------------------------------------------------------- sources

@app.get("/api/sources")
def sources(_: dict[str, Any] = Depends(require_agent)) -> dict[str, Any]:
    """Integration status. Meta reports `pending` with what it is blocked on."""
    return {
        "sources": [chatbot_source.SOURCE.status(), meta_source.SOURCE.status()],
        "recent_webhooks": db.query(
            "SELECT id, source_key, event, signature_valid, reject_reason, lead_id, "
            "       received_at FROM inbound_webhooks "
            " ORDER BY id DESC LIMIT 25"
        ),
    }


@app.post("/api/sync")
def sync(manager: dict[str, Any] = Depends(require_manager)) -> dict[str, Any]:
    """Force a pull reconciliation now."""
    return run_pull(actor=manager["email"])


# ------------------------------------------------------------------- webhooks

@app.post("/api/webhooks/riphah-chatbot")
async def chatbot_webhook(request: Request) -> dict[str, Any]:
    """Inbound `lead.created` / `lead.updated` from the assistant.

    The raw body is read before parsing, because the HMAC is over exactly those
    bytes. Verifying against a re-serialised body would fail intermittently on key
    ordering and unicode escaping — the worst kind of integration bug.
    """
    body = await request.body()
    ok, reason = security.verify_chatbot_signature(
        body,
        request.headers.get("x-riphah-timestamp"),
        request.headers.get("x-riphah-signature"),
    )
    event = request.headers.get("x-riphah-event")

    # Recorded whether or not it verified. "Never arrived" and "arrived and was
    # rejected" need completely different fixes, and only this row distinguishes
    # them.
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO inbound_webhooks (source_key, event, signature_valid, "
            "reject_reason, body, received_at) VALUES (?,?,?,?,?,?)",
            ("chatbot", event, int(ok), reason, body.decode("utf-8", "replace")[:60000],
             db.now()),
        )
        webhook_id = cur.lastrowid

    if not ok:
        print(f"[webhook] rejected chatbot delivery: {reason}")
        raise HTTPException(401, f"Signature verification failed: {reason}")

    import json

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Body is not valid JSON.") from exc

    data = payload.get("data") or payload
    lead = chatbot_source.SOURCE.normalise(data)
    result = ingest.upsert(lead, actor="webhook:chatbot")
    if not result["ok"]:
        raise HTTPException(422, result["reason"])

    with db.tx() as conn:
        conn.execute("UPDATE inbound_webhooks SET lead_id = ? WHERE id = ?",
                     (result["lead_id"], webhook_id))

    return {"received": True, "event": payload.get("event") or event, **result}


@app.get("/api/webhooks/meta")
def meta_verify(request: Request) -> PlainTextResponse:
    """Meta's subscription handshake.

    Meta calls this once when the webhook is registered and expects the challenge
    echoed back verbatim as plain text.
    """
    params = request.query_params
    challenge = meta_source.SOURCE.verify_subscription(
        params.get("hub.mode"), params.get("hub.verify_token"),
        params.get("hub.challenge"),
    )
    if challenge is None:
        raise HTTPException(
            403,
            "Verification failed. META_VERIFY_TOKEN is unset or does not match — "
            "the Meta source is pending credentials.",
        )
    return PlainTextResponse(challenge)


@app.post("/api/webhooks/meta")
async def meta_webhook(request: Request) -> dict[str, Any]:
    """Inbound Meta leadgen notification.

    Fully implemented and signature-checked, but non-functional until credentials
    arrive: Meta's payload carries only a `leadgen_id`, and reading the answers
    needs `META_PAGE_ACCESS_TOKEN`. Returns 503 with what is missing rather than a
    generic error, so whoever wires it up can see exactly what is required.
    """
    body = await request.body()
    ok, reason = security.verify_meta_signature(
        body, request.headers.get("x-hub-signature-256")
    )

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO inbound_webhooks (source_key, event, signature_valid, "
            "reject_reason, body, received_at) VALUES (?,?,?,?,?,?)",
            ("meta", "leadgen", int(ok), reason,
             body.decode("utf-8", "replace")[:60000], db.now()),
        )

    state = meta_source.SOURCE.status()
    if state["status"] != "live":
        raise HTTPException(
            503,
            "The Meta source is pending. Missing configuration: "
            + ", ".join(state["missing_config"])
            + ". The adapter and field mapping are built and tested; only "
              "credentials from Riphah marketing are outstanding.",
        )
    if not ok:
        raise HTTPException(401, f"Signature verification failed: {reason}")

    import json

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Body is not valid JSON.") from exc

    results = []
    for leadgen_id in meta_source.SOURCE.extract_leadgen_ids(payload):
        raw = meta_source.SOURCE.fetch_lead(leadgen_id)
        if not raw:
            results.append({"leadgen_id": leadgen_id, "ok": False,
                            "reason": "graph fetch failed"})
            continue
        results.append(ingest.upsert(meta_source.SOURCE.normalise(raw),
                                     actor="webhook:meta"))
    return {"received": True, "leads": results}


# ------------------------------------------------------------ pull reconciler

def run_pull(*, actor: str = "system") -> dict[str, Any]:
    """Reconcile against the chatbot's lead API.

    Asks for leads since the last sync **minus an overlap window**, not since the
    last sync. Two services with slightly different clocks, and a lead created
    during the handover, would otherwise fall between two windows and never arrive.
    Re-fetching a few already-seen leads is free because upsert is idempotent.
    """
    row = db.one("SELECT last_sync_at FROM sources WHERE key = 'chatbot'")
    since = None
    if row and row["last_sync_at"]:
        since = db.minutes_ago(config.PULL_OVERLAP_MINUTES)
        # First-ever sync pulls everything; later syncs use the overlap window.
        since = min(since, row["last_sync_at"]) if row["last_sync_at"] else since

    result = chatbot_source.SOURCE.fetch_since(since)
    if not result["ok"]:
        db.log_activity(None, actor, "sync_failed", result.get("reason"))
        return {"ok": False, "reason": result.get("reason"), "ingested": 0}

    created = updated = 0
    for raw in result["leads"]:
        outcome = ingest.upsert(chatbot_source.SOURCE.normalise(raw),
                                actor=f"pull:{actor}")
        if outcome.get("ok"):
            created += outcome["created"]
            updated += not outcome["created"]

    stamp = db.now()
    with db.tx() as conn:
        conn.execute("UPDATE sources SET last_sync_at = ?, updated_at = ? "
                     " WHERE key = 'chatbot'", (stamp, stamp))

    summary = {"ok": True, "fetched": len(result["leads"]), "created": created,
               "updated": updated, "since": since, "synced_at": stamp,
               "truncated": result.get("truncated", False)}
    db.log_activity(None, actor, "sync", summary)
    return summary


class PullWorker(threading.Thread):
    """Background reconciliation loop.

    Daemon thread rather than a separate cron entry, so the CRM is one process to
    deploy. If this ever runs on several workers, the sync needs a lock — noted
    because concurrent pulls would each advance `last_sync_at` and could skip a
    window between them.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True, name="crm-pull")
        self._stop = threading.Event()

    def run(self) -> None:
        # A short first delay lets the chatbot finish starting when both come up
        # together, so the first sync doesn't fail on a connection refused.
        if self._stop.wait(10):
            return
        while True:
            try:
                result = run_pull()
                if result.get("fetched"):
                    print(f"[pull] {result}")
            except Exception as exc:  # noqa: BLE001
                print(f"[pull] error: {type(exc).__name__}: {exc}")
            if self._stop.wait(config.PULL_INTERVAL_SECONDS):
                return

    def stop(self) -> None:
        self._stop.set()


# ------------------------------------------------------------------- frontend

@app.get("/")
def landing() -> FileResponse:
    """The marketing surface. Public, no auth, no data — safe to leave open."""
    return FileResponse(FRONTEND_DIR / "landing.html")


@app.get("/app")
def index() -> FileResponse:
    """The CRM itself. Still gated by the sign-in view inside the page and by
    every /api route it calls, so serving the shell unauthenticated leaks
    nothing."""
    return FileResponse(FRONTEND_DIR / "index.html")


def main() -> None:
    import uvicorn

    uvicorn.run("crm.server:app", host=config.HOST, port=config.PORT,
                reload=bool(__import__("os").getenv("RELOAD")))


if __name__ == "__main__":
    main()
