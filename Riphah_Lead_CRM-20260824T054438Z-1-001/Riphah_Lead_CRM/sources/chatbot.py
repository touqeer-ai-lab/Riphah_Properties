"""The chatbot source: live.

Two ingestion paths, both landing in the same `normalise()`:

**Push** — `lead.created` / `lead.updated` webhooks, HMAC-verified, handled by
`/api/webhooks/riphah-chatbot`. Low latency: a hot lead is in the CRM within
seconds of the visitor giving their number.

**Pull** — a periodic `GET /api/v1/leads?since=` reconciliation. This is not
redundancy for its own sake. A webhook can be lost while this service is
restarting or mid-deploy, and the CRM has no way to know about a delivery it never
received. Polling with an overlap window closes that gap, and because ingestion is
idempotent on `(source_key, external_id)`, a lead arriving by both paths is one
row.

The overlap matters: the pull asks for leads since *the last sync minus fifteen
minutes*, not since the last sync. Two services with slightly different clocks and
a lead created during the handover would otherwise fall between the two windows
and never arrive at all.
"""
from __future__ import annotations

from typing import Any

import config
from core import db
from sources.base import NormalisedLead, clean_qualification, clean_status

KEY = "chatbot"
DISPLAY_NAME = "AI Property Assistant"


class ChatbotSource:
    key = KEY
    display_name = DISPLAY_NAME

    # ------------------------------------------------------------- normalising

    def normalise(self, raw: dict[str, Any]) -> NormalisedLead:
        """Map the chatbot's lead payload (scope document s9.3) onto the CRM model.

        Written defensively — every access is a `.get()` with a default. This is a
        cross-service boundary, and a field added or renamed upstream must degrade
        one column rather than drop the lead.
        """
        contact = raw.get("contact") or {}
        source = raw.get("source") or {}
        consent = raw.get("consent") or {}
        portal_fields = raw.get("portal_fields") or {}

        return NormalisedLead(
            source_key=KEY,
            # The chatbot's `lead_id` is its human-readable ref ("LD-2026-00041").
            external_id=str(raw.get("lead_id") or raw.get("lead_ref") or ""),
            portal=raw.get("portal"),
            name=contact.get("name"),
            email=contact.get("email"),
            phone=contact.get("phone"),
            qualification=clean_qualification(raw.get("qualification")),
            score=int(raw.get("score") or 0),
            # Deliberately NOT taken from the payload on update. The CRM owns the
            # sales process; if a consultant marked a lead 'contacted' here, an
            # inbound `lead.updated` must not reset it to 'new'. See ingest.py,
            # which only applies this on first insert.
            status=clean_status(raw.get("business_status")),
            assigned_owner=raw.get("assigned_owner"),
            language=raw.get("language"),
            fields={k: v for k, v in portal_fields.items() if v not in (None, "")},
            needs_confirmation=list(raw.get("fields_needing_confirmation") or []),
            utm_source=source.get("utm_source"),
            utm_medium=source.get("utm_medium"),
            utm_campaign=source.get("utm_campaign"),
            referrer=source.get("referrer"),
            device=source.get("device"),
            region=source.get("region"),
            landing_url=source.get("landing_url"),
            channel=source.get("channel"),
            consent_given=bool(consent.get("given")),
            consent_version=consent.get("version"),
            contact_source=contact.get("source"),
            marketing_opt_in=bool(consent.get("marketing_opt_in")),
            has_account=bool(contact.get("has_account")),
            message_count=int(raw.get("message_count") or 0),
            session_count=int(raw.get("session_count") or 0),
            transcript_url=raw.get("transcript_url"),
            captured_at=raw.get("captured_at"),
            source_updated_at=raw.get("updated_at"),
            raw_payload=raw,
        )

    # ---------------------------------------------------------------- status

    def probe(self) -> tuple[bool, str | None]:
        """Actually call the chatbot with the configured key.

        `status()` used to report `pull_enabled: true` on the strength of the key
        being *present*. That is not the same as the key *working*, and the gap is
        not academic: rebuilding the chatbot's database wipes its `api_keys` table,
        the CRM keeps a key that no longer exists, and every pull silently 401s
        while the dashboard reports the source as healthy. Leads still arrive by
        webhook, so nothing looks broken until you ask why a backfill found
        nothing.

        One cheap request settles it, so it is worth making.
        """
        import httpx

        if not config.CHATBOT_API_KEY:
            return False, "CHATBOT_API_KEY is not set"
        try:
            with httpx.Client(base_url=config.CHATBOT_BASE_URL, timeout=8,
                              headers={"X-API-Key": config.CHATBOT_API_KEY}) as client:
                response = client.get("/api/v1/leads", params={"limit": 1})
        except Exception as exc:  # noqa: BLE001
            return False, (f"cannot reach {config.CHATBOT_BASE_URL} "
                           f"({type(exc).__name__}) — is the assistant running?")
        if response.status_code == 401:
            return False, ("the assistant rejected CHATBOT_API_KEY. Mint a new one "
                           "there and update this .env — a key does not survive a "
                           "rebuild of the assistant's database")
        if response.status_code == 403:
            return False, ("the key is valid but lacks the leads:read scope")
        if response.status_code >= 400:
            return False, f"the assistant returned HTTP {response.status_code}"
        return True, None

    def status(self, *, probe: bool = True) -> dict[str, Any]:
        row = db.one("SELECT * FROM sources WHERE key = ?", (KEY,))
        signed = bool(config.WEBHOOK_SECRET)

        problems: list[str] = []
        if not signed:
            problems.append(
                "WEBHOOK_SECRET is not set — inbound webhooks will be rejected"
            )

        pull_ok, pull_problem = (self.probe() if probe
                                 else (bool(config.CHATBOT_API_KEY), None))
        if pull_problem:
            problems.append(f"pull reconciliation is down: {pull_problem}")

        return {
            "key": KEY,
            "display_name": DISPLAY_NAME,
            # Degraded is its own state. Push working while pull is broken is not
            # "live" — a lost webhook would never be recovered — but it is not
            # "pending" either, because leads are still arriving.
            "status": ("live" if signed and pull_ok
                       else "degraded" if signed or pull_ok
                       else "pending"),
            "push_enabled": signed,
            "pull_enabled": pull_ok and config.PULL_ENABLED,
            "pull_verified": pull_ok,
            "detail": "; ".join(problems) or "Push and pull both verified.",
            "base_url": config.CHATBOT_BASE_URL,
            "last_sync_at": row.get("last_sync_at") if row else None,
            "leads_received": row.get("leads_received") if row else 0,
        }

    # ------------------------------------------------------------------- pull

    def fetch_since(self, since: str | None, *, limit: int = 200) -> dict[str, Any]:
        """Pull leads from the chatbot's `/api/v1/leads`, following the cursor.

        Requests `full=true` so each row is the same complete payload a webhook
        would deliver — which is what lets both paths share `normalise()` and
        guarantees the two cannot drift apart.
        """
        import httpx

        if not config.CHATBOT_API_KEY:
            return {"ok": False, "reason": "CHATBOT_API_KEY is not configured",
                    "leads": []}

        params: dict[str, Any] = {"limit": min(limit, 200), "full": "true"}
        if since:
            params["since"] = since

        collected: list[dict[str, Any]] = []
        cursor: int | None = None
        pages = 0
        try:
            with httpx.Client(
                base_url=config.CHATBOT_BASE_URL, timeout=30,
                headers={"X-API-Key": config.CHATBOT_API_KEY},
            ) as client:
                # Bounded at 20 pages so a misconfigured `since` cannot turn a
                # background sync into an unbounded backfill.
                while pages < 20:
                    page_params = dict(params)
                    if cursor:
                        page_params["cursor"] = cursor
                    response = client.get("/api/v1/leads", params=page_params)
                    if response.status_code == 401:
                        return {"ok": False,
                                "reason": "chatbot rejected CHATBOT_API_KEY",
                                "leads": []}
                    response.raise_for_status()
                    body = response.json()
                    collected.extend(body.get("leads") or [])
                    cursor = body.get("next_cursor")
                    pages += 1
                    if not cursor:
                        break
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}",
                    "leads": collected}

        return {"ok": True, "leads": collected, "pages": pages,
                "truncated": bool(cursor)}

    def fetch_transcript(self, session_id: str) -> dict[str, Any] | None:
        """Fetch a conversation transcript on demand.

        Lazy rather than pushed with the lead: most leads are never opened, and a
        transcript is the largest thing in the payload. Cached on first fetch — a
        consultant preparing for a call reads it several times, and the chatbot may
        retire an old session before the CRM is finished with it.
        """
        import httpx

        if not config.CHATBOT_API_KEY:
            return None
        try:
            with httpx.Client(
                base_url=config.CHATBOT_BASE_URL, timeout=30,
                headers={"X-API-Key": config.CHATBOT_API_KEY},
            ) as client:
                response = client.get(f"/api/v1/chats/{session_id}")
                if response.status_code != 200:
                    return None
                return response.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[chatbot] transcript fetch failed: {exc}")
            return None

    def fetch_field_schema(self, portal: str) -> dict[str, Any] | None:
        """Read the portal's field schema so the UI can label captured fields.

        Reading the schema instead of hard-coding labels is the reason a new portal
        field shows up in this dashboard with its proper human label and no change
        on the CRM side.
        """
        import httpx

        if not config.CHATBOT_API_KEY:
            return None
        try:
            with httpx.Client(
                base_url=config.CHATBOT_BASE_URL, timeout=15,
                headers={"X-API-Key": config.CHATBOT_API_KEY},
            ) as client:
                response = client.get(f"/api/v1/portals/{portal}/fields")
                return response.json() if response.status_code == 200 else None
        except Exception:  # noqa: BLE001
            return None

    def push_status(self, external_id: str, *, status: str | None = None,
                    assigned_owner: str | None = None) -> bool:
        """Write the sales status back to the chatbot.

        Keeps the two systems agreeing about where a lead is in the process, which
        matters because the chatbot's dashboard and this one are both read by
        people. Best-effort: a failure is logged and does not block the local
        update, since the CRM is the system of record for status.
        """
        import httpx

        if not config.CHATBOT_API_KEY:
            return False
        payload: dict[str, Any] = {}
        if status:
            payload["status"] = status
        if assigned_owner is not None:
            payload["assigned_owner"] = assigned_owner
        if not payload:
            return False
        try:
            with httpx.Client(
                base_url=config.CHATBOT_BASE_URL, timeout=15,
                headers={"X-API-Key": config.CHATBOT_API_KEY},
            ) as client:
                response = client.patch(f"/api/v1/leads/{external_id}", json=payload)
                return response.status_code == 200
        except Exception as exc:  # noqa: BLE001
            print(f"[chatbot] status write-back failed: {exc}")
            return False


SOURCE = ChatbotSource()
