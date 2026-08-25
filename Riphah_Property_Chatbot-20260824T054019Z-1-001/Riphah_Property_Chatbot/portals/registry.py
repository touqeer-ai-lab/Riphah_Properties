"""Portal configuration: read, write, and validate the per-portal field schema.

This module is the architectural hinge described in the scope document (s5). The
reference build hard-coded its lead fields, so serving a second business meant a
rewrite. Here a portal is a row in `portals` and its capture fields are rows in
`portal_fields`; the extractor, the prompt, the scorer and the API all read from
those rows at request time.

The practical test: adding `hostel_required` to the admission portal is one
`POST /api/v1/portals/riphah-admission/fields`, and the assistant starts asking
about it on the next turn. No deploy, no migration.

Config is cached in-process because it is read on every single turn (prompt build
+ extraction schema + scoring) and changes a few times a month. `invalidate()`
is called by every writer here, so a write through this module is visible
immediately; a write straight to SQLite is not.
"""
from __future__ import annotations

import threading
from typing import Any

from core import db

# Field types the extractor knows how to normalise. Anything else is rejected at
# write time rather than discovered at extraction time.
FIELD_TYPES = ("text", "enum", "money", "int", "bool", "phone", "email", "date")

# Pricing guardrail modes (spec s8). 'refer' is the default everywhere because
# it is the only one that cannot misquote a price.
PRICING_MODES = ("refer", "indicative", "live")

# Contact fields are implicit on every portal — the spec is explicit that name,
# email and phone are identical across all three example portals, so they are
# not duplicated into portal_fields.
CONTACT_FIELDS = ("name", "email", "phone")

_cache: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


class UnknownPortal(KeyError):
    pass


def invalidate(portal_key: str | None = None) -> None:
    with _lock:
        if portal_key:
            _cache.pop(portal_key, None)
        else:
            _cache.clear()


# ---------------------------------------------------------------------- reading

def get(portal_key: str, *, use_cache: bool = True) -> dict[str, Any]:
    """Full portal config including its ordered field schema.

    Raises UnknownPortal rather than returning a default. A widget presenting an
    unrecognised key is either a typo or someone else's site; both deserve a 404.
    """
    if use_cache:
        with _lock:
            cached = _cache.get(portal_key)
        if cached:
            return cached

    row = db.one(
        "SELECT * FROM portals WHERE portal_key = ? AND active = 1", (portal_key,)
    )
    if not row:
        raise UnknownPortal(portal_key)

    config = {
        "portal_key": row["portal_key"],
        "display_name": row["display_name"],
        "persona": row["persona"],
        "greeting": row["greeting"],
        "languages": db.loads(row["languages"], ["en"]),
        "allowed_domains": db.loads(row["allowed_domains"], []),
        "knowledge_scope": db.loads(row["knowledge_scope"], []),
        "consent_notice": row["consent_notice"],
        "consent_version": row["consent_version"],
        "require_auth": bool(row["require_auth"]),
        "pricing_mode": row["pricing_mode"] if row["pricing_mode"] in PRICING_MODES else "refer",
        "scoring_rules": db.loads(row["scoring_rules"], {}),
        "branding": db.loads(row["branding"], {}),
        "fields": fields(portal_key),
    }
    with _lock:
        _cache[portal_key] = config
    return config


def fields(portal_key: str) -> list[dict[str, Any]]:
    """Ordered field schema. `sort_order` is the ask priority, not just display."""
    rows = db.query(
        "SELECT field_key, label, field_type, options, required, sort_order, "
        "       prompt_hint, extract_hint "
        "  FROM portal_fields WHERE portal_key = ? "
        " ORDER BY sort_order, id",
        (portal_key,),
    )
    for row in rows:
        row["options"] = db.loads(row["options"], [])
        row["required"] = bool(row["required"])
    return rows


def field(portal_key: str, field_key: str) -> dict[str, Any] | None:
    for candidate in get(portal_key)["fields"]:
        if candidate["field_key"] == field_key:
            return candidate
    return None


