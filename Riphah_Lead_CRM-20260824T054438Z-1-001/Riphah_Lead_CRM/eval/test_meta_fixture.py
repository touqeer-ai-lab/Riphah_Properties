"""Prove the Meta adapter works, without Meta credentials.

`sources/meta.py` claims to be "built and tested against a fixture, blocked only
on credentials". This is that test. It runs a real Meta Graph API lead payload
shape through `normalise()` and asserts the mapping, the value translation, the
scoring, and the webhook envelope parsing.

What it deliberately does not test: `fetch_lead()`, which needs a live page access
token. That is the one thing genuinely blocked, and the boundary between "coded and
verified" and "needs the client" is exactly here.

    python eval/test_meta_fixture.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import meta  # noqa: E402

PASSED, FAILED = [], []


def check(label: str, condition: bool, detail: object = "") -> None:
    (PASSED if condition else FAILED).append(label)
    print(f"  {'ok  ' if condition else 'FAIL'} {label}"
          + (f"  {detail}" if not condition else ""))


# A lead as Meta's Graph API returns it. Field names are the ones Meta's own
# lead-form builder generates from question text.
FIXTURE = {
    "id": "1234567890123456",
    "created_time": "2026-08-02T14:22:31+0000",
    "form_id": "998877665544332",
    "campaign_name": "medical-suites-august",
    "ad_name": "RMC Suites — Carousel A",
    "platform": "ig",
    "field_data": [
        {"name": "full_name", "values": ["Dr. Bilal Ahmed"]},
        {"name": "email", "values": ["bilal.ahmed@clinic.pk"]},
        {"name": "phone_number", "values": ["+92 321 4567890"]},
        {"name": "city", "values": ["Lahore"]},
        {"name": "which_project_are_you_interested_in?",
         "values": ["Riphah Medical City"]},
        {"name": "what_is_your_budget?", "values": ["2 to 3 crore"]},
        {"name": "when_are_you_planning_to_buy?", "values": ["1-3 months"]},
        {"name": "are_you_buying_for_investment_or_personal_use?",
         "values": ["Personal use"]},
        {"name": "what_type_of_property_are_you_looking_for?",
         "values": ["Medical suite"]},
        # A question nobody mapped. Must be preserved, not dropped.
        {"name": "how_did_you_hear_about_us?", "values": ["Instagram ad"]},
        # Empty answer — must not create a field.
        {"name": "timeline", "values": []},
    ],
}

# The webhook envelope Meta POSTs. Carries only a leadgen_id.
ENVELOPE = {
    "object": "page",
    "entry": [{
        "id": "111222333444555",
        "time": 1785000000,
        "changes": [{
            "field": "leadgen",
            "value": {"leadgen_id": "1234567890123456",
                      "page_id": "111222333444555",
                      "form_id": "998877665544332",
                      "created_time": 1785000000},
        }],
    }],
}


def main() -> int:
    print("meta adapter — normalise()")
    lead = meta.SOURCE.normalise(FIXTURE)

    check("source key", lead.source_key == "meta", lead.source_key)
    check("external id from Meta lead id",
          lead.external_id == "1234567890123456", lead.external_id)
    check("name mapped", lead.name == "Dr. Bilal Ahmed", lead.name)
    check("email mapped", lead.email == "bilal.ahmed@clinic.pk", lead.email)
    check("phone mapped verbatim", lead.phone == "+92 321 4567890", lead.phone)
    check("attributed to the property portal", lead.portal == "riphah-property",
          lead.portal)

    print("\nfield mapping")
    check("project mapped", lead.fields.get("project") == "Riphah Medical City",
          lead.fields.get("project"))
    check("city -> buyer_location", lead.fields.get("buyer_location") == "Lahore",
          lead.fields.get("buyer_location"))
    check("budget kept as the visitor wrote it",
          lead.fields.get("budget_max") == "2 to 3 crore",
          lead.fields.get("budget_max"))
    check("timeline value translated to the portal enum",
          lead.fields.get("timeline") == "within_3_months",
          lead.fields.get("timeline"))
    check("purpose value translated",
          lead.fields.get("purpose") == "end_use", lead.fields.get("purpose"))
    check("property type translated",
          lead.fields.get("property_type") == "medical_suite",
          lead.fields.get("property_type"))
    check("empty answer produced no field", "timeline" in lead.fields
          and lead.fields["timeline"] == "within_3_months")

    print("\nunmapped questions are preserved, not dropped")
    unmapped = lead.fields.get("_unmapped") or {}
    check("unmapped question kept",
          unmapped.get("how_did_you_hear_about_us?") == "Instagram ad", unmapped)
    check("unmapped flagged for confirmation",
          "_unmapped" in lead.needs_confirmation, lead.needs_confirmation)
    check("free-text budget flagged for confirmation",
          "budget_max" in lead.needs_confirmation, lead.needs_confirmation)

    print("\nattribution and consent")
    check("utm source is facebook", lead.utm_source == "facebook")
    check("campaign name carried",
          lead.utm_campaign == "medical-suites-august", lead.utm_campaign)
    check("channel is form", lead.channel == "form", lead.channel)
    check("consent implied by form submission", lead.consent_given is True)
    check("consent version records the form id",
          lead.consent_version == "meta_form:998877665544332", lead.consent_version)
    check("created_time normalised to +00:00 offset",
          lead.captured_at == "2026-08-02T14:22:31+00:00", lead.captured_at)

    print("\nscoring — conservative by design (no conversation signal)")
    check("tiered, not defaulted", lead.qualification in ("hot", "warm", "cold"),
          lead.qualification)
    check("score within 0-100", 0 <= lead.score <= 100, lead.score)
    # A phone plus a near-term timeline plus several fields is the only route to hot.
    check("full-detail near-term lead reaches hot",
          lead.qualification == "hot", f"{lead.qualification} @ {lead.score}")

    thin, thin_score = meta.SOURCE.score({"email": "a@b.com"}, {})
    check("email-only lead is not hot", thin != "hot", thin)
    no_contact, _ = meta.SOURCE.score({}, {"project": "x", "timeline": "within_1_month"})
    check("no contact route is cold", no_contact == "cold", no_contact)
    browsing, _ = meta.SOURCE.score(
        {"phone": "+923214567890"}, {"timeline": "just_exploring"})
    check("phone but just exploring is not hot", browsing != "hot", browsing)

    print("\nwebhook envelope")
    ids = meta.SOURCE.extract_leadgen_ids(ENVELOPE)
    check("leadgen id extracted", ids == ["1234567890123456"], ids)
    check("non-leadgen changes ignored",
          meta.SOURCE.extract_leadgen_ids(
              {"entry": [{"id": "1", "changes": [{"field": "feed", "value": {}}]}]}
          ) == [])
    check("empty envelope is safe", meta.SOURCE.extract_leadgen_ids({}) == [])

    print("\nstatus reporting")
    status = meta.SOURCE.status()
    check("reports pending without credentials", status["status"] == "pending",
          status["status"])
    check("names every missing variable", len(status["missing_config"]) == 4,
          status["missing_config"])
    check("names who it is blocked on", bool(status["blocked_on"]),
          status["blocked_on"])
    check("flags that the field map needs sign-off",
          status["field_map_needs_signoff"] is True)

    print(f"\n{'=' * 56}\npassed {len(PASSED)}   failed {len(FAILED)}")
    if FAILED:
        for label in FAILED:
            print(f"  - {label}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
