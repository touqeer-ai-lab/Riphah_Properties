"""Tools the assistant can call, as one schema list rendered to OpenAI's wire format.

Three tools, deliberately few. A model given eight overlapping retrieval tools
picks badly and burns a turn doing it; one good search tool plus two
narrow-purpose tools is measurably more reliable.

The interesting one is `check_price_or_availability`. The assistant is *tempted*
to answer pricing questions — it is the most common question and the model has
plenty of plausible-sounding general knowledge about Pakistani property. Prompt
instructions alone are a single point of failure there. Giving it a tool whose
result is the referral means the refusal arrives as **data in the context**, which
the model treats as a fact about the world rather than a rule it is following.
Belt and braces on the one guardrail with real legal exposure.
"""
from __future__ import annotations

import re
from typing import Any

import config
from core import db
from kb import retrieve
from portals import registry

# Tool schemas in a provider-neutral form. `openai_tools()` renders them; adding
# a second provider later means adding a second renderer, not a second schema.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search the approved knowledge base for information about the "
            "projects: unit types, locations, buying process, documentation, "
            "payment structure, handover process, overseas-buyer procedure, "
            "eligibility, FAQs. Call this before answering ANY factual question. "
            "Write the query in English even when the visitor is writing in Urdu "
            "or Roman Urdu. Prefer a specific query over a broad one, and call it "
            "again with different wording if the first result is thin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to look for, in English. A phrase or question, not "
                        "keywords: 'documents an overseas Pakistani needs to buy' "
                        "works better than 'overseas documents'."
                    ),
                },
                "project": {
                    "type": "string",
                    "description": (
                        "Optional project filter. Use only when the visitor has "
                        "named a project; omitting it searches everything and is "
                        "usually the right call."
                    ),
                    "enum": ["riphah-medical-city", "dha-business-district"],
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_price_or_availability",
        "description": (
            "Call this whenever the visitor asks about the price of something, a "
            "payment amount, a per-square-foot rate, or whether a specific unit, "
            "plot or floor is available. It returns the authoritative handling "
            "for this portal. You MUST call it rather than answering a pricing or "
            "availability question yourself, and you must follow what it returns."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "unit_or_type": {
                    "type": "string",
                    "description": (
                        "What the visitor asked about: 'two bed apartment', "
                        "'medical suite', 'unit 304', 'commercial plot'."
                    ),
                },
                "question_kind": {
                    "type": "string",
                    "enum": ["price", "availability", "both"],
                },
            },
            "required": ["unit_or_type", "question_kind"],
        },
    },
    {
        "name": "request_consultant_callback",
        "description": (
            "Flag this conversation for a human consultant. Call it when the "
            "visitor asks to speak to someone, wants to book or visit, needs a "
            "price or availability confirmed, is frustrated, or has asked twice "
            "for something you cannot provide. Calling it does not end the "
            "conversation — keep helping afterwards."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "One line a consultant can read before calling: what the "
                        "visitor wants and what you could not answer."
                    ),
                },
                "urgency": {
                    "type": "string",
                    "enum": ["standard", "same_day"],
                    "description": (
                        "same_day only when the visitor states urgency or is "
                        "ready to transact."
                    ),
                },
            },
            "required": ["reason"],
        },
    },
    {
        "name": "search_riphah_website",
        "description": (
            "Search Riphah's own public websites. This is a FALLBACK, not a "
            "first stop: call it only after search_knowledge_base has returned "
            "found=false for the same question. The approved knowledge base is "
            "always preferred, because it is reviewed content. "
            "Use it for general project, campus, programme, facility or company "
            "questions the knowledge base does not cover. "
            "NEVER use it for prices, payment amounts, per-square-foot rates, "
            "yields, returns or unit availability — use "
            "check_price_or_availability for those, whatever a web page says. "
            "When you answer from this tool you MUST say the information comes "
            "from Riphah's website rather than presenting it as a confirmed "
            "figure, and offer a consultant for anything commercial."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to look for, in English even when the visitor "
                        "wrote Urdu or Roman Urdu. A question or phrase."
                    ),
                },
            },
            "required": ["query"],
        },
    },
]