def listing(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    where = "" if include_inactive else "WHERE active = 1"
    rows = db.query(
        f"SELECT portal_key, display_name, pricing_mode, active, created_at "
        f"  FROM portals {where} ORDER BY portal_key"
    )
    for row in rows:
        row["field_count"] = db.scalar(
            "SELECT COUNT(*) FROM portal_fields WHERE portal_key = ?", (row["portal_key"],)
        )
    return rows


def domain_allowed(portal_key: str, origin: str | None) -> bool:
    """Domain whitelist for the widget key (spec stage 1).

    An empty list means the portal has not been configured for production yet, so
    only local development origins pass. That default is deliberate: a portal
    created without a domain list should not be embeddable anywhere on the
    internet.
    """
    allowed = get(portal_key)["allowed_domains"]
    if not origin:
        # Server-to-server and same-origin requests send no Origin header.
        return True
    host = origin.split("//")[-1].split("/")[0].split(":")[0].lower()
    if not allowed:
        return host in ("localhost", "127.0.0.1", "[::1]")
    for entry in allowed:
        entry = entry.strip().lower().lstrip("*.")
        if host == entry or host.endswith("." + entry):
            return True
    return False


# ---------------------------------------------------------------------- writing

def upsert(portal_key: str, **values: Any) -> dict[str, Any]:
    """Create or update a portal. JSON-valued columns accept Python objects."""
    json_columns = {"languages", "allowed_domains", "knowledge_scope",
                    "scoring_rules", "branding"}
    writable = {
        "display_name", "persona", "greeting", "consent_notice", "consent_version",
        "pricing_mode", "active", "require_auth", *json_columns,
    }
    unknown = set(values) - writable
    if unknown:
        raise ValueError(f"unknown portal columns: {sorted(unknown)}")
    if "pricing_mode" in values and values["pricing_mode"] not in PRICING_MODES:
        raise ValueError(f"pricing_mode must be one of {PRICING_MODES}")

    payload = {
        k: (db.dumps(v) if k in json_columns else v) for k, v in values.items()
    }
    stamp = db.now()

    with db.tx() as conn:
        exists = conn.execute(
            "SELECT 1 FROM portals WHERE portal_key = ?", (portal_key,)
        ).fetchone()
        if exists:
            if payload:
                sets = ", ".join(f"{k} = ?" for k in payload)
                conn.execute(
                    f"UPDATE portals SET {sets}, updated_at = ? WHERE portal_key = ?",
                    (*payload.values(), stamp, portal_key),
                )
        else:
            payload.setdefault("display_name", portal_key)
            payload.setdefault("persona", f"Assistant for {portal_key}")
            columns = ["portal_key", *payload, "created_at", "updated_at"]
            conn.execute(
                f"INSERT INTO portals ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(columns))})",
                (portal_key, *payload.values(), stamp, stamp),
            )

    invalidate(portal_key)
    db.audit("system", "portal.upsert", entity="portal", entity_id=portal_key,
             detail=sorted(values))
    return get(portal_key)


def upsert_field(portal_key: str, field_key: str, *, label: str,
                 field_type: str = "text", options: list[str] | None = None,
                 required: bool = False, sort_order: int = 100,
                 prompt_hint: str | None = None,
                 extract_hint: str | None = None,
                 actor: str = "system") -> dict[str, Any]:
    """Add or amend one capture field. This is the no-code-release path (spec s9.1)."""
    if field_type not in FIELD_TYPES:
        raise ValueError(f"field_type must be one of {FIELD_TYPES}")
    if field_type == "enum" and not options:
        raise ValueError("enum fields need a non-empty options list")
    if field_key in CONTACT_FIELDS:
        raise ValueError(
            f"'{field_key}' is a built-in contact field; it is captured on every "
            f"portal and must not be redefined here"
        )
    if not field_key.replace("_", "").isalnum():
        # The key becomes a JSON Schema property name in the extraction call.
        raise ValueError("field_key must be alphanumeric with underscores")

    with db.tx() as conn:
        conn.execute(
            """
            INSERT INTO portal_fields (portal_key, field_key, label, field_type,
                                       options, required, sort_order, prompt_hint,
                                       extract_hint, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (portal_key, field_key) DO UPDATE SET
                 label = excluded.label,
                 field_type = excluded.field_type,
                 options = excluded.options,
                 required = excluded.required,
                 sort_order = excluded.sort_order,
                 prompt_hint = excluded.prompt_hint,
                 extract_hint = excluded.extract_hint
            """,
            (portal_key, field_key, label, field_type, db.dumps(options or None),
             int(required), sort_order, prompt_hint, extract_hint, db.now()),
        )

    invalidate(portal_key)
    db.audit(actor, "portal_field.upsert", entity="portal_field",
             entity_id=f"{portal_key}.{field_key}",
             detail={"label": label, "type": field_type, "required": required})
    return field(portal_key, field_key) or {}


def delete_field(portal_key: str, field_key: str, *, actor: str = "system") -> bool:
    """Remove a field from the schema.

    Values already captured under it are left in `lead_field_values` on purpose:
    a lead a salesperson worked last week should not lose its budget because the
    schema was tidied today.
    """
    with db.tx() as conn:
        cur = conn.execute(
            "DELETE FROM portal_fields WHERE portal_key = ? AND field_key = ?",
            (portal_key, field_key),
        )
    invalidate(portal_key)
    if cur.rowcount:
        db.audit(actor, "portal_field.delete", entity="portal_field",
                 entity_id=f"{portal_key}.{field_key}")
    return cur.rowcount > 0
