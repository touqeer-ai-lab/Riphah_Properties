"""Preflight check: is this deployment actually going to work?

    python doctor.py

Checks configuration, the knowledge base, the model credentials, and the outbound
integration, and prints what to do about anything broken. Exists because the
failure modes here are mostly silent — an unset `WEBHOOK_SECRET` doesn't crash
anything, it just means no lead ever reaches the CRM, and you find out a week later
when someone asks why the pipeline is empty.

Ordered so the cheapest checks run first and one live model call happens last.
"""
from __future__ import annotations

import sys
from typing import Any

import config
from core import db

OK, WARN, BAD = "ok", "warn", "FAIL"
findings: list[tuple[str, str, str, str]] = []   # (level, area, message, action)


def note(level: str, area: str, message: str, action: str = "") -> None:
    findings.append((level, area, message, action))


def check_config() -> None:
    if config.has_openai_key():
        note(OK, "config", "OPENAI_API_KEY is set")
    else:
        note(BAD, "config", "OPENAI_API_KEY is missing or a placeholder",
             "cp .env.example .env and add a real key from platform.openai.com")

    if config.WEBHOOK_SECRET:
        if len(config.WEBHOOK_SECRET) < 24:
            note(WARN, "config",
                 f"WEBHOOK_SECRET is short ({len(config.WEBHOOK_SECRET)} chars)",
                 'generate one: python -c "import secrets;print(secrets.token_urlsafe(32))"')
        elif "change-me" in config.WEBHOOK_SECRET or "dev-" in config.WEBHOOK_SECRET:
            note(WARN, "config", "WEBHOOK_SECRET looks like a development value",
                 "replace it before this is exposed beyond localhost")
        else:
            note(OK, "config", "WEBHOOK_SECRET is set")
    else:
        note(BAD, "config", "WEBHOOK_SECRET is unset — lead delivery is DISABLED",
             "set it here AND to the same value in the CRM's .env. Leads will "
             "queue as failed deliveries until you do")

    if config.WEBHOOK_URL:
        note(OK, "config", f"lead webhook target: {config.WEBHOOK_URL}")
    else:
        note(WARN, "config", "LEAD_WEBHOOK_URL is unset",
             "the CRM can still pull via /api/v1/leads, but push delivery is off")

    if config.ALLOWED_ORIGINS == ["*"]:
        note(WARN, "config", "ALLOWED_ORIGINS is '*' — any site can call this API",
             "set it to the portal's real origin before going live")
    else:
        note(OK, "config", f"CORS restricted to {config.ALLOWED_ORIGINS}")


def check_portals() -> None:
    from portals import registry

    try:
        portals = registry.listing()
    except Exception as exc:  # noqa: BLE001
        note(BAD, "portals", f"cannot read the portal registry: {exc}",
             "run: python -m portals.seed")
        return

    if not portals:
        note(BAD, "portals", "no portals configured", "run: python -m portals.seed")
        return
    note(OK, "portals", f"{len(portals)} active: "
         + ", ".join(f"{p['portal_key']} ({p['field_count']} fields)"
                     for p in portals))

    for portal in portals:
        conf = registry.get(portal["portal_key"])
        if not conf["allowed_domains"]:
            note(WARN, "portals",
                 f"{portal['portal_key']}: no allowed_domains — the widget is "
                 f"localhost-only",
                 "add the production domain before embedding it on the portal")
        if conf["pricing_mode"] == "refer":
            note(OK, "portals",
                 f"{portal['portal_key']}: pricing_mode=refer (safest; no figure "
                 f"can be quoted)")
        else:
            note(WARN, "portals",
                 f"{portal['portal_key']}: pricing_mode={conf['pricing_mode']} — "
                 f"the assistant may state figures",
                 "confirm Riphah has signed off on this mode (scope document s8)")
        if not conf["consent_notice"]:
            note(WARN, "portals", f"{portal['portal_key']}: no consent notice",
                 "required before personal data is accepted (stage 2)")
        if not conf["scoring_rules"]:
            note(WARN, "portals", f"{portal['portal_key']}: no scoring rules; "
                 f"defaults will be used",
                 "get the tiers approved by sales leadership (stage 5 dependency)")