# DEMO ONLY — the owner's integration briefing. Registered only while
# BRIEFING_ENABLED is set, so switching the flag off removes the tool from both
# the chat and the realtime tool lists. See agent/briefing.py.
if config.BRIEFING_ENABLED:
    TOOL_SPECS.append({
        "name": "internal_integration_briefing",
        "description": (
            "INTERNAL, not for property visitors. Call this when someone asks "
            "about the cost, budget, plan or CRM integration of the WhatsApp "
            "channel, SIP calling, SIM numbers (Jazz, Zong), SMS, or what the AI "
            "assistant itself costs to run. Flow: if you do not yet know who is "
            "speaking, call it with requester_name empty — it will tell you to "
            "ask. Once they give a name, call it again with that name and the "
            "topic they want. Follow the guidance it returns exactly: it decides "
            "whether the briefing may be given."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requester_name": {
                    "type": "string",
                    "description": "The name the caller gave, written in Latin "
                                   "letters even when they spoke Urdu — "
                                   "transliterate, e.g. 'Ali Waqas'. Empty if "
                                   "you have not asked yet.",
                },
                "topic": {
                    "type": "string",
                    "enum": ["overview", "whatsapp", "calls", "sms", "ai_usage",
                             "budget", "crm_integration", "timeline"],
                    "description": "Which part they asked about. 'overview' when "
                                   "unclear or first time.",
                },
            },
            "required": [],
        },
    })


def openai_tools() -> list[dict[str, Any]]:
    """Render to the OpenAI chat-completions tool format."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        }
        for spec in TOOL_SPECS
    ]


def realtime_tools() -> list[dict[str, Any]]:
    """Render to the Realtime API tool format, which is flat rather than nested."""
    return [
        {
            "type": "function",
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["parameters"],
        }
        for spec in TOOL_SPECS
    ]


# ------------------------------------------------------------------ implementations

def _search_knowledge_base(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return {"found": False, "error": "empty query"}

    portal_key = ctx["portal_key"]
    portal = registry.get(portal_key)
    # Volatile passages (prices, inventory) only enter the context when the portal
    # is configured to use them. Withholding at retrieval rather than trusting the
    # prompt is the difference between "shouldn't quote" and "can't quote".
    include_volatile = portal.get("pricing_mode") in ("indicative", "live")

    result = retrieve.search(
        query,
        portal_key=portal_key,
        project=args.get("project"),
        include_volatile=include_volatile,
    )

    if not result["found"]:
        retrieve.log_gap(
            query, portal_key=portal_key, session_id=ctx.get("session_id"),
            top_similarity=result["top_similarity"], language=ctx.get("language"),
        )

    return {
        "found": result["found"],
        "passages": retrieve.format_passages(result["passages"]),
        "sources": [
            {
                "document": p.get("document"),
                "heading": p.get("heading"),
                "similarity": p.get("similarity"),
            }
            for p in result["passages"]
        ],
        "top_similarity": result["top_similarity"],
        "guidance": (
            "Answer from these passages only."
            if result["found"] else
            "Nothing relevant. Tell the visitor you don't have that and offer a "
            "consultant. Do not answer from general knowledge."
        ),
    }


# Pricing language, checked before a web search runs. The model is instructed not
# to use this tool for pricing, but an instruction is not a control: a web page
# stating a per-square-foot rate would otherwise walk straight past the volatile
# withholding in retrieval and the referral in check_price_or_availability. This
# is the same defence-in-depth argument the pricing tool itself makes.
_PRICING_QUERY = re.compile(
    r"\b(price|pricing|cost|rate|per\s*(sq|square)|sq\.?\s*ft|installment|"
    r"instalment|payment\s*plan|down\s*payment|booking\s*amount|yield|return|"
    r"roi|rent|rental|profit|discount|offer|deal|lakh|crore|lac|arab|"
    r"available|availability|inventory|stock|vacant|qeemat|keemat|kimat)\b",
    re.IGNORECASE,
)


def _search_riphah_website(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Domain-limited web search, used only when the knowledge base came up empty.

    Returns prose plus the URLs it came from, so the reply can attribute it. The
    caller is told, in the result rather than only in the prompt, that this is
    website material and not reviewed corpus content.
    """
    query = (args.get("query") or "").strip()
    if not query:
        return {"found": False, "error": "empty query"}

    if not config.WEB_SEARCH_ENABLED:
        return {
            "found": False,
            "guidance": (
                "Web search is disabled for this deployment. Tell the visitor "
                "you don't have that information and offer a consultant."
            ),
        }

    if _PRICING_QUERY.search(query):
        return {
            "found": False,
            "refused": "pricing_or_availability",
            "guidance": (
                "This is a pricing or availability question. Web search is not "
                "permitted for these. Call check_price_or_availability instead "
                "and follow what it returns."
            ),
        }

    tool: dict[str, Any] = {"type": "web_search"}
    if config.WEB_SEARCH_DOMAINS:
        tool["filters"] = {"allowed_domains": list(config.WEB_SEARCH_DOMAINS)}

    try:
        from openai import OpenAI

        client = OpenAI(api_key=config.openai_key(), timeout=60)
        response = client.responses.create(
            model=config.WEB_SEARCH_MODEL,
            input=(
                "Answer this question about Riphah using only the search "
                "results. Be brief and factual. State no prices, payment "
                "amounts, yields or availability even if a page mentions them. "
                "If the results do not answer it, say exactly: NOT_FOUND.\n\n"
                f"Question: {query}"
            ),
            tools=[tool],
            max_output_tokens=config.WEB_SEARCH_MAX_TOKENS,
        )
        text = (response.output_text or "").strip()
    except Exception as exc:  # noqa: BLE001
        # A failed web search is not a failed turn. The assistant still has the
        # "I don't have that, here's a consultant" path.
        return {
            "found": False,
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "guidance": (
                "The website search failed. Tell the visitor you don't have "
                "that information and offer a consultant. Do not answer from "
                "general knowledge."
            ),
        }

    if not text or "NOT_FOUND" in text:
        retrieve.log_gap(
            query, portal_key=ctx["portal_key"], session_id=ctx.get("session_id"),
            top_similarity=None, language=ctx.get("language"),
        )
        return {
            "found": False,
            "guidance": (
                "Riphah's website does not cover this either. Tell the visitor "
                "you don't have it and offer a consultant."
            ),
        }

    # Citation URLs come back as annotations on the output text.
    sources: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        for block in getattr(item, "content", []) or []:
            for note in getattr(block, "annotations", []) or []:
                url = getattr(note, "url", None)
                if not url:
                    continue
                url = url.split("?utm_source=")[0]
                if any(s["url"] == url for s in sources):
                    continue
                sources.append({
                    "document": getattr(note, "title", None) or url,
                    "heading": "Riphah website",
                    "url": url,
                    "similarity": None,
                })

    return {
        "found": True,
        "answer": text,
        "sources": sources[:4],
        "origin": "riphah_website",
        "guidance": (
            "This came from Riphah's public website, not the approved corpus. "
            "Say so in your reply — for example 'according to Riphah's "
            "website' — and do not present it as a confirmed commitment. State "
            "no price, payment, yield or availability figure from it. Offer a "
            "consultant for anything commercial."
        ),
    }


