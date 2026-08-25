"""The Meta lead-ads source: PENDING.

The client's brief listed two lead sources — the chatbot, and Meta (Facebook and
Instagram lead forms) — and said Meta is pending. So this adapter is written,
tested against a captured fixture, and wired into ingest, analytics and the
dashboard. What it does not have is credentials.

**What is done here**

* `normalise()` maps Meta's `field_data` array onto the same `NormalisedLead` the
  chatbot produces, so a Meta lead flows through the identical ingest, dedupe,
  analytics and UI path.
* `FIELD_MAP` translates Meta's form field names to the property portal's field
  keys, including the phrasings Meta's own lead-form templates generate.
* `score()` assigns a tier. Meta forms carry no conversation, so the chatbot's
  engagement and grounding signals do not exist — the tiering is deliberately
  more conservative, and says so.
* `/api/webhooks/meta` exists, verifies `X-Hub-Signature-256`, and handles the
  `GET` subscription handshake.
* `eval/test_meta_fixture.py` runs a real Meta payload shape end to end.

**What is blocked, and on whom**

| Needed | From | Why |
|---|---|---|
| `META_APP_SECRET` | Riphah marketing / agency | verify webhook signatures |
| `META_PAGE_ACCESS_TOKEN` | same | read `/{lead_id}` to fetch full field data |
| `META_VERIFY_TOKEN` | our choice, theirs to enter | subscription handshake |
| `META_PAGE_ID` | same | scope which page's leads we accept |
| Form → field mapping sign-off | Riphah sales | `FIELD_MAP` below is our best guess |

The webhook Meta sends contains only a `leadgen_id`; the field data must then be
fetched from the Graph API with the page token. So without the token, the adapter
can normalise a payload but cannot obtain one — which is why this is credential-
blocked rather than half-built.

Set the four variables and flip `status()` to live; no other file changes.
"""
from __future__ import annotations

from typing import Any

import config
from core import db
from sources.base import NormalisedLead

KEY = "meta"
DISPLAY_NAME = "Meta Lead Ads (Facebook / Instagram)"

GRAPH_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Meta form field names -> property portal field keys.
#
# The left side is what Meta's lead-form builder produces, including its
# auto-generated question-text keys. Marketing teams rename these freely, so this
# map needs sign-off against the actual live forms — it is the single most likely
# thing to be wrong on the first real lead, and unmapped fields are preserved
# rather than dropped so nothing is lost while it is being corrected.
FIELD_MAP: dict[str, str] = {
    # contact
    "full_name": "name", "first_name": "name", "name": "name",
    "email": "email", "phone_number": "phone", "phone": "phone",
    # requirement
    "city": "buyer_location", "city_name": "buyer_location",
    "which_project_are_you_interested_in?": "project",
    "which_project_are_you_interested_in": "project",
    "project": "project",
    "what_is_your_budget?": "budget_max",
    "what_is_your_budget": "budget_max",
    "budget": "budget_max", "budget_range": "budget_max",
    "property_type": "property_type",
    "what_type_of_property_are_you_looking_for?": "property_type",
    "when_are_you_planning_to_buy?": "timeline",
    "when_are_you_planning_to_buy": "timeline",
    "timeline": "timeline",
    "are_you_buying_for_investment_or_personal_use?": "purpose",
    "purpose": "purpose",
    "do_you_need_a_payment_plan?": "payment_pref",
}

# Meta's free-text answers -> the portal's enum options. Meta lead forms allow
# custom answer text per form, so this is best-effort and falls through to
# storing the raw string, which the dashboard flags for confirmation.
VALUE_MAP: dict[str, dict[str, str]] = {
    "purpose": {
        "investment": "investment", "invest": "investment",
        "personal use": "end_use", "personal": "end_use", "own use": "end_use",
        "both": "both",
    },
    "timeline": {
        "immediately": "within_1_month", "1 month": "within_1_month",
        "1-3 months": "within_3_months", "3 months": "within_3_months",
        "3-6 months": "within_6_months", "6 months": "within_6_months",
        "6-12 months": "within_12_months", "1 year": "within_12_months",
        "just looking": "just_exploring", "just exploring": "just_exploring",
    },
    "property_type": {
        "medical suite": "medical_suite", "clinic": "medical_suite",
        "apartment": "apartment", "flat": "apartment",
        "shop": "commercial_shop", "commercial": "commercial_shop",
        "office": "office", "plot": "plot",
    },
}


