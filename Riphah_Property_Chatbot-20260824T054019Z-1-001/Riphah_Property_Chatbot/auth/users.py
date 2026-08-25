"""Accounts, login sessions, and the anonymous-to-registered handover.

Signing in is **optional**. A visitor can open the widget and chat immediately —
requiring an account before a property enquiry would cost more leads than it
captures. What an account buys the visitor is continuity: their conversations
follow them to another device.

The mechanism that makes that work is `claim_sessions()`. An anonymous visitor is
tracked by a `visitor_id` in a first-party cookie. When they sign up or log in,
every chat session carrying that visitor_id is attached to the account — including
the one they are in the middle of. Without this step, signing up mid-conversation
would appear to erase the conversation, which is exactly when a visitor is most
likely to sign up (they have just been asked for contact details).

Any lead already created from those sessions is linked to the account at the same
time, so the CRM shows one person rather than an anonymous lead and a registered
user who happen to be the same human.
"""
from __future__ import annotations

from typing import Any

import config
from core import db, security


class AuthError(Exception):
    """Credential or account-state failure. The message is safe to show a user."""


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """A user record without the password hash. Everything returned to a client
    goes through this, so a hash cannot leak by someone forgetting to strip it."""
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "phone": row["phone"],
        "role": row["role"],
        "marketing_opt_in": bool(row["marketing_opt_in"]),
        "created_at": row["created_at"],
    }


# ------------------------------------------------------------------------ signup

def signup(*, email: str, password: str, name: str | None = None,
           phone: str | None = None, marketing_opt_in: bool = False,
           visitor_id: str | None = None,
           user_agent: str | None = None, ip: str | None = None) -> dict[str, Any]:
    """Create an account and return a session token plus the public user record."""
    normalised = security.normalise_email(email)
    if not normalised:
        raise AuthError("That doesn't look like a valid email address.")
    try:
        password_hash = security.hash_password(password)
    except security.WeakPassword as exc:
        raise AuthError(str(exc)) from exc

    phone_norm = security.normalise_phone(phone) if phone else None
    if phone and not phone_norm:
        raise AuthError("That phone number doesn't look right. Try +92 300 1234567.")

    stamp = db.now()
    with db.tx() as conn:
        existing = conn.execute(
            "SELECT 1 FROM users WHERE email = ?", (normalised,)
        ).fetchone()
        if existing:
            # Deliberately explicit rather than vague. Property portals are not a
            # context where email enumeration is a meaningful threat, and "that
            # email is already registered" saves the visitor a support message.
            raise AuthError("That email is already registered. Try signing in.")
        cur = conn.execute(
            "INSERT INTO users (email, name, phone, password_hash, role, "
            "marketing_opt_in, created_at, last_login_at) VALUES (?,?,?,?,?,?,?,?)",
            (normalised, (name or "").strip() or None, phone_norm, password_hash,
             "visitor", int(marketing_opt_in), stamp, stamp),
        )
        user_id = cur.lastrowid

    token = _mint_session(user_id, user_agent=user_agent, ip=ip)
    claimed = claim_sessions(user_id, visitor_id) if visitor_id else 0
    db.audit(f"user:{user_id}", "user.signup", entity="user", entity_id=user_id,
             detail={"claimed_sessions": claimed})

    return {
        "token": token,
        "user": _public(db.one("SELECT * FROM users WHERE id = ?", (user_id,))),
        "claimed_sessions": claimed,
    }


# ------------------------------------------------------------------------- login

def login(*, email: str, password: str, visitor_id: str | None = None,
          user_agent: str | None = None, ip: str | None = None) -> dict[str, Any]:
    normalised = security.normalise_email(email)
    row = db.one("SELECT * FROM users WHERE email = ?", (normalised or email.strip(),))

    # Same message and comparable work for both failure modes, so response timing
    # and wording don't distinguish "no such user" from "wrong password".
    reference = row["password_hash"] if row else security.hash_password("x" * 12)
    if not security.verify_password(password, reference) or not row:
        raise AuthError("Email or password is incorrect.")
    if row["disabled_at"]:
        raise AuthError("This account has been disabled. Contact the sales office.")

    stamp = db.now()
    with db.tx() as conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                     (stamp, row["id"]))
        # Opportunistic upgrade as the iteration count rises over the years.
        if security.needs_rehash(row["password_hash"]):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (security.hash_password(password), row["id"]))

    token = _mint_session(row["id"], user_agent=user_agent, ip=ip)
    claimed = claim_sessions(row["id"], visitor_id) if visitor_id else 0
    return {"token": token, "user": _public(row), "claimed_sessions": claimed}


