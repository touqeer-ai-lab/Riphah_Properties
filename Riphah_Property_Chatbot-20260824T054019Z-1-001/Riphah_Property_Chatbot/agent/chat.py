"""The turn loop: retrieve, answer, extract, score, deliver.

One visitor message runs two independent model calls:

  1. **Reply** — tool-calling loop over the knowledge base, writes prose.
  2. **Extract** — structured capture (agent/extraction.py), writes data.

They are deliberately not the same call. See the module docstring in
extraction.py; the short version is that a change to the assistant's tone must
not be able to break budget capture.

Ordering inside a turn is chosen so a failure loses as little as possible:

  persist user message  ->  reply  ->  persist reply  ->  extract  ->  lead  ->  queue

The visitor's message is stored before anything can fail. The reply is stored
before extraction runs, so an extraction error costs a lead field rather than the
answer the visitor already read. Webhook delivery is queued, never awaited, so a
dead CRM cannot show up as latency in the chat.
"""
from __future__ import annotations

import json
from typing import Any

import config
import re
from agent import conversations, extraction, prompts, tools
from core import db
from kb import retrieve
from leads import delivery, store
from portals import registry

# A turn is capped at this many tool round-trips. The realistic worst case is now
# a knowledge-base search, a retry with different wording, the website fallback
# when both came up empty, and a pricing check — four calls, so the cap sits one
# above it. Beyond that the model is looping and the visitor is waiting.
MAX_TOOL_ROUNDS = 5

# Messages that need no knowledge lookup: greetings, thanks, acknowledgements,
# and the bare contact details a visitor sends when asked. Pre-retrieval is
# skipped for these — an embedding call on "thanks" is latency and cost for a
# guaranteed miss, and a miss would log a spurious knowledge gap.
_SOCIAL_ONLY = re.compile(
    r"^(hi|hey|hello|salam|assalam[\w\s-]*|aoa|thanks?|thank you|thx|shukriya|"
    r"ok(ay)?|acha|thik|sure|yes|no|yeah|nope|got it|bye|goodbye|allah hafiz|"
    r"good (morning|afternoon|evening))[\s!.,]*$",
    re.IGNORECASE,
)


def _needs_retrieval(message: str) -> bool:
    """Whether to pre-retrieve for this message.

    Biased towards retrieving: a false positive costs one embedding call, a false
    negative costs a grounded answer.
    """
    stripped = message.strip()
    if len(stripped) < 3:
        return False
    if _SOCIAL_ONLY.match(stripped):
        return False
    # A message that is only contact details is an answer to our question, not a
    # question of its own.
    without_contact = re.sub(
        r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s()-]{7,}", " ", stripped
    )
    return len(without_contact.split()) >= 2


def _client():
    from openai import OpenAI

    return OpenAI(api_key=config.openai_key(), base_url=config.OPENAI_BASE_URL)


