"""Seed the two portals from the scope document (s5) and their scoring rules.

The property portal is what launches. The admission portal is seeded alongside it
for one reason: it is the proof that the multi-portal claim is real. Both portals
run through the same extractor, scorer, dashboard and API — the diff between them
is entirely in these dicts.

Idempotent. Re-running updates copy and fields without touching captured leads,
so this doubles as the way to edit greeting text and prompt hints in git.
"""
from __future__ import annotations

from portals import registry

# --------------------------------------------------------------------- property

PROPERTY_PORTAL = {
    "portal_key": "riphah-property",
    "display_name": "Riphah Properties",
    "persona": (
        "a property consultant for Riphah Properties, working on Riphah Medical "
        "City and the DHA Business District development"
    ),
    "greeting": (
        "Assalam-o-Alaikum — welcome to Riphah Properties. Ask me anything about "
        "Riphah Medical City or DHA Business District: unit types, locations, "
        "payment plans, or handover. Type here, or switch to the voice agent "
        "to just talk."
    ),
    "languages": ["en", "ur"],
    "knowledge_scope": ["riphah-medical-city", "dha-business-district"],
    # Empty in the seed on purpose: the widget stays localhost-only until Riphah
    # confirms the production domain (a section 13 open item).
    "allowed_domains": [],
    "consent_notice": (
        "This assistant records your conversation and any contact details you "
        "share so a Riphah sales consultant can follow up. Nothing is shared "
        "outside Riphah Properties. You can ask us to delete your record at any "
        "time."
    ),
    "consent_version": "v1",
    # Sign-in required before the chat opens, per the client's decision.
    #
    # The trade-off, recorded so it can be revisited with evidence rather than
    # from memory: a gate means every captured lead already has a verified name,
    # email and phone, so contact quality goes up and `contact_source` is always
    # `account`. It also turns away visitors who only wanted one answer, and those
    # are a real share of traffic — the scope document's own s2.1 correction is
    # about not putting friction in front of the visitor's question.
    #
    # Flip to False to run open, or set it per portal. Nothing else changes.
    "require_auth": True,
    # 'refer' until Riphah picks a mode (dependency table, stage 4). Until then
    # the assistant will not put a number on a unit — which is the correct
    # default for the one thing in this build that carries legal exposure.
    "pricing_mode": "refer",
    "branding": {
        "accent": "#0f5c8c",
        "accent_soft": "#e6f0f7",
        "title": "Riphah Properties",
        "subtitle": "Property assistant",
    },
}

