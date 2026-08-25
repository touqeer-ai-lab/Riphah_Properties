"""FastAPI backend for the Riphah property assistant.

Route groups:

  /                         the chat UI
  /api/health               readiness
  /api/bootstrap            widget config + session mint (one call on page load)
  /api/auth/*               signup, login, logout, me
  /api/chat                 the turn loop
  /api/sessions/*           conversation history
  /api/voice/*              realtime credential, transcribe, speak
  /api/tools/{name}         tool bridge for the browser's Realtime data channel
  /api/admin/*              KB documents, gaps, portal config, API keys
  /api/v1/*                 the CRM integration surface (leads/api.py)

Identity is two-layered and both layers are cookies set here. A `visitor_id`
cookie tracks an anonymous browser so history works without an account; an auth
cookie carries a session token when someone signs in. Signing in claims the
anonymous sessions (auth/users.py:claim_sessions), which is what stops a
mid-conversation signup from appearing to erase the conversation.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (APIRouter, Cookie, Depends, FastAPI, File, Form, Header,
                     HTTPException, Request, Response, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import config
from agent import chat, conversations, prompts, tools, voice
from auth import users
from core import db
from kb import ingest, retrieve
from kb.vector_store import STORE
from leads import api as lead_api
from leads import delivery, lifecycle, store
from portals import registry

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

_sweepers: list[Any] = []


@asynccontextmanager
async def lifespan(_: FastAPI):
    _startup()
    try:
        yield
    finally:
        for sweeper in _sweepers:
            sweeper.stop()


app = FastAPI(title="Riphah AI Property Assistant", version="1.0.0",
              lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(lead_api.router)


def _startup() -> None:
    db.migrate()
    try:
        registry.get(config.DEFAULT_PORTAL)
    except registry.UnknownPortal:
        from portals import seed

        seed.run()
        print("[startup] seeded portals")

    try:
        print(f"[startup] vector store: {STORE.reload()} live passages")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] vector store unavailable: {exc}")

    pruned = conversations.prune_empty()
    if pruned:
        print(f"[startup] pruned {pruned} empty sessions")

    # Two daemon threads: one moves sessions through the lifecycle and finalises
    # leads, one works the webhook backlog. Both are idempotent, so a restart
    # mid-sweep loses nothing.
    for sweeper in (lifecycle.Sweeper(), delivery.Sweeper()):
        sweeper.start()
        _sweepers.append(sweeper)
    print("[startup] lifecycle + delivery sweepers running")


# ------------------------------------------------------------------- identity

def current_visitor(riphah_visitor: str | None = Cookie(default=None)) -> str | None:
    return riphah_visitor


def current_user(riphah_auth: str | None = Cookie(default=None)) -> dict[str, Any] | None:
    return users.current_user(riphah_auth)


def _set_visitor_cookie(response: Response, visitor_id: str) -> None:
    response.set_cookie(
        config.VISITOR_COOKIE, visitor_id,
        max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax",
    )


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        config.AUTH_COOKIE, token,
        max_age=60 * 60 * 24 * config.SESSION_TTL_DAYS,
        httponly=True, samesite="lax",
    )


def _request_meta(request: Request) -> dict[str, Any]:
    """Source metadata captured once per session (spec stage 2)."""
    agent_string = request.headers.get("user-agent", "")
    device = "mobile" if any(
        marker in agent_string.lower()
        for marker in ("iphone", "android", "mobile", "ipad")
    ) else "desktop"
    return {
        "referrer": request.headers.get("referer"),
        "device": device,
        # Coarse region only, from a proxy header when one is present. There is no
        # IP-geolocation database here and inventing a country from a header the
        # client controls would be worse than leaving it null.
        "region": request.headers.get("cf-ipcountry") or request.headers.get(
            "x-vercel-ip-country"),
        "ip": (request.client.host if request.client else None),
    }


# --------------------------------------------------------------------- health

@app.get("/api/health")
def health() -> dict[str, Any]:
    counts = db.counts()
    ready = counts["chunks_embedded"] > 0
    can_deliver, delivery_reason = delivery.enabled()
    return {
        "ready": ready,
        "knowledge_base": counts,
        "vector_store": STORE.size,
        "models": {
            "chat": config.CHAT_MODEL,
            "extraction": config.EXTRACT_MODEL,
            "embeddings": f"{config.EMBED_MODEL} @ {config.EMBED_DIMENSIONS}d",
            "realtime": config.REALTIME_MODEL,
        },
        # Length-checked because .env.example ships a literal "sk-proj-..."
        # placeholder, and reporting that as present sends you debugging the
        # wrong thing entirely.
        "openai_key_present": config.has_openai_key(),
        "lead_delivery": {"enabled": can_deliver, "reason": delivery_reason,
                          "target": config.WEBHOOK_URL or None},
        "lifecycle": {"idle_after_minutes": config.IDLE_AFTER_MINUTES,
                      "inactive_after_minutes": config.INACTIVE_AFTER_MINUTES},
        "hint": None if ready else "Run: python -m kb.build",
    }


# ------------------------------------------------------------------ bootstrap

@app.get("/api/bootstrap")
def bootstrap(request: Request, response: Response,
              portal: str = config.DEFAULT_PORTAL,
              session_id: str | None = None,
              landing_url: str | None = None,
              utm_source: str | None = None,
              utm_medium: str | None = None,
              utm_campaign: str | None = None,
              visitor: str | None = Depends(current_visitor),
              user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    """Everything the widget needs on page load, in one round-trip.

    Combined deliberately: branding, consent copy, the field schema, the session
    id and the visitor's history are all needed before the first paint, and four
    separate requests would show the visitor an empty box while they resolved.
    """
    try:
        portal_config = registry.get(portal)
    except registry.UnknownPortal as exc:
        raise HTTPException(404, f"Unknown portal '{portal}'.") from exc

    origin = request.headers.get("origin")
    if not registry.domain_allowed(portal, origin):
        raise HTTPException(
            403,
            f"This portal key is not authorised for {origin}. Add the domain to "
            f"the portal's allowed_domains.",
        )

    if not visitor:
        visitor = str(uuid.uuid4())
        _set_visitor_cookie(response, visitor)

    meta = _request_meta(request)
    sid = conversations.ensure(
        session_id, portal_key=portal, visitor_id=visitor,
        user_id=user["id"] if user else None,
        landing_url=landing_url, referrer=meta["referrer"],
        utm={"utm_source": utm_source, "utm_medium": utm_medium,
             "utm_campaign": utm_campaign},
        device=meta["device"], region=meta["region"], ip=meta["ip"],
    )

    clause, params = users.sessions_for(user, visitor)
    return {
        "portal": {
            "key": portal_config["portal_key"],
            "display_name": portal_config["display_name"],
            "greeting": portal_config["greeting"],
            "languages": portal_config["languages"],
            "branding": portal_config["branding"],
            "consent_notice": portal_config["consent_notice"],
            "consent_version": portal_config["consent_version"],
            # The widget shows the sign-in gate before the chat when this is set.
            "require_auth": portal_config["require_auth"],
            # Exposed so the UI can label the pricing behaviour honestly rather
            # than letting the visitor discover it by being refused.
            "pricing_mode": portal_config["pricing_mode"],
            "fields": [
                {"key": f["field_key"], "label": f["label"],
                 "type": f["field_type"], "required": f["required"]}
                for f in portal_config["fields"]
            ],
        },
        "session_id": sid,
        "consent_given": conversations.has_consent(sid),
        "user": user,
        "history": conversations.recent(where=clause, params=params, limit=25),
        "captured": store.captured_for_session(sid),
        "ready": db.counts()["chunks_embedded"] > 0,
        "voice_available": config.has_openai_key(),
    }


# ------------------------------------------------------------------------ auth

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


class SignupRequest(BaseModel):
    email: str
    password: str
    name: str | None = None
    phone: str | None = None
    marketing_opt_in: bool = False


@auth_router.post("/signup")
def signup(payload: SignupRequest, request: Request, response: Response,
           visitor: str | None = Depends(current_visitor)) -> dict[str, Any]:
    try:
        result = users.signup(
            email=payload.email, password=payload.password, name=payload.name,
            phone=payload.phone, marketing_opt_in=payload.marketing_opt_in,
            visitor_id=visitor, user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
    except users.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    _set_auth_cookie(response, result.pop("token"))
    return result


class LoginRequest(BaseModel):
    email: str
    password: str


@auth_router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response,
          visitor: str | None = Depends(current_visitor)) -> dict[str, Any]:
    try:
        result = users.login(
            email=payload.email, password=payload.password, visitor_id=visitor,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
    except users.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    _set_auth_cookie(response, result.pop("token"))
    return result


@auth_router.post("/logout")
def logout(response: Response,
           riphah_auth: str | None = Cookie(default=None)) -> dict[str, Any]:
    if riphah_auth:
        users.logout(riphah_auth)
    response.delete_cookie(config.AUTH_COOKIE)
    return {"ok": True}


@auth_router.get("/me")
def me(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    return {"user": user}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


@auth_router.post("/password")
def change_password(payload: PasswordChange, response: Response,
                    user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    if not user:
        raise HTTPException(401, "Sign in required.")
    try:
        users.change_password(user["id"], current=payload.current_password,
                              new=payload.new_password)
    except users.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Every session was revoked, including this one.
    response.delete_cookie(config.AUTH_COOKIE)
    return {"ok": True, "reauth_required": True}


app.include_router(auth_router)


# ------------------------------------------------------------------------ chat

class ChatRequest(BaseModel):
    message: str
    session_id: str
    portal: str = config.DEFAULT_PORTAL
    language: str | None = None
    channel: str = "text"


def _guard_session(session_id: str, user: dict[str, Any] | None,
                   visitor: str | None,
                   service: dict[str, Any] | None = None) -> dict[str, Any]:
    """Confirm the requester owns this session.

    Without this check a session id — which travels in request bodies and appears
    in the CRM — would be enough to read or extend someone else's conversation.

    A `chat:proxy` service is allowed through, but only for sessions it created:
    `visitor_id` is set to `service:<key_prefix>` at creation, so one hub cannot
    reach into a browser visitor's conversation or another consumer's.
    """
    session = conversations.get(session_id)
    if not session:
        raise HTTPException(404, "No such session.")
    owns = (
        (user and session["user_id"] == user["id"])
        or (visitor and session["visitor_id"] == visitor)
        or (service and session["visitor_id"] == f"service:{service['key_prefix']}")
    )
    if not owns:
        raise HTTPException(403, "That conversation belongs to someone else.")
    return session


def service_caller(x_api_key: str | None = Header(default=None)) -> dict[str, Any] | None:
    """A trusted internal service calling on a visitor's behalf.

    The voice hub is the caller this exists for: the visitor is talking to the hub,
    the hub relays each turn here, and there is no browser cookie in that path.

    This is not a hole in the sign-in gate. The gate stops *anonymous public*
    traffic — someone pointing curl at the endpoint and spending Riphah's model
    budget. A key holder is neither anonymous nor public: the key is minted per
    consumer, scoped, revocable, and every call updates `last_used_at`. What the
    key does NOT do is invent a user; leads from proxied conversations still need
    real contact details, which the hub collects by voice.
    """
    if not x_api_key:
        return None
    from leads.api import _resolve_key

    key = _resolve_key(x_api_key)
    scopes = key.get("scopes") or []
    if "*" not in scopes and "chat:proxy" not in scopes:
        raise HTTPException(
            403, "This key lacks the 'chat:proxy' scope needed to relay a "
                 "conversation on a visitor's behalf.")
    return key


def _guard_auth_required(portal_key: str, user: dict[str, Any] | None,
                         service: dict[str, Any] | None = None) -> None:
    """Enforce a portal's sign-in requirement.

    Server-side because the frontend gate is a convenience, not a control: the
    chat endpoint is reachable with curl, and a gate that only exists in
    JavaScript protects nothing.
    """
    try:
        portal = registry.get(portal_key)
    except registry.UnknownPortal as exc:
        raise HTTPException(404, f"Unknown portal '{portal_key}'.") from exc
    if portal.get("require_auth") and not user and not service:
        raise HTTPException(
            401,
            "This portal requires you to sign in before chatting. "
            "Create an account or sign in, then start the conversation.",
        )


@app.post("/api/chat")
def post_chat(payload: ChatRequest,
              user: dict[str, Any] | None = Depends(current_user),
              visitor: str | None = Depends(current_visitor),
              service: dict[str, Any] | None = Depends(service_caller)) -> dict[str, Any]:
    _guard_auth_required(payload.portal, user, service)
    _guard_session(payload.session_id, user, visitor, service)
    if not config.has_openai_key():
        raise HTTPException(503, "OPENAI_API_KEY is not configured on the server.")

    try:
        return chat.answer(
            payload.message, session_id=payload.session_id,
            portal_key=payload.portal, language=payload.language,
            channel=payload.channel,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        lowered = message.lower()
        if "insufficient_quota" in lowered or "exceeded your current quota" in lowered:
            raise HTTPException(
                503,
                "The OpenAI account has run out of quota. Top it up at "
                "platform.openai.com to restore the assistant.",
            ) from exc
        if "rate_limit" in lowered or "429" in message:
            raise HTTPException(429, "Rate-limited by OpenAI; retry shortly.") from exc
        if "invalid_api_key" in lowered or "incorrect api key" in lowered:
            raise HTTPException(503, "The OpenAI API key is rejected. Check .env.") from exc
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc


class ConsentRequest(BaseModel):
    session_id: str
    granted: bool = True
    portal: str = config.DEFAULT_PORTAL


@app.post("/api/consent")
def post_consent(payload: ConsentRequest,
                 user: dict[str, Any] | None = Depends(current_user),
                 visitor: str | None = Depends(current_visitor)) -> dict[str, Any]:
    _guard_session(payload.session_id, user, visitor)
    return conversations.record_consent(payload.session_id, portal_key=payload.portal,
                                        granted=payload.granted)


# -------------------------------------------------------------------- sessions

@app.get("/api/sessions")
def list_sessions(limit: int = 25,
                  user: dict[str, Any] | None = Depends(current_user),
                  visitor: str | None = Depends(current_visitor)) -> dict[str, Any]:
    clause, params = users.sessions_for(user, visitor)
    return {"sessions": conversations.recent(where=clause, params=params,
                                             limit=max(1, min(limit, 100)))}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str,
                user: dict[str, Any] | None = Depends(current_user),
                visitor: str | None = Depends(current_visitor)) -> dict[str, Any]:
    _guard_session(session_id, user, visitor)
    data = conversations.transcript(session_id)
    data["session"].pop("ip_hash", None)
    data["captured"] = store.captured_for_session(session_id)
    return data


@app.post("/api/sessions")
def new_session(request: Request, response: Response,
                portal: str = config.DEFAULT_PORTAL,
                user: dict[str, Any] | None = Depends(current_user),
                visitor: str | None = Depends(current_visitor),
                service: dict[str, Any] | None = Depends(service_caller)) -> dict[str, Any]:
    """Start a fresh conversation, keeping the same visitor identity."""
    if service and not visitor:
        # Sessions a service opens are stamped with the key that opened them, so
        # `_guard_session` can let that consumer back in and nobody else — a
        # random uuid would make the session unreachable on the next turn.
        visitor = f"service:{service['key_prefix']}"
    if not visitor:
        visitor = str(uuid.uuid4())
        _set_visitor_cookie(response, visitor)
    meta = _request_meta(request)
    session = conversations.create(
        portal_key=portal, visitor_id=visitor,
        user_id=user["id"] if user else None,
        referrer=meta["referrer"], device=meta["device"],
        region=meta["region"], ip=meta["ip"],
    )
    return {"session_id": session["id"]}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str,
                   user: dict[str, Any] | None = Depends(current_user),
                   visitor: str | None = Depends(current_visitor)) -> dict[str, Any]:
    clause, params = users.sessions_for(user, visitor)
    if not conversations.delete(session_id, where=clause, params=params):
        raise HTTPException(404, "No such conversation.")
    return {"deleted": session_id}


@app.post("/api/sessions/{session_id}/end")
def end_session(session_id: str,
                user: dict[str, Any] | None = Depends(current_user),
                visitor: str | None = Depends(current_visitor)) -> dict[str, Any]:
    """Explicitly finalise a conversation.

    Exists so a visitor who says goodbye does not wait thirty minutes for the
    lifecycle sweeper before their lead reaches the CRM.
    """
    _guard_session(session_id, user, visitor)
    return lifecycle.finalise(session_id)


# --------------------------------------------------------------------- voice

class VoiceSessionRequest(BaseModel):
    session_id: str
    portal: str = config.DEFAULT_PORTAL
    voice: str | None = None
    language_hint: str | None = None


@app.post("/api/voice/realtime-session")
async def realtime_session(payload: VoiceSessionRequest,
                           user: dict[str, Any] | None = Depends(current_user),
                           visitor: str | None = Depends(current_visitor)) -> dict[str, Any]:
    # Voice is a paid model call like any other, so the gate applies here too.
    _guard_auth_required(payload.portal, user)
    _guard_session(payload.session_id, user, visitor)
    try:
        return await voice.mint_session(
            portal_key=payload.portal, session_id=payload.session_id,
            voice=payload.voice, language_hint=payload.language_hint,
        )
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/voice/transcribe")
async def transcribe(session_id: str = Form(...),
                     language: str | None = Form(default=None),
                     portal: str = Form(default=config.DEFAULT_PORTAL),
                     audio: UploadFile = File(...),
                     user: dict[str, Any] | None = Depends(current_user),
                     visitor: str | None = Depends(current_visitor)) -> dict[str, Any]:
    """Fallback path, step 1: speech -> text."""
    _guard_auth_required(portal, user)
    _guard_session(session_id, user, visitor)
    data = await audio.read()
    if not data:
        raise HTTPException(422, "Empty audio upload.")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "Audio too large (25 MB limit).")
    try:
        return voice.transcribe(data, content_type=audio.content_type,
                                language=language)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Transcription failed: {exc}") from exc


class SpeakRequest(BaseModel):
    text: str
    voice: str | None = None


@app.post("/api/voice/speak")
def speak(payload: SpeakRequest) -> Response:
    """Fallback path, step 3: text -> speech."""
    if not payload.text.strip():
        raise HTTPException(422, "Nothing to speak.")
    try:
        audio = voice.speak(payload.text, voice=payload.voice)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Speech synthesis failed: {exc}") from exc
    return Response(content=audio, media_type="audio/mpeg")


# ---------------------------------------------------------------- tool bridge

class ToolRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    portal: str = config.DEFAULT_PORTAL


@app.post("/api/tools/{tool_name}")
def run_tool(tool_name: str, payload: ToolRequest | None = None,
             user: dict[str, Any] | None = Depends(current_user),
             visitor: str | None = Depends(current_visitor)) -> JSONResponse:
    """Execute a tool on behalf of the browser's Realtime data channel.

    With WebRTC, audio and function calls run browser↔OpenAI directly, so the call
    surfaces in the browser — which has no database and must never hold an API
    key. It posts the call here, this process runs the query, and the browser
    returns the result over its data channel.
    """
    if tool_name not in tools.DISPATCH:
        raise HTTPException(404, f"Unknown tool '{tool_name}'.")
    payload = payload or ToolRequest()
    if payload.session_id:
        _guard_session(payload.session_id, user, visitor)
    ctx = tools.tool_context(portal_key=payload.portal, session_id=payload.session_id)
    return JSONResponse(tools.execute(tool_name, payload.arguments, ctx))


class VoiceTurnsRequest(BaseModel):
    turns: list[dict[str, Any]] = Field(default_factory=list)


@app.post("/api/sessions/{session_id}/turns")
def append_turns(session_id: str, payload: VoiceTurnsRequest,
                 portal: str = config.DEFAULT_PORTAL,
                 user: dict[str, Any] | None = Depends(current_user),
                 visitor: str | None = Depends(current_visitor)) -> dict[str, Any]:
    """Record voice turns as they happen.

    The voice transcript arrives in the browser, so the browser reports each turn
    here as it happens rather than at the end of the call — which would lose
    everything if the tab closed mid-conversation. Extraction runs over the batch
    afterwards, so a spoken budget still becomes a lead field.
    """
    _guard_auth_required(portal, user)
    _guard_session(session_id, user, visitor)
    stored = conversations.add_messages(session_id, payload.turns, channel="voice")

    lead_state: dict[str, Any] = {}
    if stored:
        try:
            from agent import extraction

            captured = store.captured_for_session(session_id)
            extracted = extraction.extract(
                portal, conversations.history(session_id), already_captured=captured
            )
            result = store.apply_extraction(
                session_id=session_id, portal_key=portal, extracted=extracted
            )
            if result:
                lead_state = result
                lead_state["dispatched"] = delivery.dispatch(result)
        except Exception as exc:  # noqa: BLE001
            print(f"[voice] extraction failed: {type(exc).__name__}: {exc}")

    return {"session_id": session_id, "stored": stored, "lead": lead_state,
            "captured": store.captured_for_session(session_id)}


# --------------------------------------------------------------------- admin

admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    try:
        return users.require_role(user, "admin", "agent")
    except users.AuthError as exc:
        raise HTTPException(403 if user else 401, str(exc)) from exc


@admin_router.get("/documents")
def admin_documents(portal: str | None = None,
                    include_retired: bool = False,
                    _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return {"documents": ingest.listing(portal, include_retired=include_retired)}


@admin_router.post("/documents")
async def admin_upload(portal: str = Form(default=config.DEFAULT_PORTAL),
                       classification: str = Form(default="public"),
                       project: str | None = Form(default=None),
                       publish: bool = Form(default=False),
                       file: UploadFile = File(...),
                       admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Upload a knowledge base document.

    `publish` defaults to False: an uploaded document is invisible to the
    assistant until a human publishes it (spec s6 step 11). Files committed to
    `content/` take the other default, because review happened in the pull request.
    """
    data = await file.read()
    name = file.filename or "upload"
    suffix = Path(name).suffix.lower()
    if suffix not in ingest.SUPPORTED_SUFFIXES:
        raise HTTPException(
            422, f"Unsupported file type '{suffix}'. "
                 f"Accepted: {sorted(ingest.SUPPORTED_SUFFIXES)}"
        )

    config.ensure_dirs()
    path = config.UPLOAD_DIR / name
    path.write_bytes(data)
    try:
        result = ingest.ingest_file(path, portal_key=portal, publish=publish,
                                    actor=f"user:{admin['id']}")
    except ingest.RestrictedDocument as exc:
        path.unlink(missing_ok=True)
        # 422 rather than 403: the request was well-formed, the content is not
        # acceptable. And the file is deleted, so a restricted document leaves no
        # trace on disk either.
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    if project or classification != "public":
        with db.tx() as conn:
            conn.execute(
                "UPDATE kb_documents SET project = COALESCE(?, project), "
                "classification = ? WHERE id = ?",
                (project, classification, result["id"]),
            )
    return {"document": result,
            "next": "Chunk and embed with: python -m kb.build --only chunk --only embed"}


