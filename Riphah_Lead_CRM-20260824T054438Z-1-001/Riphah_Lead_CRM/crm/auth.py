"""Staff accounts and role-based access (scope document stage 11).

Three roles, because the access questions are genuinely different:

  agent    — works leads. Sees and edits the leads assigned to them, plus the
             unassigned pool they can pick from. Cannot export or see analytics
             across the whole team.
  manager  — sees everything, assigns work, reads analytics, exports.
  admin    — manager, plus staff administration and integration settings.

The agent restriction is not decoration. A CSV export of every lead with phone
numbers is the single most portable asset in this system, and "which consultant
took the database to a competitor" is a real question in property sales. So export
is manager-and-above and every export is logged to the activity trail with who and
how many rows.
"""
from __future__ import annotations

from typing import Any

import config
from core import db, security

ROLES = ("agent", "manager", "admin")
# Role inclusion, so a check is a set membership rather than a chain of ifs.
_RANK = {"agent": 0, "manager": 1, "admin": 2}


class AuthError(Exception):
    """Safe to show a user."""


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "email": row["email"], "name": row["name"],
            "role": row["role"], "created_at": row["created_at"]}


def create_staff(*, email: str, password: str, name: str | None = None,
                 role: str = "agent", actor: str = "system") -> dict[str, Any]:
    normalised = security.normalise_email(email)
    if not normalised:
        raise AuthError("That doesn't look like a valid email address.")
    if role not in ROLES:
        raise AuthError(f"Role must be one of {ROLES}.")
    try:
        password_hash = security.hash_password(password)
    except security.WeakPassword as exc:
        raise AuthError(str(exc)) from exc

    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM staff WHERE email = ?",
                        (normalised,)).fetchone():
            raise AuthError("That email already has an account.")
        cur = conn.execute(
            "INSERT INTO staff (email, name, password_hash, role, created_at) "
            "VALUES (?,?,?,?,?)",
            (normalised, (name or "").strip() or None, password_hash, role, db.now()),
        )
        staff_id = cur.lastrowid

    db.log_activity(None, actor, "staff_created",
                    {"email": normalised, "role": role})
    return _public(db.one("SELECT * FROM staff WHERE id = ?", (staff_id,)))


def login(*, email: str, password: str) -> dict[str, Any]:
    normalised = security.normalise_email(email) or email.strip()
    row = db.one("SELECT * FROM staff WHERE email = ?", (normalised,))

    # Comparable work either way, so timing does not distinguish "no such account"
    # from "wrong password".
    reference = row["password_hash"] if row else security.hash_password("x" * 12)
    if not security.verify_password(password, reference) or not row:
        raise AuthError("Email or password is incorrect.")
    if row["disabled_at"]:
        raise AuthError("This account has been disabled.")

    token = security.new_token()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO staff_sessions (token_hash, staff_id, created_at, expires_at) "
            "VALUES (?,?,?,?)",
            (security.token_hash(token), row["id"], db.now(),
             db.days_from_now(config.SESSION_TTL_DAYS)),
        )
        conn.execute("UPDATE staff SET last_login_at = ? WHERE id = ?",
                     (db.now(), row["id"]))
    return {"token": token, "user": _public(row)}


def logout(token: str) -> bool:
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE staff_sessions SET revoked_at = ? "
            " WHERE token_hash = ? AND revoked_at IS NULL",
            (db.now(), security.token_hash(token)),
        )
    return cur.rowcount > 0


def current_user(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    row = db.one(
        """
        SELECT s.* FROM staff_sessions ss JOIN staff s ON s.id = ss.staff_id
         WHERE ss.token_hash = ? AND ss.revoked_at IS NULL AND ss.expires_at > ?
           AND s.disabled_at IS NULL
        """,
        (security.token_hash(token), db.now()),
    )
    return _public(row) if row else None


def require(user: dict[str, Any] | None, minimum: str = "agent") -> dict[str, Any]:
    if not user:
        raise AuthError("Sign in required.")
    if _RANK.get(user.get("role", ""), -1) < _RANK[minimum]:
        raise AuthError(f"This needs {minimum} access or above.")
    return user


def lead_scope(user: dict[str, Any]) -> tuple[str, list[Any]]:
    """The rows this user may see, as (clause, params).

    An agent sees their own leads plus the unassigned pool — they need the pool to
    pick work from, and hiding it would make self-service assignment impossible.
    Managers and admins see everything.
    """
    if _RANK.get(user["role"], 0) >= _RANK["manager"]:
        return "1 = 1", []
    return "(l.assigned_owner = ? OR l.assigned_owner IS NULL)", [user["email"]]


def bootstrap_admin() -> str | None:
    """Create the first admin on an empty database.

    Runs only when `staff` is empty and `BOOTSTRAP_ADMIN_PASSWORD` is set. Shipping
    a default password would be worse; requiring a manual insert before anyone can
    log in would be annoying. This is the middle, and the README says to change it
    after first login.
    """
    if db.scalar("SELECT COUNT(*) FROM staff"):
        return None
    if not config.BOOTSTRAP_ADMIN_PASSWORD:
        return ("No staff accounts exist and BOOTSTRAP_ADMIN_PASSWORD is unset. "
                "Set it in .env and restart, or run: python -m crm.manage "
                "create-staff")
    try:
        create_staff(email=config.BOOTSTRAP_ADMIN_EMAIL,
                     password=config.BOOTSTRAP_ADMIN_PASSWORD,
                     name="Administrator", role="admin", actor="bootstrap")
    except AuthError as exc:
        return f"Bootstrap admin not created: {exc}"
    return f"Created bootstrap admin {config.BOOTSTRAP_ADMIN_EMAIL} — change the password."


def listing() -> list[dict[str, Any]]:
    return db.query(
        "SELECT id, email, name, role, created_at, last_login_at, disabled_at "
        "  FROM staff ORDER BY id"
    )


def owners() -> list[str]:
    """Assignable owners, for the dashboard's assignment dropdown."""
    return [
        row["email"] for row in db.query(
            "SELECT email FROM staff WHERE disabled_at IS NULL ORDER BY email"
        )
    ]