def check_knowledge_base() -> None:
    counts = db.counts()
    if not counts["documents_published"]:
        note(BAD, "knowledge base", "no published documents",
             "run: python -m kb.build")
        return
    note(OK, "knowledge base",
         f"{counts['documents_published']} published documents, "
         f"{counts['chunks']} passages")

    if counts["chunks_embedded"] < counts["chunks"]:
        missing = counts["chunks"] - counts["chunks_embedded"]
        note(BAD, "knowledge base", f"{missing} passages have no embedding — "
             f"retrieval is keyword-only for those",
             "run: python -m kb.build --only embed")
    else:
        note(OK, "knowledge base", f"all {counts['chunks_embedded']} passages embedded")

    from kb.vector_store import STORE

    try:
        size = STORE.reload()
        note(OK if size else BAD, "knowledge base",
             f"vector store loaded {size} live passages",
             "" if size else "check that documents are published and not retired")
    except Exception as exc:  # noqa: BLE001
        note(BAD, "knowledge base", f"vector store failed to load: {exc}",
             "run: python -m kb.build --only embed --rebuild")

    # Placeholder content reaching production is a real risk: it reads as real.
    placeholders = db.scalar(
        "SELECT COUNT(*) FROM kb_documents WHERE retired_at IS NULL "
        "   AND (text LIKE '%Placeholder document%' OR text LIKE '%NOT REAL FIGURES%')"
    )
    if placeholders:
        note(WARN, "knowledge base",
             f"{placeholders} document(s) are the shipped placeholders",
             "replace content/ with Riphah-supplied documents before launch, and "
             "delete the placeholders — retrieval cannot tell them apart")

    if counts["knowledge_gaps_open"]:
        note(OK, "knowledge base",
             f"{counts['knowledge_gaps_open']} unanswered questions logged",
             "review with: python -m kb.build --gaps")


def check_delivery() -> None:
    from leads import delivery

    enabled, reason = delivery.enabled()
    if enabled:
        note(OK, "delivery", "outbound lead delivery is enabled")
    else:
        note(BAD, "delivery", f"delivery disabled: {reason}",
             "leads will be captured and queued, but nothing will reach the CRM")

    pending = db.scalar(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE status = 'pending'")
    failed = db.scalar(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE status = 'failed'")
    if failed:
        last = db.one("SELECT last_error FROM webhook_deliveries "
                      " WHERE status = 'failed' ORDER BY id DESC LIMIT 1")
        note(WARN, "delivery", f"{failed} deliveries gave up permanently",
             f"last error: {(last or {}).get('last_error', '')[:120]} — retry from "
             f"the delivery log once the receiver is fixed")
    if pending:
        note(OK, "delivery", f"{pending} deliveries queued for retry")

    keys = db.query("SELECT label, scopes, revoked_at FROM api_keys")
    live = [k for k in keys if not k["revoked_at"]]
    if live:
        note(OK, "delivery", f"{len(live)} active API key(s): "
             + ", ".join(k["label"] for k in live))
    else:
        note(WARN, "delivery", "no API keys issued — the CRM cannot pull leads",
             "mint one: POST /api/admin/api-keys as an admin")


def check_model() -> None:
    """One live embedding call. Cheap, and it is the check that catches a key that
    is present but revoked, out of quota, or pointed at the wrong project."""
    if not config.has_openai_key():
        note(WARN, "model", "skipped the live check (no key)")
        return
    try:
        from kb import embed

        vector = embed.embed_query("preflight")
        note(OK, "model", f"{config.EMBED_MODEL} responded ({len(vector)} dims)")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        action = "check the key at platform.openai.com"
        if "insufficient_quota" in message or "quota" in message.lower():
            action = "the account is out of quota — top it up"
        elif "invalid_api_key" in message or "Incorrect API key" in message:
            action = "the key is rejected; regenerate it"
        note(BAD, "model", f"{type(exc).__name__}: {message[:160]}", action)


def main() -> int:
    print("Riphah AI Property Assistant — preflight\n")
    db.migrate()

    for check in (check_config, check_portals, check_knowledge_base,
                  check_delivery, check_model):
        try:
            check()
        except Exception as exc:  # noqa: BLE001
            note(BAD, check.__name__, f"check itself failed: "
                 f"{type(exc).__name__}: {exc}")

    area_width = max(len(a) for _, a, _, _ in findings) + 1
    for level, area, message, action in findings:
        marker = {OK: "  ok  ", WARN: "  warn", BAD: "  FAIL"}[level]
        print(f"{marker}  {area:<{area_width}} {message}")
        if action:
            print(f"          {'':<{area_width}} → {action}")

    bad = sum(1 for level, *_ in findings if level == BAD)
    warn = sum(1 for level, *_ in findings if level == WARN)
    print(f"\n{len(findings)} checks · {bad} failing · {warn} warnings")

    if bad:
        print("\nNot ready. Fix the FAIL lines above.")
    elif warn:
        print("\nWill run. The warnings are things to close before launch.")
    else:
        print("\nReady.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