def _check_price_or_availability(args: dict[str, Any],
                                 ctx: dict[str, Any]) -> dict[str, Any]:
    """Authoritative pricing handling for the portal (scope document s8).

    Under the default 'refer' mode this returns a refusal plus a script — which is
    the point: the model receives the constraint as a tool result rather than
    having to remember a prompt rule under pressure from a visitor pushing for a
    ballpark.
    """
    portal = registry.get(ctx["portal_key"])
    mode = portal.get("pricing_mode", "refer")
    subject = (args.get("unit_or_type") or "that unit").strip()
    kind = args.get("question_kind", "price")

    if kind in ("availability", "both"):
        availability = (
            "NOT_AVAILABLE_TO_YOU: availability of a named unit is never confirmed "
            "by the assistant. There is no inventory connection on this portal. "
            "Say the sales office confirms what is currently available."
        )
    else:
        availability = None

    if mode == "refer":
        return {
            "pricing_mode": "refer",
            "may_state_figure": False,
            "availability": availability,
            "result": (
                f"NO_FIGURE_AVAILABLE for '{subject}'. This portal refers all "
                "pricing to a human consultant."
            ),
            "guidance": (
                "Do not state or estimate any figure, including a range or a "
                "'rough idea'. Explain that pricing is confirmed by a consultant "
                "because it depends on the release phase and the specific unit — "
                "present that as accuracy, not as a restriction. You MAY explain "
                "payment structure, plan lengths, and which charges sit outside "
                "the unit price. Then offer to arrange the call and ask for one "
                "contact route if you don't have one. If the visitor pushes for a "
                "ballpark, decline warmly and move on — do not concede a number."
            ),
        }

    if mode == "indicative":
        result = retrieve.search(
            f"indicative price band {subject}",
            portal_key=ctx["portal_key"], include_volatile=True,
        )
        return {
            "pricing_mode": "indicative",
            "may_state_figure": bool(result["found"]),
            "availability": availability,
            "result": retrieve.format_passages(result["passages"]),
            "guidance": (
                "Quote only a band that appears verbatim above. Do not narrow, "
                "average, interpolate, or compute an instalment. State in the same "
                "breath that it is indicative, per unit type rather than per unit, "
                "and superseded by the price list current at booking. If no band "
                "above covers what was asked, refer to a consultant instead."
            ),
        }

    # mode == "live": the inventory feed is a client dependency, not built here.
    return {
        "pricing_mode": "live",
        "may_state_figure": False,
        "availability": availability,
        "result": (
            "INVENTORY_FEED_NOT_CONNECTED: this portal is configured for live "
            "figures but no Riphah inventory endpoint is wired up yet."
        ),
        "guidance": (
            "Treat this exactly as the refer case: no figure, explain that a "
            "consultant confirms current pricing, offer the call. Do not "
            "substitute a knowledge-base passage for a live figure."
        ),
    }


