"""Qualification scoring: Hot / Warm / Cold / Spam.

Rules live in `portals.scoring_rules` as JSON, so sales leadership can retune the
thresholds after watching real traffic without a code deployment (spec s8). This
module is the interpreter for that JSON, not the rules themselves.

Two design points worth defending:

**Every score carries its own explanation.** `score_detail` records which rules
fired and for how many points. A salesperson who is told a lead is Hot will ask
why, and "the model decided" is not an answer that survives contact with a sales
floor. It also makes a threshold change auditable: you can replay why yesterday's
leads scored what they did.

**A Hot lead must be contactable.** No phone and no email means no callback is
possible, whatever else the conversation contained — so `hot_requires_contact`
demotes it to Warm regardless of score. Without that rule the Hot queue fills with
leads nobody can act on, and the sales team stops trusting the tier.
"""
from __future__ import annotations

from typing import Any

from core import security

TIERS = ("hot", "warm", "cold", "spam")

# Fallbacks used only when a portal has no scoring_rules JSON at all. A portal
# should always have them; this exists so a misconfigured portal still produces a
# defensible tier rather than crashing the lead pipeline.
DEFAULT_RULES: dict[str, Any] = {
    "thresholds": {"hot": 70, "warm": 35},
    "weights": {
        "has_email": 12, "has_phone": 18, "has_name": 5,
        "required_fields_complete": 20,
        "budget_in_range": 15,
        "timeline": {}, "purpose": {}, "payment_pref": {},
        "engagement_per_turn": 2, "engagement_cap": 12,
    },
    "hot_requires_contact": True,
}

# --------------------------------------------------------------------------- spam

# Phrases that indicate the conversation is not a property enquiry. Kept short and
# high-precision: a false spam classification removes a real lead from the sales
# queue entirely, which is a worse error than letting junk through to be triaged.
_SPAM_MARKERS = (
    "ignore previous instructions", "ignore all previous", "you are now",
    "system prompt", "developer mode", "jailbreak", "disregard your",
    "seo services", "rank your website", "increase your traffic",
    "bitcoin", "crypto investment", "forex signals", "loan offer",
    "click here to claim", "you have won",
)


def spam_signals(*, contact: dict[str, Any], messages: list[dict[str, Any]],
                 turn_count: int) -> list[str]:
    """Reasons to treat this as spam. Empty list means it isn't.

    A disposable email (yopmail, mailinator…) is deliberately NOT a spam signal.
    A real buyer often uses a throwaway address to avoid marketing, and hiding
    their lead loses a genuine enquiry — a far worse error than showing one that
    turns out to be junk. It is surfaced as a soft flag in the score detail
    instead (see `score`), so a consultant knows to get a phone number, and the
    lead stays visible. Only an *unparseable* email is spam, because that is not
    a contact route at all.
    """
    reasons: list[str] = []

    email = contact.get("email")
    if email and not security.normalise_email(email):
        reasons.append("email_invalid")
    if contact.get("phone") and not security.normalise_phone(contact["phone"]):
        reasons.append("phone_invalid")

    visitor_text = " ".join(
        (m.get("content") or "").lower()
        for m in messages if m.get("role") == "user"
    )
    for marker in _SPAM_MARKERS:
        if marker in visitor_text:
            reasons.append("prompt_injection" if "instruction" in marker
                           or "prompt" in marker or "mode" in marker
                           else "promotional_content")
            break

    # Identical message repeated: a bot, or someone hammering the send button.
    user_messages = [
        " ".join((m.get("content") or "").split()).lower()
        for m in messages if m.get("role") == "user"
    ]
    if len(user_messages) >= 4 and len(set(user_messages)) == 1:
        reasons.append("repeated_identical_messages")

    return list(dict.fromkeys(reasons))


# -------------------------------------------------------------------- scoring

