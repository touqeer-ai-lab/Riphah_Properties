"""Structured field extraction: a second, independent model call per turn.

Why it is separate from the conversational reply (scope document stage 6): the
reply and the data capture must not share a failure mode. If they were one call,
a prompt change aimed at making the assistant warmer could quietly degrade budget
capture, and nobody would notice until the CRM filled with nulls. Two calls means
tone changes cannot corrupt data, and the extractor can run on a cheaper model at
temperature 0 while the reply runs warmer.

The parameter schema is **built from `portal_fields` at request time**. That is
what makes the multi-portal claim real: the extractor has no idea it is looking at
property data. Point it at the admission portal and it extracts programme, campus
and intake instead, from the same code path.

Normalisation happens here, not in the model. "2.5 crore" becomes 25000000 in
Python because that conversion must be deterministic and testable — asking a
language model to do arithmetic on money is how a lead ends up with a budget of
250,000 instead of 25,000,000.
"""
from __future__ import annotations

import json
import re
from typing import Any

import config
from core import security
from portals import registry

# Pakistani numbering. The multipliers people actually type, including the
# spellings that show up in practice ("lac", "lakh", "crore", "karor").
_MULTIPLIERS = {
    "arab": 1_000_000_000,
    "crore": 10_000_000, "karor": 10_000_000, "cr": 10_000_000,
    "lakh": 100_000, "lac": 100_000, "lack": 100_000, "lakhs": 100_000,
    "million": 1_000_000, "mn": 1_000_000, "m": 1_000_000,
    "thousand": 1_000, "k": 1_000,
    "billion": 1_000_000_000, "bn": 1_000_000_000,
}
_MONEY_RE = re.compile(
    r"(?P<amount>\d[\d,]*\.?\d*)\s*(?P<unit>arab|crores?|karor|cr|lakhs?|lacs?|lack|"
    r"millions?|mn|billions?|bn|thousand|[kmk])?\b",
    re.IGNORECASE,
)

_TRUE_WORDS = {"yes", "true", "y", "haan", "han", "ji", "required", "needed", "1"}
_FALSE_WORDS = {"no", "false", "n", "nahi", "nai", "not required", "0"}


# No property budget or fee budget in PKR is a three-figure sum. A bare "50" is
# almost certainly "50 lakh" said carelessly — but guessing between 50 and
# 5,000,000 is worse than leaving the field unset, because a wrong budget silently
# misroutes the lead's tier. Below this floor, parsing fails and the assistant
# asks again.
MIN_PLAUSIBLE_MONEY = 1_000

_UNIT_SUFFIX_RE = re.compile(
    r"(arab|crores?|karor|cr|lakhs?|lacs?|lack|millions?|mn|billions?|bn|thousand|k)\s*$",
    re.IGNORECASE,
)


# Number words, for voice. A speech transcript writes "two crore", never
# "2 crore", so a parser that requires a digit captures no budget at all from a
# spoken call. Kept to the range people state budgets in — nobody says "four
# hundred and seventy three thousand" out loud about a property.
_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    # Urdu/Hindi numerals heard on a Pakistani portal.
    "aik": 1, "ek": 1, "do": 2, "teen": 3, "char": 4, "panch": 5, "paanch": 5,
    "das": 10, "bees": 20, "pachas": 50, "sau": 100,
}


def _words_to_number(text: str) -> str | None:
    """Rewrite leading number words as digits: "two and a half" -> "2.5".

    Returns None when there is no number word to convert, so callers can tell
    "nothing to do" from "converted to zero".
    """
    tokens = re.findall(r"[a-z]+", text.lower())
    total = 0
    current = 0
    seen = False
    half = False

    index = 0
    while index < len(tokens):
        token = tokens[index]
        # "and a half" / "point five" tails, which is how people round aloud.
        if token == "half" and seen:
            half = True
            index += 1
            continue
        if token in ("and", "a", "point") and seen:
            index += 1
            continue
        if token not in _NUMBER_WORDS:
            # Before the number, skip the hedges people front a budget with
            # ("around", "roughly", "my budget is"). After it, stop — whatever
            # follows is the unit, and the existing regex reads that.
            if not seen:
                index += 1
                continue
            break
        value = _NUMBER_WORDS[token]
        seen = True
        if value == 100:
            current = (current or 1) * 100
        else:
            current += value
        index += 1

    if not seen:
        return None
    total += current
    amount = total + (0.5 if half else 0)
    if amount <= 0:
        return None
    # Hand back the digits plus whatever followed, so the unit ("crore") and the
    # existing regex do the rest of the work.
    remainder = " ".join(tokens[index:])
    formatted = f"{amount:g}"
    return f"{formatted} {remainder}".strip()