# Fields are the property column of the s5 table, plus the ask-order and copy
# that the reference build lacked. `sort_order` matters: the assistant asks at
# most one question per turn, so this list is effectively the qualification
# script, cheapest and least intrusive question first.
PROPERTY_FIELDS = [
    {
        "field_key": "project", "label": "Project of interest", "field_type": "enum",
        "options": ["Riphah Medical City", "DHA Business District", "Undecided"],
        "required": True, "sort_order": 10,
        "prompt_hint": "Ask which project they're looking at — Riphah Medical City "
                       "or DHA Business District.",
        "extract_hint": "Map any mention of 'medical city', 'RMC', or medical suites "
                        "to 'Riphah Medical City'. Map 'DHA', 'business district', "
                        "or 'commercial' to 'DHA Business District'.",
    },
    {
        "field_key": "purpose", "label": "End use or investment", "field_type": "enum",
        "options": ["end_use", "investment", "both", "unsure"],
        "required": True, "sort_order": 20,
        "prompt_hint": "Ask whether this is for their own use or as an investment. "
                       "Frame it as helping you show the right units.",
        "extract_hint": "'for my clinic', 'to live in', 'for myself' => end_use. "
                        "'rental', 'resale', 'returns', 'portfolio' => investment.",
    },
    {
        "field_key": "property_type", "label": "Property type", "field_type": "enum",
        "options": ["medical_suite", "apartment", "commercial_shop", "office",
                    "plot", "other"],
        "required": True, "sort_order": 30,
        "prompt_hint": "Ask what kind of unit they have in mind.",
        "extract_hint": "'clinic', 'consulting room', 'medical suite' => "
                        "medical_suite. 'flat' => apartment. 'shop', 'retail' => "
                        "commercial_shop.",
    },
    {
        "field_key": "unit_size", "label": "Unit size", "field_type": "text",
        "required": False, "sort_order": 40,
        "prompt_hint": "Ask roughly what size they need, in square feet or beds.",
        "extract_hint": "Keep the visitor's own unit: '600 sq ft', '2 bed', "
                        "'1 kanal'. Do not convert.",
    },
    {
        "field_key": "budget_max", "label": "Budget ceiling", "field_type": "money",
        "required": True, "sort_order": 50,
        # Budget is the highest-value field and the one people most resist. Asked
        # as a range, late, and never before the visitor's own question has been
        # answered.
        "prompt_hint": "Ask for a budget range rather than an exact figure, and "
                       "only after you have answered what they asked. Never make "
                       "it a precondition for information.",
        # Voice transcripts spell numbers out — "two crore", not "2 crore" — so
        # the digit examples alone left every spoken budget uncaptured.
        "extract_hint": "Pakistani numbering: 1 lakh = 100000, 1 crore = 10000000, "
                        "1 arab = 1000000000. '2.5 crore' => 25000000. '50 lac' => "
                        "5000000. Numbers spelled out in words count the same, "
                        "because voice transcripts are written that way: 'two "
                        "crore' => 20000000, 'one and a half crore' => 15000000, "
                        "'fifty lakh' => 5000000, 'two and a half' after a crore "
                        "figure => 25000000. Hedges do not block capture — "
                        "'around', 'roughly', 'up to', 'maybe' still give a "
                        "figure. If they give a range, record the upper bound. "
                        "Assume PKR unless they say USD.",
    },
    {
        "field_key": "payment_pref", "label": "Payment preference", "field_type": "enum",
        "options": ["full_payment", "installments_2y", "installments_3y",
                    "installments_4y_plus", "bank_finance", "undecided"],
        "required": False, "sort_order": 60,
        "prompt_hint": "Ask whether they'd prefer a lump sum or an installment plan.",
        "extract_hint": "'cash', 'one go', 'full' => full_payment. Map a stated "
                        "number of years to the matching installments option. "
                        "'mortgage', 'loan', 'bank' => bank_finance.",
    },
    {
        "field_key": "timeline", "label": "Purchase timeline", "field_type": "enum",
        "options": ["within_1_month", "within_3_months", "within_6_months",
                    "within_12_months", "just_exploring"],
        "required": True, "sort_order": 25,
        # Deliberately early (25, between project and purpose): timeline is the
        # single strongest scoring input and it is socially easy to ask.
        "prompt_hint": "Ask when they're hoping to buy. Easy question, high value "
                       "— ask it early.",
        "extract_hint": "'immediately', 'this month', 'ASAP' => within_1_month. "
                        "'just looking', 'browsing', 'next year maybe' => "
                        "just_exploring.",
    },
    {
        "field_key": "buyer_location", "label": "Buyer location", "field_type": "text",
        "required": False, "sort_order": 70,
        "prompt_hint": "Ask where they're based — it decides whether a site visit "
                       "or a video walkthrough is the right next step.",
        "extract_hint": "City and country if given: 'Islamabad, PK', 'Dubai, AE'. "
                        "An overseas location is commercially significant — record it.",
    },
    {
        "field_key": "is_overseas", "label": "Overseas buyer", "field_type": "bool",
        "required": False, "sort_order": 80,
        "prompt_hint": "Do not ask this directly. Infer it from where they say "
                       "they're based.",
        "extract_hint": "True only if they state a location outside Pakistan, or "
                        "say they are an overseas Pakistani.",
    },
]

# Scoring is configuration (spec s8: tunable without a deploy). Read by
# leads/scoring.py. Weights sum to well over 100; the tier thresholds do the
# real work, and having headroom means a strong lead missing one field still
# clears 'hot'.
PROPERTY_SCORING = {
    "thresholds": {"hot": 70, "warm": 35},
    "weights": {
        "has_email": 12,
        "has_phone": 18,          # a phone number is worth more than an email to a
                                  # property sales team that works by calling
        "has_name": 5,
        "required_fields_complete": 20,   # pro-rated across the required set
        "budget_in_range": 15,
        "timeline": {
            "within_1_month": 22,
            "within_3_months": 16,
            "within_6_months": 8,
            "within_12_months": 3,
            "just_exploring": 0,
        },
        "purpose": {"end_use": 8, "investment": 8, "both": 8, "unsure": 0},
        "payment_pref": {"full_payment": 8, "bank_finance": 3},
        "is_overseas": 4,
        "engagement_per_turn": 2,
        "engagement_cap": 12,
    },
    # Used for the budget_in_range weight. Confirmed price bands are a client
    # dependency; these are placeholders and are labelled as such in the score
    # detail so nobody mistakes them for Riphah figures.
    "budget_range": {"currency": "PKR", "min": 4000000, "max": 250000000,
                     "provisional": True},
    # A hot lead must be contactable — no phone or email means no callback is
    # possible, whatever else the conversation contained.
    "hot_requires_contact": True,
}

# -------------------------------------------------------------------- admission