def _weight(weights: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = weights.get(key, default)
    return float(value) if isinstance(value, (int, float)) else default


def score(*, rules: dict[str, Any], contact: dict[str, Any],
          fields: dict[str, Any], required_keys: list[str],
          turn_count: int = 0,
          messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Score one assembled lead.

    `fields` are normalised values keyed by field_key; `contact` holds name, email
    and phone. Returns tier, points, and the rule-by-rule breakdown.
    """
    rules = rules or DEFAULT_RULES
    weights = {**DEFAULT_RULES["weights"], **(rules.get("weights") or {})}
    thresholds = {**DEFAULT_RULES["thresholds"], **(rules.get("thresholds") or {})}
    messages = messages or []

    detail: list[dict[str, Any]] = []
    points = 0.0

    def award(rule: str, amount: float, note: str = "") -> None:
        nonlocal points
        if amount:
            points += amount
            detail.append({"rule": rule, "points": round(amount, 1), "note": note})

    # --- spam takes precedence over everything else -------------------------
    reasons = spam_signals(contact=contact, messages=messages, turn_count=turn_count)
    if reasons:
        return {
            "qualification": "spam",
            "score": 0,
            "detail": {"tier": "spam", "reasons": reasons, "rules_fired": [],
                       "excluded_from_lead_counts": True},
        }

    # --- contact reachability ----------------------------------------------
    has_email = bool(security.normalise_email(contact.get("email")))
    has_phone = bool(security.normalise_phone(contact.get("phone")))
    # A disposable email still counts as an email, but it is a weak route — flag
    # it so a consultant asks for a phone number rather than trusting a mailbox
    # that may not be read. Not a spam signal; the lead stays visible.
    disposable_email = has_email and security.is_disposable_email(contact.get("email"))
    if has_email:
        note = "disposable — get a phone number" if disposable_email else ""
        award("has_email", _weight(weights, "has_email"), note)
    if has_phone:
        award("has_phone", _weight(weights, "has_phone"))
    if security.looks_like_real_name(contact.get("name")):
        award("has_name", _weight(weights, "has_name"))

    # --- required-field completeness, pro-rated ----------------------------
    if required_keys:
        filled = sum(1 for key in required_keys if fields.get(key) not in (None, ""))
        ratio = filled / len(required_keys)
        award("required_fields_complete",
              _weight(weights, "required_fields_complete") * ratio,
              f"{filled}/{len(required_keys)} required fields")

    # --- budget against the project band -----------------------------------
    budget_range = rules.get("budget_range") or {}
    budget = fields.get("budget_max") or fields.get("fee_budget")
    if budget and budget_range.get("min") is not None:
        try:
            amount = int(budget)
        except (TypeError, ValueError):
            amount = None
        if amount is not None:
            low = int(budget_range.get("min", 0))
            high = int(budget_range.get("max", 10**12))
            if low <= amount <= high:
                note = "within band"
                if budget_range.get("provisional"):
                    # Say so in the audit trail. A band nobody has confirmed should
                    # not look like a Riphah figure in a score explanation.
                    note += " (band is provisional, not Riphah-confirmed)"
                award("budget_in_range", _weight(weights, "budget_in_range"), note)
            elif amount > high:
                # Above the band is a strong buyer, not a bad fit. Half credit
                # rather than zero, and flagged for a consultant.
                award("budget_above_range",
                      _weight(weights, "budget_in_range") * 0.5,
                      "above band — verify inventory suits")
            else:
                detail.append({"rule": "budget_below_range", "points": 0,
                               "note": f"{amount} below band minimum {low}"})

    # --- categorical fields with per-value weights -------------------------
    for field_key in ("timeline", "purpose", "payment_pref"):
        table = weights.get(field_key)
        value = fields.get(field_key)
        if isinstance(table, dict) and value:
            award(f"{field_key}:{value}", float(table.get(str(value), 0) or 0))

    if fields.get("is_overseas"):
        award("is_overseas", _weight(weights, "is_overseas"),
              "overseas buyer — needs remote handling")

    # --- engagement depth ---------------------------------------------------
    per_turn = _weight(weights, "engagement_per_turn")
    cap = _weight(weights, "engagement_cap")
    if per_turn and turn_count:
        award("engagement", min(turn_count * per_turn, cap or turn_count * per_turn),
              f"{turn_count} turns")

    raw_points = points
    # Reported out of 100. Weights deliberately sum past 100 so a strong lead
    # missing one field still clears the hot threshold comfortably — but a
    # dashboard showing "score 110" invites the question of what the scale is, so
    # the reported figure is clamped and the raw total kept in the audit detail.
    total = min(100, int(round(raw_points)))

    # --- tier ---------------------------------------------------------------
    # Tiering uses the clamped score. Thresholds are well under 100, so clamping
    # cannot change a tier; it only affects presentation.
    if total >= float(thresholds.get("hot", 70)):
        tier = "hot"
    elif total >= float(thresholds.get("warm", 35)):
        tier = "warm"
    else:
        tier = "cold"

    demoted = None
    if tier == "hot" and rules.get("hot_requires_contact", True) \
            and not (has_email or has_phone):
        tier, demoted = "warm", "hot_requires_contact"
    # Cold is also the floor for anyone we cannot contact at all: the scope
    # document defines Cold as "browsing only, or no contact detail captured".
    if not (has_email or has_phone) and tier == "warm" and total < float(
            thresholds.get("warm", 35)) * 1.5:
        tier, demoted = "cold", "no_contact_route"

    return {
        "qualification": tier,
        "score": total,
        "detail": {
            "tier": tier,
            "points": total,
            "raw_points": round(raw_points, 1),
            "clamped": raw_points > 100,
            "thresholds": thresholds,
            "has_email": has_email,
            "has_phone": has_phone,
            "disposable_email": disposable_email,
            "demoted_by": demoted,
            "rules_fired": detail,
        },
    }


def tier_action(tier: str) -> dict[str, Any]:
    """What the tier means operationally (scope document s7).

    Returned with the lead so the CRM and the alerting path agree on what a tier
    obliges, rather than each re-deciding.
    """
    return {
        "hot": {
            "alert": True, "queue": "priority",
            "callback_target": "one working day",
            "note": "Immediate alert to the assigned sales owner.",
        },
        "warm": {
            "alert": False, "queue": "standard",
            "callback_target": "three working days",
            "note": "Standard queue and nurture sequence.",
        },
        "cold": {
            "alert": False, "queue": "none",
            "callback_target": None,
            "note": "Retained for analytics. No sales action.",
        },
        "spam": {
            "alert": False, "queue": "none",
            "callback_target": None,
            "note": "Excluded from lead counts.",
        },
    }.get(tier, {"alert": False, "queue": "standard", "callback_target": None,
                 "note": ""})