def parse_money(text: str | int | float | None) -> int | None:
    """"2.5 crore" -> 25000000. Returns None rather than guessing.

    Ranges record the **upper** bound, because the field these feed is a budget
    ceiling: "2 to 3 crore" means the visitor will go to 3 crore.
    """
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)):
        value = int(text)
        return value if value >= MIN_PLAUSIBLE_MONEY else None

    cleaned = str(text).lower().replace("pkr", "").replace("rs.", "").replace("rs", "")
    cleaned = cleaned.replace("rupees", "").strip()

    # Split a range and keep the upper side. When the unit appears only on one
    # side ("2-3 crore", "2 crore to 3"), it applies to both, so carry it over.
    range_parts = re.split(r"\s*(?:-|–|—|to|se|till|upto|up to)\s*", cleaned)
    if len(range_parts) == 2 and all(p.strip() for p in range_parts):
        lower, upper = (p.strip() for p in range_parts)
        if not _UNIT_SUFFIX_RE.search(upper):
            carried = _UNIT_SUFFIX_RE.search(lower)
            if carried:
                upper = f"{upper} {carried.group(1)}"
        cleaned = upper

    match = _MONEY_RE.search(cleaned)
    if not match:
        # No digits: it may still be a spoken figure ("two crore"). Convert the
        # words and try once more rather than dropping a stated budget.
        spoken = _words_to_number(cleaned)
        if spoken:
            match = _MONEY_RE.search(spoken)
        if not match:
            return None
    try:
        amount = float(match.group("amount").replace(",", ""))
    except ValueError:
        return None

    unit = (match.group("unit") or "").lower()
    multiplier = _MULTIPLIERS.get(unit) or _MULTIPLIERS.get(unit.rstrip("s"), 1)

    value = int(round(amount * multiplier))
    return value if value >= MIN_PLAUSIBLE_MONEY else None


def parse_bool(text: Any) -> bool | None:
    if isinstance(text, bool):
        return text
    if text is None:
        return None
    lowered = str(text).strip().lower()
    if lowered in _TRUE_WORDS:
        return True
    if lowered in _FALSE_WORDS:
        return False
    return None


def _match_enum(value: str, options: list[str]) -> str | None:
    """Map a model-supplied value onto the portal's option list.

    The model is given the enum in its schema and usually returns a member, but
    "medical suites" for "medical_suite" is common enough to be worth handling
    rather than dropping the field.
    """
    if not options:
        return value
    target = value.strip().lower()
    for option in options:
        if option.lower() == target:
            return option
    flat = target.replace(" ", "_").replace("-", "_")
    for option in options:
        if option.lower() == flat:
            return option
    # Substring match, only when exactly one option matches — an ambiguous
    # substring is dropped rather than resolved arbitrarily.
    hits = [o for o in options if target in o.lower() or o.lower() in target]
    return hits[0] if len(hits) == 1 else None