def _to_openai_messages(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Stored turns -> chat-completions messages.

    Tool turns are dropped from the replayed history on purpose. Their results
    were already folded into the assistant reply that followed, and replaying raw
    passages from six turns ago crowds out the passages retrieved for *this*
    question.
    """
    out = []
    for turn in history:
        if turn.get("role") not in ("user", "assistant"):
            continue
        content = turn.get("content")
        if content:
            out.append({"role": turn["role"], "content": content})
    return out


def answer(message: str, *, session_id: str, portal_key: str | None = None,
           language: str | None = None, channel: str = "text",
           run_extraction: bool = True) -> dict[str, Any]:
    """Handle one visitor message end to end."""
    portal_key = portal_key or config.DEFAULT_PORTAL
    portal = registry.get(portal_key)

    message = (message or "").strip()
    if not message:
        raise ValueError("empty message")
    if len(message) > config.MAX_MESSAGE_CHARS:
        # Truncated rather than rejected: a visitor who pasted a long brief should
        # get an answer to the start of it, not an error.
        message = message[:config.MAX_MESSAGE_CHARS]

    limited, remaining = conversations.rate_limit_exceeded(session_id)
    if limited:
        return {
            "answer": (
                "You've sent a lot of messages in a short time, so I need to pause "
                "for a few minutes. If something is urgent, ask for a consultant "
                "and I'll pass your enquiry on."
            ),
            "rate_limited": True,
            "trace": [],
            "citations": [],
        }

    # --- 1. persist before processing ---------------------------------------
    history = conversations.history(session_id)
    user_message_id = conversations.add_message(
        session_id, "user", message, channel=channel, language=language
    )

    # --- 2. build the prompt from live state --------------------------------
    captured = store.captured_for_session(session_id)
    outstanding = store.outstanding_for(portal_key, captured)
    turn_count = conversations.turn_count(session_id)

    system = prompts.system_prompt(
        portal,
        captured=captured,
        outstanding=outstanding,
        turn_count=turn_count,
        has_contact=store.has_contact(captured),
        channel=channel,
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        *_to_openai_messages(history),
    ]

    ctx = tools.tool_context(portal_key=portal_key, session_id=session_id,
                             language=language)
    trace: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []

    # --- pre-retrieval ------------------------------------------------------
    # Passages for this message are fetched before the model runs, rather than
    # only when it decides to call the search tool. Relying on the tool call alone
    # leaves a gap: on a follow-up the model often believes it already knows the
    # answer from three turns ago and skips the search, then extends the
    # remembered answer with plausible neighbouring detail. Injecting the passages
    # unconditionally means the grounding is present whether or not it asks.
    # The tool remains available for follow-up searches within the turn.
    if _needs_retrieval(message):
        portal_conf = registry.get(portal_key)
        retrieved = retrieve.search(
            message,
            portal_key=portal_key,
            include_volatile=portal_conf.get("pricing_mode") in ("indicative", "live"),
        )
        messages.append({
            "role": "system",
            "content": prompts.knowledge_turn(
                retrieve.format_passages(retrieved["passages"]),
                found=retrieved["found"],
            ),
        })
        trace.append({"tool": "pre_retrieval", "input": {"query": message},
                      "found": retrieved["found"]})
        for passage in retrieved["passages"]:
            citations.append({
                "document": passage.get("document"),
                "heading": passage.get("heading"),
                "similarity": passage.get("similarity"),
            })
        if not retrieved["found"]:
            retrieve.log_gap(message, portal_key=portal_key, session_id=session_id,
                             top_similarity=retrieved["top_similarity"],
                             language=language)

    messages.append({"role": "user", "content": message})

    client = _client()
    reply_text = ""
    usage_in = usage_out = 0

    # --- 3. tool loop -------------------------------------------------------
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.chat.completions.create(
            model=config.CHAT_MODEL,
            temperature=config.CHAT_TEMPERATURE,
            messages=messages,
            tools=tools.openai_tools(),
            tool_choice="auto",
        )
        if response.usage:
            usage_in += response.usage.prompt_tokens or 0
            usage_out += response.usage.completion_tokens or 0

        choice = response.choices[0].message
        calls = choice.tool_calls or []

        if not calls:
            reply_text = (choice.content or "").strip()
            break

        # Echo the assistant's tool-call turn back, or the follow-up call has no
        # antecedent for its tool results.
        messages.append({
            "role": "assistant",
            "content": choice.content,
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name,
                              "arguments": c.function.arguments}}
                for c in calls
            ],
        })

        for call in calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            result = tools.execute(name, args, ctx)
            found = result.get("found")
            trace.append({"tool": name, "input": args, "found": found})
            for source in result.get("sources") or []:
                citations.append(source)

            conversations.add_message(
                session_id, "tool", None, channel=channel,
                tool_name=name, tool_input=args, tool_found=found,
            )
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False)[:12000],
            })
    else:
        # Loop exhausted without a text reply. Ask once for prose, no tools.
        final = client.chat.completions.create(
            model=config.CHAT_MODEL,
            temperature=config.CHAT_TEMPERATURE,
            messages=[*messages, {
                "role": "system",
                "content": "Answer the visitor now, from what the tool results "
                           "above contain. Do not call any more tools.",
            }],
        )
        reply_text = (final.choices[0].message.content or "").strip()

    return _finalize(
        reply_text=reply_text, citations=citations, trace=trace,
        history=history, message=message, captured=captured,
        session_id=session_id, portal_key=portal_key, language=language,
        channel=channel, user_message_id=user_message_id,
        run_extraction=run_extraction, usage_in=usage_in, usage_out=usage_out,
        remaining=remaining,
    )


def _finalize(*, reply_text: str, citations: list[dict[str, Any]],
              trace: list[dict[str, Any]], history: list[dict[str, Any]],
              message: str, captured: dict[str, Any], session_id: str,
              portal_key: str, language: str | None, channel: str,
              user_message_id: int | None, run_extraction: bool,
              usage_in: int, usage_out: int, remaining: int) -> dict[str, Any]:
    """Shared tail for both the blocking and streaming paths: dedupe citations,
    persist the reply, run extraction, assemble/deliver the lead, return the
    turn summary. Kept in one place so the two paths cannot drift apart."""
    if not reply_text:
        reply_text = (
            "Sorry — I couldn't put that answer together. Could you rephrase it, "
            "or would you like a consultant to get in touch?"
        )

    # Deduplicate citations by document+heading, keeping the strongest match.
    seen: dict[tuple, dict[str, Any]] = {}
    for source in citations:
        key = (source.get("document"), source.get("heading"))
        if key not in seen or (source.get("similarity") or 0) > (
                seen[key].get("similarity") or 0):
            seen[key] = source

    # Website sources sort ahead of corpus passages: the website is only searched
    # after the corpus failed, so a URL here is what the answer was built from.
    citations = sorted(
        seen.values(),
        key=lambda s: (0 if s.get("url") else 1, -(s.get("similarity") or 0)),
    )[:4]

    conversations.add_message(
        session_id, "assistant", reply_text, channel=channel,
        citations=citations, language=language,
        tokens_in=usage_in or None, tokens_out=usage_out or None,
    )

    lead_state: dict[str, Any] = {}
    if run_extraction:
        try:
            transcript = [
                *_to_openai_messages(history),
                {"role": "user", "content": message},
                {"role": "assistant", "content": reply_text},
            ]
            extracted = extraction.extract(
                portal_key, transcript, already_captured=captured
            )
            result = store.apply_extraction(
                session_id=session_id, portal_key=portal_key,
                extracted=extracted, source_message_id=user_message_id,
            )
            if result:
                lead_state = result
                lead_state["dispatched"] = delivery.dispatch(result)
        except Exception as exc:  # noqa: BLE001
            print(f"[chat] extraction failed: {type(exc).__name__}: {exc}")
            db.audit("system", "extraction.failed", entity="chat_session",
                     entity_id=session_id, detail=str(exc)[:400])

    return {
        "answer": reply_text,
        "session_id": session_id,
        "trace": trace,
        "citations": citations,
        "lead": lead_state,
        "captured": store.captured_for_session(session_id),
        "outstanding": [
            f["field_key"] for f in store.outstanding_for(
                portal_key, store.captured_for_session(session_id)
            )
        ],
        "tokens": {"in": usage_in, "out": usage_out},
        "rate_limit_remaining": remaining - 1,
    }


def answer_stream(message: str, *, session_id: str, portal_key: str | None = None,
                  language: str | None = None, channel: str = "text",
                  run_extraction: bool = True):
    """Streaming twin of `answer()`. A generator that yields event dicts:

        {"type": "tool",  "label": "..."}          a tool ran (for a status line)
        {"type": "delta", "text": "..."}           a chunk of the reply
        {"type": "done",  ...same shape as answer()...}
        {"type": "error", "detail": "...", "status": 503}

    The reply text is streamed token-by-token as the model produces it; the tool
    loop, retrieval, extraction, scoring and CRM delivery are identical to the
    blocking path — only the final prose is streamed instead of returned whole.
    """
    portal_key = portal_key or config.DEFAULT_PORTAL
    registry.get(portal_key)  # validates the portal

    message = (message or "").strip()
    if not message:
        yield {"type": "error", "detail": "empty message", "status": 422}
        return
    if len(message) > config.MAX_MESSAGE_CHARS:
        message = message[:config.MAX_MESSAGE_CHARS]

    limited, remaining = conversations.rate_limit_exceeded(session_id)
    if limited:
        text = ("You've sent a lot of messages in a short time, so I need to pause "
                "for a few minutes. If something is urgent, ask for a consultant "
                "and I'll pass your enquiry on.")
        yield {"type": "delta", "text": text}
        yield {"type": "done", "answer": text, "rate_limited": True,
               "trace": [], "citations": [], "lead": {},
               "captured": store.captured_for_session(session_id),
               "rate_limit_remaining": 0}
        return

    if not config.has_openai_key():
        yield {"type": "error", "status": 503,
               "detail": "OPENAI_API_KEY is not configured on the server."}
        return

    # --- setup: identical to answer() ---------------------------------------
    history = conversations.history(session_id)
    user_message_id = conversations.add_message(
        session_id, "user", message, channel=channel, language=language)

    captured = store.captured_for_session(session_id)
    outstanding = store.outstanding_for(portal_key, captured)
    system = prompts.system_prompt(
        registry.get(portal_key), captured=captured, outstanding=outstanding,
        turn_count=conversations.turn_count(session_id),
        has_contact=store.has_contact(captured), channel=channel)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        *_to_openai_messages(history),
    ]
    ctx = tools.tool_context(portal_key=portal_key, session_id=session_id,
                             language=language)
    trace: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []

    if _needs_retrieval(message):
        portal_conf = registry.get(portal_key)
        retrieved = retrieve.search(
            message, portal_key=portal_key,
            include_volatile=portal_conf.get("pricing_mode") in ("indicative", "live"))
        messages.append({"role": "system", "content": prompts.knowledge_turn(
            retrieve.format_passages(retrieved["passages"]), found=retrieved["found"])})
        trace.append({"tool": "pre_retrieval", "input": {"query": message},
                      "found": retrieved["found"]})
        for passage in retrieved["passages"]:
            citations.append({"document": passage.get("document"),
                              "heading": passage.get("heading"),
                              "similarity": passage.get("similarity")})
        if not retrieved["found"]:
            retrieve.log_gap(message, portal_key=portal_key, session_id=session_id,
                             top_similarity=retrieved["top_similarity"], language=language)

    messages.append({"role": "user", "content": message})

    client = _client()
    reply_text = ""
    usage_in = usage_out = 0

    try:
        # --- streaming tool loop --------------------------------------------
        for _ in range(MAX_TOOL_ROUNDS):
            stream = client.chat.completions.create(
                model=config.CHAT_MODEL, temperature=config.CHAT_TEMPERATURE,
                messages=messages, tools=tools.openai_tools(), tool_choice="auto",
                stream=True, stream_options={"include_usage": True})

            content_parts: list[str] = []
            tool_calls: dict[int, dict[str, str]] = {}
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage_in += chunk.usage.prompt_tokens or 0
                    usage_out += chunk.usage.completion_tokens or 0
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    content_parts.append(delta.content)
                    yield {"type": "delta", "text": delta.content}
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        slot = tool_calls.setdefault(
                            tc.index, {"id": "", "name": "", "args": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["args"] += tc.function.arguments

            if not tool_calls:
                reply_text = "".join(content_parts).strip()
                break

            ordered = [tool_calls[i] for i in sorted(tool_calls)]
            messages.append({
                "role": "assistant",
                "content": "".join(content_parts) or None,
                "tool_calls": [
                    {"id": c["id"] or f"call_{i}", "type": "function",
                     "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                    for i, c in enumerate(ordered)],
            })
            for i, c in enumerate(ordered):
                name = c["name"]
                try:
                    args = json.loads(c["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = tools.execute(name, args, ctx)
                found = result.get("found")
                trace.append({"tool": name, "input": args, "found": found})
                for source in result.get("sources") or []:
                    citations.append(source)
                conversations.add_message(session_id, "tool", None, channel=channel,
                                          tool_name=name, tool_input=args,
                                          tool_found=found)
                yield {"type": "tool", "label": name, "found": found}
                messages.append({
                    "role": "tool", "tool_call_id": c["id"] or f"call_{i}",
                    "content": json.dumps(result, ensure_ascii=False)[:12000]})
        else:
            # Tool budget exhausted: one final streamed answer, no tools.
            stream = client.chat.completions.create(
                model=config.CHAT_MODEL, temperature=config.CHAT_TEMPERATURE,
                messages=[*messages, {"role": "system",
                          "content": "Answer the visitor now, from what the tool "
                          "results above contain. Do not call any more tools."}],
                stream=True, stream_options={"include_usage": True})
            parts: list[str] = []
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage_in += chunk.usage.prompt_tokens or 0
                    usage_out += chunk.usage.completion_tokens or 0
                if chunk.choices and chunk.choices[0].delta \
                        and chunk.choices[0].delta.content:
                    piece = chunk.choices[0].delta.content
                    parts.append(piece)
                    yield {"type": "delta", "text": piece}
            reply_text = "".join(parts).strip()
    except Exception as exc:  # noqa: BLE001
        message_l = str(exc).lower()
        if "insufficient_quota" in message_l or "exceeded your current quota" in message_l:
            yield {"type": "error", "status": 503,
                   "detail": "The OpenAI account has run out of quota. Top it up at "
                             "platform.openai.com to restore the assistant."}
        elif "rate_limit" in message_l or "429" in str(exc):
            yield {"type": "error", "status": 429,
                   "detail": "Rate-limited by OpenAI; retry shortly."}
        else:
            yield {"type": "error", "status": 500,
                   "detail": f"{type(exc).__name__}: {exc}"}
        return

    result = _finalize(
        reply_text=reply_text, citations=citations, trace=trace, history=history,
        message=message, captured=captured, session_id=session_id,
        portal_key=portal_key, language=language, channel=channel,
        user_message_id=user_message_id, run_extraction=run_extraction,
        usage_in=usage_in, usage_out=usage_out, remaining=remaining)
    yield {"type": "done", **result}