def _map_value(field_key: str, raw: str) -> str:
    table = VALUE_MAP.get(field_key)
    if not table:
        return raw
    return table.get(raw.strip().lower(), raw)


class MetaSource:
    key = KEY
    display_name = DISPLAY_NAME

    # ------------------------------------------------------------- normalising

    def normalise(self, raw: dict[str, Any]) -> NormalisedLead:
        """Map a Graph API lead object onto the CRM model.

        Expected shape (as returned by `GET /{leadgen_id}`):

            {"id": "...", "created_time": "2026-08-03T09:41:12+0000",
             "form_id": "...", "campaign_name": "...", "ad_name": "...",
             "field_data": [{"name": "email", "values": ["a@b.com"]}, ...]}
        """
        fields: dict[str, Any] = {}
        unmapped: dict[str, Any] = {}
        contact = {"name": None, "email": None, "phone": None}

        for entry in raw.get("field_data") or []:
            meta_name = str(entry.get("name") or "").strip().lower().replace(" ", "_")
            values = entry.get("values") or []
            value = str(values[0]).strip() if values else ""
            if not value:
                continue

            mapped = FIELD_MAP.get(meta_name)
            if mapped in ("name", "email", "phone"):
                contact[mapped] = value
            elif mapped:
                fields[mapped] = _map_value(mapped, value)
            else:
                # Never discarded. An unmapped question is a gap in FIELD_MAP, and
                # keeping the answer means the map can be corrected retroactively
                # instead of the data being re-collected.
                unmapped[meta_name] = value

        if unmapped:
            fields["_unmapped"] = unmapped

        # Money arrives as free text ("2.5 crore", "PKR 50 lakh"). It is left as
        # the visitor wrote it and flagged, rather than parsed here — the chatbot
        # owns money parsing, and a second implementation would be a second thing
        # to keep in sync.
        needs_confirmation = [k for k in ("budget_max",) if k in fields]
        needs_confirmation += [k for k in fields if k == "_unmapped"]

        qualification, score = self.score(contact, fields)

        return NormalisedLead(
            source_key=KEY,
            external_id=str(raw.get("id") or ""),
            # Meta leads are not portal-scoped; attributed to the property portal
            # because that is what the ads run for.
            portal=raw.get("portal") or "riphah-property",
            name=contact["name"],
            email=contact["email"],
            phone=contact["phone"],
            qualification=qualification,
            score=score,
            fields=fields,
            needs_confirmation=needs_confirmation,
            utm_source="facebook",
            utm_medium="paid_social",
            utm_campaign=raw.get("campaign_name") or raw.get("ad_name"),
            referrer="meta_lead_ad",
            channel="form",
            # Meta collects consent inside the form itself, and the lead cannot be
            # submitted without it — so a lead arriving at all implies consent was
            # given. The version is Meta's form id, which is the closest thing to
            # "which notice did they see".
            consent_given=True,
            consent_version=f"meta_form:{raw.get('form_id') or 'unknown'}",
            captured_at=self._iso(raw.get("created_time")),
            raw_payload=raw,
        )

    @staticmethod
    def _iso(value: Any) -> str | None:
        """Meta sends `2026-08-03T09:41:12+0000`; the CRM stores `+00:00`."""
        if not value:
            return None
        text = str(value)
        if len(text) >= 5 and (text[-5] in "+-") and ":" not in text[-5:]:
            text = f"{text[:-2]}:{text[-2:]}"
        return text

    @staticmethod
    def score(contact: dict[str, Any], fields: dict[str, Any]) -> tuple[str, int]:
        """Tier a Meta lead.

        Deliberately more conservative than the chatbot's scorer, because the
        signals it relies on do not exist here. A form submission has no
        engagement depth, no evidence the person read anything, and no
        conversational context in which a stated budget can be judged. Paid-social
        form fill rates are also high relative to intent.

        So the ceiling is Warm unless the lead has *both* a phone number and a
        near-term timeline. Marking these Hot on arrival would fill the priority
        queue with form fills and train the sales team to ignore the tier.
        """
        points = 0
        if contact.get("phone"):
            points += 25
        if contact.get("email"):
            points += 15
        if contact.get("name"):
            points += 5
        points += min(len([k for k in fields if not k.startswith("_")]) * 8, 32)

        timeline = str(fields.get("timeline") or "")
        near_term = timeline in ("within_1_month", "within_3_months")
        if near_term:
            points += 15

        if not (contact.get("phone") or contact.get("email")):
            return "cold", min(points, 100)
        if contact.get("phone") and near_term and points >= 60:
            return "hot", min(points, 100)
        if points >= 35:
            return "warm", min(points, 100)
        return "cold", min(points, 100)

    # ---------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        missing = [
            name for name, value in (
                ("META_APP_SECRET", config.META_APP_SECRET),
                ("META_PAGE_ACCESS_TOKEN", config.META_PAGE_ACCESS_TOKEN),
                ("META_VERIFY_TOKEN", config.META_VERIFY_TOKEN),
                ("META_PAGE_ID", config.META_PAGE_ID),
            ) if not value
        ]
        row = db.one("SELECT * FROM sources WHERE key = ?", (KEY,))
        return {
            "key": KEY,
            "display_name": DISPLAY_NAME,
            "status": "pending" if missing else "live",
            "detail": (
                "Adapter, field mapping and webhook are built and tested against a "
                f"fixture. Blocked on credentials from Riphah marketing: "
                f"{', '.join(missing)}."
                if missing else
                "Connected. Meta leads flow through the same ingest path as the "
                "chatbot."
            ),
            "missing_config": missing,
            "blocked_on": "Riphah marketing / agency" if missing else None,
            "field_map_needs_signoff": bool(missing),
            "webhook_path": "/api/webhooks/meta",
            "last_sync_at": row.get("last_sync_at") if row else None,
            "leads_received": row.get("leads_received") if row else 0,
        }

    # ------------------------------------------------------------------ graph

    def fetch_lead(self, leadgen_id: str) -> dict[str, Any] | None:
        """Fetch a lead's field data from the Graph API.

        Meta's webhook carries only a `leadgen_id`; the answers must be fetched
        with the page token. This is the exact call that cannot run without
        `META_PAGE_ACCESS_TOKEN`, and the reason this source is credential-blocked
        rather than merely unconfigured.
        """
        import httpx

        if not config.META_PAGE_ACCESS_TOKEN:
            return None
        try:
            response = httpx.get(
                f"{GRAPH_BASE}/{leadgen_id}",
                params={
                    "access_token": config.META_PAGE_ACCESS_TOKEN,
                    "fields": "id,created_time,form_id,campaign_name,ad_name,"
                              "adset_name,platform,field_data",
                },
                timeout=20,
            )
            if response.status_code != 200:
                print(f"[meta] graph {response.status_code}: {response.text[:200]}")
                return None
            return response.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[meta] graph fetch failed: {exc}")
            return None

    def verify_subscription(self, mode: str | None, token: str | None,
                            challenge: str | None) -> str | None:
        """Meta's `GET` subscription handshake. Returns the challenge to echo."""
        if not config.META_VERIFY_TOKEN:
            return None
        if mode == "subscribe" and token == config.META_VERIFY_TOKEN:
            return challenge
        return None

    def extract_leadgen_ids(self, body: dict[str, Any]) -> list[str]:
        """Pull `leadgen_id` values out of a `page` webhook envelope.

        Meta batches: one POST can carry several entries, each with several
        changes. Filtered by page id when one is configured, so a shared app cannot
        inject leads from someone else's page.
        """
        ids: list[str] = []
        for entry in body.get("entry") or []:
            if config.META_PAGE_ID and str(entry.get("id")) != str(config.META_PAGE_ID):
                continue
            for change in entry.get("changes") or []:
                if change.get("field") != "leadgen":
                    continue
                value = change.get("value") or {}
                leadgen_id = value.get("leadgen_id")
                if leadgen_id:
                    ids.append(str(leadgen_id))
        return ids


SOURCE = MetaSource()