def normalise(field: dict[str, Any], raw: Any) -> tuple[Any, bool]:
    """Coerce one extracted value to its field type.

    Returns (value, ok). `ok=False` means the value could not be normalised and
    should be discarded — storing an unparseable budget is worse than storing
    nothing, because the CRM will filter on it.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, False

    kind = field["field_type"]

    if kind == "money":
        value = parse_money(raw)
        return (value, value is not None)
    if kind == "int":
        try:
            return int(float(str(raw).strip())), True
        except (TypeError, ValueError):
            return None, False
    if kind == "bool":
        value = parse_bool(raw)
        return (value, value is not None)
    if kind == "enum":
        value = _match_enum(str(raw), field.get("options") or [])
        return (value, value is not None)
    if kind == "email":
        value = security.normalise_email(str(raw))
        return (value, value is not None)
    if kind == "phone":
        value = security.normalise_phone(str(raw))
        return (value, value is not None)

    text = " ".join(str(raw).split())[:300]
    return (text, bool(text))


# --------------------------------------------------------------- schema building

def build_schema(portal_key: str) -> dict[str, Any]:
    """JSON Schema for the extraction function, from portal config.

    Contact fields are added first because they exist on every portal; the rest
    come from `portal_fields` in ask order. Every property is nullable and nothing
    is required — an extractor that must return a value will invent one.
    """
    properties: dict[str, Any] = {
        "name": {
            "type": ["string", "null"],
            "description": "The visitor's own name, if they gave it. Not a "
                           "greeting, not a company name, not a project name.",
        },
        "email": {
            "type": ["string", "null"],
            "description": "Email address exactly as written.",
        },
        "phone": {
            "type": ["string", "null"],
            # Voice transcripts spell digits: "oh three double one, four five
            # six". Those are digits the visitor actually said, so transcribing
            # them back is not reformatting — and without this the phone number
            # from a voice call never reaches the CRM, because the normaliser
            # strips everything that isn't a digit and is left with nothing.
            "description": "Phone number, including any country code or leading "
                           "zero. Do not reformat a number written in digits. "
                           "If it is spoken as words, write it as digits: "
                           "'oh three double one four five six' => '0311456', "
                           "'triple eight' => '888', 'double oh' => '00'. "
                           "Return null if you cannot recover a full number.",
        },
    }

    for field in registry.get(portal_key)["fields"]:
        description = field["label"]
        if field.get("extract_hint"):
            description = f"{description}. {field['extract_hint']}"

        if field["field_type"] == "enum":
            properties[field["field_key"]] = {
                "type": ["string", "null"],
                "enum": [*(field.get("options") or []), None],
                "description": description,
            }
        elif field["field_type"] == "bool":
            properties[field["field_key"]] = {
                "type": ["boolean", "null"], "description": description,
            }
        elif field["field_type"] in ("money", "int"):
            # Requested as a string so the visitor's own phrasing survives to
            # Python, where the conversion is deterministic. Asking the model for
            # an integer means asking it to do the arithmetic.
            properties[field["field_key"]] = {
                "type": ["string", "null"],
                "description": f"{description} Report the visitor's own words "
                               f"verbatim (e.g. '2.5 crore', '80 lakh'); do not "
                               f"convert to digits.",
            }
        else:
            properties[field["field_key"]] = {
                "type": ["string", "null"], "description": description,
            }

    properties["_low_confidence"] = {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Field names above that you inferred rather than were told directly, "
            "or are unsure about. Be honest here — a flagged field gets checked by "
            "a human, an unflagged wrong one does not."
        ),
    }
    # The basis annotation is how "asked about it" is separated from "chose it".
    #
    # An earlier attempt required a verbatim visitor quote per field and checked it
    # against the transcript in Python. That does not work, and the reason is worth
    # recording: in the failing case the visitor *did* say the words. "Do you have
    # any two-bed apartments?" contains "two-bed apartments" — a quote check passes
    # it happily. The distinction that matters is not whether the words appeared
    # but whether they were a question or a requirement, and that is semantic.
    #
    # Models are reliable at that classification when asked for it directly, and
    # unreliable at silently declining to fill the field. So the classification is
    # made explicit, per field, and the two bad bases are dropped in code.
    properties["_basis"] = {
        "type": "array",
        "description": (
            "REQUIRED. One entry for every field you filled above, classifying "
            "where the value came from. Fields marked asked_about or "
            "assistant_said are discarded, so classify honestly rather than "
            "defensively."
        ),
        "items": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "description": "the field name above"},
                "basis": {
                    "type": "string",
                    "enum": ["stated", "implied", "asked_about", "assistant_said"],
                    "description": (
                        "stated: the visitor declared it as their own requirement "
                        "('I want a two-bed'). "
                        "implied: not said outright but unambiguous from what they "
                        "said ('I'm in Dubai' implies an overseas buyer). "
                        "asked_about: they only ASKED about it ('do you have "
                        "two-bed apartments?') — a question is not a choice. "
                        "assistant_said: it appears only in the assistant's reply, "
                        "not the visitor's."
                    ),
                },
                "quote": {
                    "type": "string",
                    "description": "the visitor's own words, verbatim, if any",
                },
            },
            "required": ["field", "basis"],
        },
    }

    return {"type": "object", "properties": properties, "required": []}


# Bases that produce a stored value. `asked_about` and `assistant_said` are
# discarded: a visitor who asked whether apartments exist has not chosen one, and
# a value that appears only in the assistant's own reply was never the visitor's.
ACCEPTED_BASES = {"stated", "implied"}




EXTRACT_SYSTEM = """You extract structured data from a sales conversation. You do \
not talk to the visitor and you never write prose.

## The one rule that matters

**Only the VISITOR's own statements are data. The ASSISTANT's lines are context, \
never a source.** If the assistant described three unit types and the visitor said \
nothing about which they want, then no unit type was chosen — return null.

For every value you report you must quote the visitor's own words in `_evidence`. \
That quote is checked against the transcript. **If you cannot quote a VISITOR line \
for a value, do not report that value.** A dropped field costs nothing; a wrong one \
sends a consultant to call about the wrong thing.

## Asking about something is not choosing it

- "Do you have two-bed apartments?" → property_type is null. They asked a question.
- "I'm looking for a two-bed" → property_type: apartment. They stated a requirement.
- "What's the budget range for suites?" → budget_max is null.
- "I can go up to 3 crore" → budget_max: "3 crore".
- "Is Medical City near the hospital?" → project is null.
- "I want to buy in Medical City" → project: Riphah Medical City.