@admin_router.post("/documents/{document_id}/publish")
def admin_publish(document_id: int,
                  admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    if not ingest.publish(document_id, actor=f"user:{admin['id']}"):
        raise HTTPException(404, "No such document.")
    return {"published": document_id, "vector_store": STORE.reload()}


@admin_router.post("/documents/{document_id}/retire")
def admin_retire(document_id: int,
                 admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    if not ingest.retire(document_id, actor=f"user:{admin['id']}"):
        raise HTTPException(404, "No such document.")
    # Reloading here is what makes retirement immediate: the store only loads
    # published, non-retired passages.
    return {"retired": document_id, "vector_store": STORE.reload()}


@admin_router.post("/reload")
def admin_reload(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return {"vector_store": STORE.reload()}


@admin_router.get("/gaps")
def admin_gaps(portal: str | None = None, limit: int = 50,
               _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """The content backlog: questions the knowledge base could not answer."""
    return {"gaps": retrieve.gap_report(portal_key=portal, limit=limit)}


class ApiKeyRequest(BaseModel):
    label: str
    portal_key: str | None = None
    scopes: list[str] = Field(default_factory=lambda: ["leads:read"])


@admin_router.post("/api-keys")
def admin_create_key(payload: ApiKeyRequest,
                     admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return lead_api.create_key(
        label=payload.label, portal_key=payload.portal_key,
        scopes=payload.scopes, actor=f"user:{admin['id']}",
    )


@admin_router.get("/api-keys")
def admin_list_keys(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    return {"keys": db.query(
        "SELECT id, key_prefix, label, portal_key, scopes, created_at, "
        "       last_used_at, revoked_at FROM api_keys ORDER BY id DESC"
    )}


@admin_router.delete("/api-keys/{key_id}")
def admin_revoke_key(key_id: int,
                     admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (db.now(), key_id),
        )
    if not cur.rowcount:
        raise HTTPException(404, "No such active key.")
    db.audit(f"user:{admin['id']}", "api_key.revoked", entity="api_key",
             entity_id=key_id)
    return {"revoked": key_id}


@admin_router.get("/chats")
def admin_chats(q: str | None = None, portal: str | None = None,
                limit: int = 100,
                _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Every visitor conversation, for the staff chat monitor sidebar."""
    return {"chats": conversations.admin_listing(q=q, portal=portal, limit=limit)}


@admin_router.get("/chats/{session_id}")
def admin_chat_detail(session_id: str,
                      _: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """One conversation in full: transcript, captured fields, and the lead."""
    data = conversations.transcript(session_id)
    if not data.get("found"):
        raise HTTPException(404, "No such conversation.")
    data["session"].pop("ip_hash", None)
    data["captured"] = store.captured_for_session(session_id)
    data["lead"] = db.one(
        """
        SELECT l.lead_ref, l.name, l.email, l.phone, l.qualification, l.score,
               l.status, l.contact_source, l.marketing_opt_in, l.created_at
          FROM lead_sessions ls JOIN leads l ON l.id = ls.lead_id
         WHERE ls.session_id = ? LIMIT 1
        """,
        (session_id,),
    )
    return data


@admin_router.post("/lifecycle/sweep")
def admin_sweep(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Force a lifecycle sweep. Useful in testing and after a config change."""
    return {"lifecycle": lifecycle.sweep(), "delivery": delivery.flush()}


app.include_router(admin_router)


# ------------------------------------------------------------------- frontend

@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/admin")
def admin_page() -> FileResponse:
    """The staff chat monitor. The page itself is public; every API call it
    makes sits behind require_admin, so an unauthenticated visitor sees only
    the sign-in card."""
    return FileResponse(FRONTEND_DIR / "admin.html")


@app.get("/widget.js")
def widget() -> FileResponse:
    """Embeddable snippet. Served from here so the portal only needs one script tag."""
    return FileResponse(FRONTEND_DIR / "widget.js", media_type="application/javascript")


def main() -> None:
    import uvicorn

    uvicorn.run("agent.server:app", host=config.HOST, port=config.PORT,
                reload=bool(__import__("os").getenv("RELOAD")))


if __name__ == "__main__":
    main()