# Second portal, from the same s5 table. Present to demonstrate that the engine
# is portal-agnostic; not launched in phase 1.
ADMISSION_PORTAL = {
    "portal_key": "riphah-admission",
    "display_name": "Riphah Admissions",
    "persona": (
        "an admissions adviser for Riphah International University, helping "
        "prospective students choose a programme and campus"
    ),
    "greeting": (
        "Assalam-o-Alaikum — Riphah International University admissions. Ask me "
        "about programmes, campuses, eligibility or intake dates."
    ),
    "languages": ["en", "ur"],
    "knowledge_scope": ["riphah-admissions"],
    "allowed_domains": [],
    "consent_notice": (
        "This assistant records your conversation and any contact details you "
        "share so the admissions office can follow up."
    ),
    "pricing_mode": "refer",
    "branding": {"accent": "#155e4a", "accent_soft": "#e6f2ee",
                 "title": "Riphah Admissions", "subtitle": "Admissions assistant"},
}

ADMISSION_FIELDS = [
    {"field_key": "programme_of_interest", "label": "Programme of interest",
     "field_type": "text", "required": True, "sort_order": 10,
     "prompt_hint": "Ask which programme they're interested in.",
     "extract_hint": "Keep the programme name as written: 'MBBS', 'BS Computer "
                     "Science', 'Pharm-D'. Do not expand abbreviations."},
    {"field_key": "intake_session", "label": "Intake session", "field_type": "enum",
     "options": ["Fall 2026", "Spring 2027", "Fall 2027", "undecided"],
     "required": True, "sort_order": 20,
     "prompt_hint": "Ask which intake they're applying for."},
    {"field_key": "preferred_campus", "label": "Preferred campus", "field_type": "enum",
     "options": ["I-14 Islamabad", "G-7 City Islamabad", "Gulberg Green Islamabad",
                 "Al-Mizan Rawalpindi", "Raiwind Lahore", "Gulberg Lahore",
                 "Malakand", "undecided"],
     "required": True, "sort_order": 30,
     "prompt_hint": "Ask which campus suits them."},
    {"field_key": "city", "label": "City", "field_type": "text",
     "required": False, "sort_order": 40,
     "prompt_hint": "Ask where they're based."},
    {"field_key": "last_qualification", "label": "Last qualification",
     "field_type": "text", "required": True, "sort_order": 50,
     "prompt_hint": "Ask what they last studied and their marks or grade — it "
                    "decides eligibility.",
     "extract_hint": "Capture both qualification and result if given: "
                     "'FSc Pre-Medical, 850/1100'."},
    {"field_key": "fee_budget", "label": "Fee budget", "field_type": "money",
     "required": False, "sort_order": 60,
     "extract_hint": "Pakistani numbering: 1 lakh = 100000, 1 crore = 10000000. "
                     "Words count as digits — voice transcripts spell numbers "
                     "out: 'five lakh' => 500000, 'two lakh fifty' => 250000."},
    {"field_key": "hostel_required", "label": "Hostel required", "field_type": "bool",
     "required": False, "sort_order": 70,
     "prompt_hint": "Ask whether they'll need hostel accommodation — only if they "
                    "are from outside the campus city."},
]

ADMISSION_SCORING = {
    "thresholds": {"hot": 65, "warm": 30},
    "weights": {
        "has_email": 14,
        "has_phone": 16,
        "has_name": 5,
        "required_fields_complete": 30,
        "timeline": {},
        "engagement_per_turn": 2,
        "engagement_cap": 10,
    },
    "hot_requires_contact": True,
}


def _seed_portal(portal: dict, fields: list[dict], scoring: dict) -> str:
    key = portal["portal_key"]
    registry.upsert(key, **{k: v for k, v in portal.items() if k != "portal_key"},
                    scoring_rules=scoring)
    for spec in fields:
        registry.upsert_field(key, spec["field_key"], **{
            k: v for k, v in spec.items() if k != "field_key"
        })
    return key


def run() -> dict[str, int]:
    """Seed both portals. Safe to re-run."""
    from core import db

    db.migrate()
    _seed_portal(PROPERTY_PORTAL, PROPERTY_FIELDS, PROPERTY_SCORING)
    _seed_portal(ADMISSION_PORTAL, ADMISSION_FIELDS, ADMISSION_SCORING)
    return {
        "portals": 2,
        "property_fields": len(PROPERTY_FIELDS),
        "admission_fields": len(ADMISSION_FIELDS),
    }


if __name__ == "__main__":
    result = run()
    print(f"seeded {result['portals']} portals: "
          f"{result['property_fields']} property fields, "
          f"{result['admission_fields']} admission fields")