def _request_consultant_callback(args: dict[str, Any],
                                 ctx: dict[str, Any]) -> dict[str, Any]:
    """Record a handover request against the session and note it for the CRM."""
    session_id = ctx.get("session_id")
    reason = (args.get("reason") or "Visitor asked for a consultant").strip()[:500]
    urgency = args.get("urgency", "standard")

    if session_id:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO notes (session_id, author, body, created_at) "
                "VALUES (?,?,?,?)",
                (session_id, "assistant",
                 f"[callback requested — {urgency}] {reason}", db.now()),
            )
        db.audit("assistant", "callback.requested", entity="chat_session",
                 entity_id=session_id, detail={"reason": reason, "urgency": urgency})

    return {
        "recorded": True,
        "urgency": urgency,
        "guidance": (
            "Confirm to the visitor that you've passed the enquiry to the team, in "
            "one sentence, and say briefly what you've passed on. Then ask for one "
            "contact route if you still don't have a phone number or an email. Do "
            "not promise a specific callback time and do not invent a phone number "
            "or an office address."
        ),
    }


def _internal_integration_briefing(args: dict[str, Any],
                                   ctx: dict[str, Any]) -> dict[str, Any]:
    from agent import briefing

    return briefing.briefing(
        requester_name=args.get("requester_name"), topic=args.get("topic"),
        session_id=ctx.get("session_id"),
    )


DISPATCH = {
    "search_knowledge_base": _search_knowledge_base,
    "search_riphah_website": _search_riphah_website,
    "check_price_or_availability": _check_price_or_availability,
    "request_consultant_callback": _request_consultant_callback,
}
if config.BRIEFING_ENABLED:
    DISPATCH["internal_integration_briefing"] = _internal_integration_briefing


def execute(name: str, args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Run one tool. Never raises: a tool failure becomes a result the model can
    read and recover from, because an exception here would abort the visitor's turn."""
    handler = DISPATCH.get(name)
    if not handler:
        return {"error": f"unknown tool '{name}'"}
    try:
        return handler(args or {}, ctx)
    except Exception as exc:  # noqa: BLE001
        print(f"[tools] {name} failed: {type(exc).__name__}: {exc}")
        return {
            "found": False,
            "error": f"{type(exc).__name__}: {exc}",
            "guidance": (
                "The lookup failed for technical reasons. Tell the visitor you "
                "can't reach that information right now and offer a consultant. "
                "Do not answer from general knowledge."
            ),
        }


def tool_context(*, portal_key: str | None = None, session_id: str | None = None,
                 language: str | None = None) -> dict[str, Any]:
    return {
        "portal_key": portal_key or config.DEFAULT_PORTAL,
        "session_id": session_id,
        "language": language,
    }