Interest expressed as a question is not a preference. Curiosity is not a commitment.

## Other rules

- If a value was not given, return null. Never guess, never fill a plausible \
default, never carry a value over from an example in the assistant's reply.
- Record the visitor's own wording for money and sizes, verbatim. Do not convert \
units — "2.5 crore" stays "2.5 crore".
- If the visitor corrects an earlier answer, report the correction.
- List anything you inferred rather than were told directly in `_low_confidence`."""


def _client():
    from openai import OpenAI

    return OpenAI(api_key=config.openai_key())


def extract(portal_key: str, transcript: list[dict[str, Any]], *,
            already_captured: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the extraction pass over a conversation.

    Returns {field_key: {"value", "value_raw", "confidence"}} for fields present
    in this conversation. Already-captured fields are passed in so the extractor
    can be told not to re-report them, which keeps the response small and avoids
    a high-confidence value being overwritten by a later low-confidence restatement.
    """
    fields_by_key = {f["field_key"]: f for f in registry.get(portal_key)["fields"]}
    # Contact fields are implicit on every portal, so they need synthetic specs.
    fields_by_key.setdefault("name", {"field_key": "name", "field_type": "text",
                                      "label": "Name"})
    fields_by_key.setdefault("email", {"field_key": "email", "field_type": "email",
                                       "label": "Email"})
    fields_by_key.setdefault("phone", {"field_key": "phone", "field_type": "phone",
                                       "label": "Phone"})

    lines = []
    for turn in transcript:
        if turn.get("role") not in ("user", "assistant"):
            continue
        # The VISITOR / ASSISTANT labels matter: the basis classification depends
        # on the model being able to tell whose line a value came from.
        who = "VISITOR" if turn["role"] == "user" else "ASSISTANT"
        content = " ".join((turn.get("content") or "").split())
        if content:
            lines.append(f"{who}: {content}")
    if not lines:
        return {}

    instruction = "\n".join(lines[-40:])
    if already_captured:
        instruction += (
            "\n\nAlready recorded (do not re-report unless the visitor has "
            "changed it): " + json.dumps(already_captured, ensure_ascii=False)
        )

    response = _client().chat.completions.create(
        model=config.EXTRACT_MODEL,
        temperature=config.EXTRACT_TEMPERATURE,
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": instruction},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": "record_lead_fields",
                "description": "Record the fields the visitor has provided.",
                "parameters": build_schema(portal_key),
            },
        }],
        # Forced: the extractor's only job is to produce the object, and letting
        # it choose means sometimes getting a chat message instead.
        tool_choice={"type": "function", "function": {"name": "record_lead_fields"}},
    )

    calls = response.choices[0].message.tool_calls or []
    if not calls:
        return {}
    try:
        payload = json.loads(calls[0].function.arguments or "{}")
    except json.JSONDecodeError:
        return {}

    low_confidence = {str(k) for k in (payload.pop("_low_confidence", None) or [])}

    basis: dict[str, str] = {}
    quotes: dict[str, str] = {}
    for entry in (payload.pop("_basis", None) or []):
        if isinstance(entry, dict) and entry.get("field"):
            basis[str(entry["field"])] = str(entry.get("basis") or "")
            if entry.get("quote"):
                quotes[str(entry["field"])] = str(entry["quote"])[:200]

    out: dict[str, dict[str, Any]] = {}
    for key, raw in payload.items():
        field = fields_by_key.get(key)
        if not field or raw in (None, "", []):
            continue

        # Contact details are self-evidencing: a pasted email or phone number is
        # not something the assistant could have volunteered on the visitor's
        # behalf, so the basis check adds a failure mode without adding safety.
        if key not in ("email", "phone"):
            declared = basis.get(key)
            if declared and declared not in ACCEPTED_BASES:
                print(f"[extraction] dropped {key}={raw!r}: basis={declared}")
                continue
            # No annotation is not treated as a rejection. When the model omits
            # the entry we cannot tell "the visitor didn't state it" from "the
            # model didn't answer that part", and dropping on silence loses real
            # data — an earlier version of this code did exactly that and threw
            # away a stated budget. Kept, but at reduced confidence, which routes
            # it to human confirmation in the dashboard.
            if not declared:
                low_confidence.add(key)

        value, ok = normalise(field, raw)
        if not ok:
            continue
        # A junk name is a common extractor failure ("Sir", "Property Buyer").
        if key == "name" and not security.looks_like_real_name(str(value)):
            continue

        confident = key not in low_confidence and basis.get(key) != "implied"
        out[key] = {
            "value": value,
            "value_raw": str(raw)[:300],
            "confidence": 0.95 if confident else 0.55,
            "basis": basis.get(key),
            "quote": quotes.get(key),
        }
    return out