def logout(token: str) -> bool:
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE auth_sessions SET revoked_at = ? "
            " WHERE token_hash = ? AND revoked_at IS NULL",
            (db.now(), security.token_hash(token)),
        )
    return cur.rowcount > 0


def change_password(user_id: int, *, current: str, new: str) -> None:
    row = db.one("SELECT password_hash FROM users WHERE id = ?", (user_id,))
    if not row or not security.verify_password(current, row["password_hash"]):
        raise AuthError("Current password is incorrect.")
    try:
        password_hash = security.hash_password(new)
    except security.WeakPassword as exc:
        raise AuthError(str(exc)) from exc
    with db.tx() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (password_hash, user_id))
        # Every other session is invalidated: a password change is usually a
        # response to suspecting one is compromised.
        conn.execute("UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ?",
                     (db.now(), user_id))
    db.audit(f"user:{user_id}", "user.password_changed", entity="user",
             entity_id=user_id)


# ---------------------------------------------------------------------- sessions

def _mint_session(user_id: int, *, user_agent: str | None = None,
                  ip: str | None = None) -> str:
    token = security.new_token()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, user_agent, ip, "
            "created_at, expires_at) VALUES (?,?,?,?,?,?)",
            (security.token_hash(token), user_id, (user_agent or "")[:300],
             security.hash_ip(ip), db.now(),
             db.days_from_now(config.SESSION_TTL_DAYS)),
        )
    return token


def current_user(token: str | None) -> dict[str, Any] | None:
    """Resolve a bearer token to a public user record, or None.

    Returns None for every failure — expired, revoked, unknown, disabled — because
    the caller's only correct response to any of them is to treat the request as
    anonymous.
    """
    if not token:
        return None
    row = db.one(
        """
        SELECT u.* FROM auth_sessions s
          JOIN users u ON u.id = s.user_id
         WHERE s.token_hash = ?
           AND s.revoked_at IS NULL
           AND s.expires_at > ?
           AND u.disabled_at IS NULL
        """,
        (security.token_hash(token), db.now()),
    )
    return _public(row) if row else None


def require_role(user: dict[str, Any] | None, *roles: str) -> dict[str, Any]:
    """Guard for staff-only endpoints. Raises AuthError, which the server maps to 403."""
    if not user:
        raise AuthError("Sign in required.")
    if roles and user.get("role") not in roles:
        raise AuthError("You don't have access to that.")
    return user


# ------------------------------------------------- anonymous session handover

def claim_sessions(user_id: int, visitor_id: str | None) -> int:
    """Attach an anonymous visitor's chats — and any lead built from them — to an account.

    Called on both signup and login. Idempotent: sessions already attached to this
    user are skipped, and sessions belonging to a *different* user are never
    reassigned, so a shared browser cannot hand one person's conversations to
    another.
    """
    if not visitor_id:
        return 0
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE chat_sessions SET user_id = ? "
            " WHERE visitor_id = ? AND (user_id IS NULL OR user_id = ?)",
            (user_id, visitor_id, user_id),
        )
        claimed = cur.rowcount
        # Leads created from those sessions belong to the same person. Without
        # this the CRM shows an anonymous lead and a registered user separately.
        conn.execute(
            """
            UPDATE leads SET user_id = ?, updated_at = ?
             WHERE user_id IS NULL
               AND session_id IN (SELECT id FROM chat_sessions WHERE visitor_id = ?)
            """,
            (user_id, db.now(), visitor_id),
        )
    return claimed


def sessions_for(user: dict[str, Any] | None, visitor_id: str | None) -> list[Any]:
    """Which chat sessions this requester may see, as a (clause, params) pair.

    Centralised so every history endpoint applies the same rule: a signed-in user
    sees their own sessions, an anonymous visitor sees their browser's sessions,
    and nobody sees anyone else's.
    """
    if user:
        return ["(user_id = ? OR visitor_id = ?)", [user["id"], visitor_id or ""]]
    if visitor_id:
        return ["(visitor_id = ? AND user_id IS NULL)", [visitor_id]]
    return ["1 = 0", []]
