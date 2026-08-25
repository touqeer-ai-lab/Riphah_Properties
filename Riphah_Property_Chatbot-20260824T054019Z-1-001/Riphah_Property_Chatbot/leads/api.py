"""The `/api/v1` integration surface (scope document s9), as a FastAPI router.

This is the contract the CRM depends on, so two properties are treated as
non-negotiable:

**Versioned path.** `/api/v1/...`. A breaking change becomes `/api/v2` and the
CRM keeps working until someone chooses to migrate it. The alternative — changing
the shape under a live integration — is how a lead pipeline silently stops.

**One payload builder.** Push (webhook) and pull (this API) both call
`store.payload()`. A CRM integration tested against the pull API therefore cannot
be surprised by a differently-shaped webhook, which is a class of bug that is
miserable to diagnose from the receiving end.

Authentication is a per-consumer API key, hashed at rest, revocable
independently. Keys are scoped, so the CRM's read key cannot be used to mutate
the portal field schema.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

import config
from agent import conversations
from core import db, security
from leads import delivery, store
from portals import registry

router = APIRouter(prefix="/api/v1", tags=["integration"])


# ------------------------------------------------------------------------- auth

def _resolve_key(raw: str | None) -> dict[str, Any]:
    if not raw:
        raise HTTPException(401, "Missing API key. Send it as 'X-API-Key'.")
    row = db.one(
        "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
        (security.token_hash(raw.strip()),),
    )
    if not row:
        raise HTTPException(401, "Invalid or revoked API key.")
    with db.tx() as conn:
        conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                     (db.now(), row["id"]))
    row["scopes"] = db.loads(row["scopes"], [])
    return row


def api_key(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    return _resolve_key(x_api_key)


def require_scope(*needed: str):
    """Dependency factory enforcing a scope on a route."""
    def guard(key: dict[str, Any] = Depends(api_key)) -> dict[str, Any]:
        scopes = key.get("scopes") or []
        if "*" in scopes:
            return key
        if not any(scope in scopes for scope in needed):
            raise HTTPException(
                403, f"This key lacks the required scope: one of {list(needed)}."
            )
        return key
    return guard


def _portal_guard(key: dict[str, Any], portal: str | None) -> str | None:
    """A key bound to one portal cannot read another's leads.

    Returns the portal filter to apply: the key's own portal when it is bound,
    otherwise whatever the caller asked for.
    """
    if key.get("portal_key"):
        if portal and portal != key["portal_key"]:
            raise HTTPException(403, "This key is scoped to a different portal.")
        return key["portal_key"]
    return portal


def create_key(*, label: str, portal_key: str | None = None,
               scopes: list[str] | None = None,
               actor: str = "admin") -> dict[str, Any]:
    """Mint a key. The raw value is returned once and never stored."""
    raw, hashed, prefix = security.new_api_key()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, label, portal_key, scopes, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (hashed, prefix, label, portal_key,
             db.dumps(scopes or ["leads:read"]), db.now()),
        )
    db.audit(actor, "api_key.created", entity="api_key", entity_id=prefix,
             detail={"label": label, "portal": portal_key, "scopes": scopes})
    return {"api_key": raw, "prefix": prefix, "label": label,
             "scopes": scopes or ["leads:read"],
             "warning": "Store this now. It cannot be retrieved again."}


# ------------------------------------------------------------------------ leads

@router.get("/leads")
def list_leads(
    key: dict[str, Any] = Depends(require_scope("leads:read")),
    portal: str | None = None,
    since: str | None = Query(default=None, description="ISO-8601 lower bound on created_at"),
    until: str | None = None,
    qualification: str | None = Query(default=None, description="hot | warm | cold | spam"),
    status: str | None = Query(default=None, description="new | contacted | qualified | converted | lost | spam"),
    project: str | None = None,
    owner: str | None = None,
    search: str | None = None,
    cursor: int | None = Query(default=None, description="Opaque cursor from next_cursor"),
    limit: int = Query(default=config.API_PAGE_SIZE, ge=1, le=config.API_PAGE_SIZE_MAX),
    full: bool = Query(default=False, description="Return complete payloads rather than summaries"),
) -> dict[str, Any]:
    """List leads. Cursor-paginated, newest first.

    Cursor rather than offset because leads arrive continuously: an offset-paged
    consumer walking pages would see duplicates and skip rows as the underlying
    set shifts under it.
    """
    result = store.listing(
        portal_key=_portal_guard(key, portal), qualification=qualification,
        status=status, since=since, until=until, project=project, owner=owner,
        search=search, cursor=cursor, limit=limit,
    )
    if full:
        result["leads"] = [
            store.payload(row["id"]) for row in result["leads"]
        ]
    return result


@router.get("/leads/{lead_ref}")
def get_lead(lead_ref: str,
             key: dict[str, Any] = Depends(require_scope("leads:read"))) -> dict[str, Any]:
    """One lead with the full field set and its session history."""
    body = store.payload(lead_ref=lead_ref)
    if not body:
        raise HTTPException(404, "No such lead.")
    _portal_guard(key, body["portal"])
    return body


class LeadPatch(BaseModel):
    status: str | None = Field(
        default=None,
        description="new | contacted | qualified | converted | lost | spam",
    )
    assigned_owner: str | None = None


@router.patch("/leads/{lead_ref}")
def patch_lead(lead_ref: str, payload: LeadPatch,
               key: dict[str, Any] = Depends(require_scope("leads:write"))) -> dict[str, Any]:
    """Update business status or ownership from the CRM side.

    Deliberately narrow: the CRM owns *its* fields — where the lead is in the sales
    process and whose it is. It does not get to rewrite the captured record, which
    is evidence of what the visitor actually said.
    """
    lead = db.one("SELECT id, portal_key FROM leads WHERE lead_ref = ?", (lead_ref,))
    if not lead:
        raise HTTPException(404, "No such lead.")
    _portal_guard(key, lead["portal_key"])

    try:
        changed = store.set_status(
            lead["id"], status=payload.status,
            assigned_owner=payload.assigned_owner,
            actor=f"api:{key['key_prefix']}",
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not changed:
        raise HTTPException(422, "Nothing to update.")
    return store.payload(lead["id"]) or {}


# -------------------------------------------------------------------- transcript

@router.get("/chats/{session_id}")
def get_chat(session_id: str,
             key: dict[str, Any] = Depends(require_scope("leads:read"))) -> dict[str, Any]:
    """Full transcript for a session **that produced a lead**.

    Tool turns travel with it deliberately: a consultant reading a transcript to
    prepare for a call benefits from seeing which document produced an answer, and
    a disputed statement is only investigable if the retrieval is on the record.

    The lead-linkage requirement is the important part, and it was missing in the
    first version of this endpoint. Without it, a `leads:read` key could read the
    transcript of *any* session — including a visitor who browsed, gave no contact
    details, never entered the sales pipeline, and never consented to being passed
    to sales. Session ids are uuid4 and so not guessable, but they are handed to the
    browser, kept in localStorage, and appear in logs; "hard to guess" is not a
    permission model.

    So the rule matches the boundary the rest of the system draws: the CRM sees what
    became a lead, and nothing else. A conversation that produced no lead stays in
    this service.
    """
    linked = db.scalar(
        "SELECT COUNT(*) FROM lead_sessions WHERE session_id = ?", (session_id,)
    )
    if not linked:
        # 404 rather than 403, and the same message as a genuinely missing session.
        # Distinguishing "exists but you may not see it" from "does not exist" would
        # confirm to a key holder that a given visitor had a conversation, which is
        # itself the information being withheld.
        exists = db.scalar(
            "SELECT COUNT(*) FROM chat_sessions WHERE id = ?", (session_id,))
        if exists:
            db.audit(f"api:{key['key_prefix']}", "chat.denied_no_lead",
                     entity="chat_session", entity_id=session_id)
        raise HTTPException(404, "No transcript available for that session.")

    data = conversations.transcript(session_id)
    if not data.get("found"):
        raise HTTPException(404, "No transcript available for that session.")
    _portal_guard(key, data["session"]["portal_key"])
    # Hashed IP is for rate limiting, not for consumers. Same for the anonymous
    # browser id, which would let a consumer correlate sessions across leads.
    data["session"].pop("ip_hash", None)
    data["session"].pop("visitor_id", None)
    return data


# ----------------------------------------------------------------- portal schema

@router.get("/portals")
def list_portals(key: dict[str, Any] = Depends(require_scope("leads:read",
                                                            "portals:read"))) -> dict[str, Any]:
    return {"portals": registry.listing()}


@router.get("/portals/{portal}/fields")
def get_fields(portal: str,
               key: dict[str, Any] = Depends(require_scope("leads:read",
                                                           "portals:read"))) -> dict[str, Any]:
    """Current field schema — lets the CRM map fields automatically.

    A CRM that reads this instead of hard-coding field names picks up a new portal
    field without a change on its side, which is the whole point of the field
    schema being data.
    """
    _portal_guard(key, portal)
    try:
        config_row = registry.get(portal)
    except registry.UnknownPortal as exc:
        raise HTTPException(404, "No such portal.") from exc
    return {
        "portal": portal,
        "display_name": config_row["display_name"],
        "languages": config_row["languages"],
        "pricing_mode": config_row["pricing_mode"],
        "contact_fields": list(registry.CONTACT_FIELDS),
        "fields": config_row["fields"],
    }


class FieldSpec(BaseModel):
    field_key: str
    label: str
    field_type: str = "text"
    options: list[str] | None = None
    required: bool = False
    sort_order: int = 100
    prompt_hint: str | None = None
    extract_hint: str | None = None


@router.post("/portals/{portal}/fields")
def post_field(portal: str, spec: FieldSpec,
               key: dict[str, Any] = Depends(require_scope("portals:write"))) -> dict[str, Any]:
    """Add or amend a capture field without a code release (spec s9.1).

    The assistant starts asking about a new field on the very next turn, because
    the prompt and the extraction schema are both built from these rows at request
    time.
    """
    _portal_guard(key, portal)
    try:
        registry.get(portal)
    except registry.UnknownPortal as exc:
        raise HTTPException(404, "No such portal.") from exc
    try:
        field = registry.upsert_field(
            portal, spec.field_key, label=spec.label, field_type=spec.field_type,
            options=spec.options, required=spec.required,
            sort_order=spec.sort_order, prompt_hint=spec.prompt_hint,
            extract_hint=spec.extract_hint, actor=f"api:{key['key_prefix']}",
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"portal": portal, "field": field}


@router.delete("/portals/{portal}/fields/{field_key}")
def delete_field(portal: str, field_key: str,
                 key: dict[str, Any] = Depends(require_scope("portals:write"))) -> dict[str, Any]:
    _portal_guard(key, portal)
    if not registry.delete_field(portal, field_key, actor=f"api:{key['key_prefix']}"):
        raise HTTPException(404, "No such field.")
    return {"deleted": field_key, "portal": portal}


# ------------------------------------------------------------------- deliveries

@router.get("/deliveries")
def list_deliveries(
    key: dict[str, Any] = Depends(require_scope("leads:read")),
    status: str | None = Query(default=None, description="pending | delivered | failed"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """The webhook delivery log — what was sent, what failed, and what will retry."""
    return {"deliveries": delivery.log(limit=limit, status=status)}


@router.post("/deliveries/{delivery_id}/retry")
def retry_delivery(delivery_id: int,
                   key: dict[str, Any] = Depends(require_scope("leads:write"))) -> dict[str, Any]:
    if not delivery.retry(delivery_id):
        raise HTTPException(404, "No such failed delivery.")
    result = delivery.flush(limit=5)
    return {"requeued": delivery_id, "flush": result}
