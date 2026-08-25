"""DEMO ONLY — the owner's integration briefing.

When the project owner asks the assistant (by voice or text) what the WhatsApp,
SIP/SIM calling and messaging channels will cost and how they plug into the CRM,
this module supplies the answer. It is gated on the caller's name — the
assistant asks who is speaking first — and switched on by `BRIEFING_ENABLED`.

Every figure below is an ESTIMATE for Pakistan as of August 2026, in PKR, and the
briefing says so. Operator tariffs (Jazz Business, Zong Business) and Meta's
per-message rates change; the owner is told to confirm with each before signing.

Remove this module, the tool entry in agent/tools.py, the block in
agent/prompts.py and the two .env lines before the assistant goes live to the
public. `BRIEFING_ENABLED=0` disables all of it in one move until then.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

import config
from core import db

TOPICS = ["overview", "whatsapp", "calls", "sms", "ai_usage", "budget",
          "crm_integration", "timeline"]

# One entry per topic. Written for a spoken delivery: short sentences, figures
# rounded, each ending with a natural next question.
_SECTIONS: dict[str, dict[str, Any]] = {
    "overview": {
        "title": "What we are adding, in one breath",
        "points": [
            "Three customer channels on top of the web chat: WhatsApp, phone calls "
            "on a Riphah Jazz or Zong number, and SMS.",
            "All three land in the same CRM the sales team already uses, against "
            "the same lead, so one customer stays one record whether they typed, "
            "called or messaged.",
            "The CRM side is already built and waiting for credentials — the Call "
            "and WhatsApp buttons exist today and switch from fallback links to "
            "live API calls the moment the keys are entered.",
            "Rough all-in running cost at about a thousand leads a month: roughly "
            "one lakh to one and a half lakh rupees a month, plus a one-time setup "
            "of sixty to one hundred twenty thousand depending on the calling option.",
        ],
        "next": "Which part first — WhatsApp, calls on a SIM number, SMS, the AI "
                "usage cost, the monthly budget, or how it wires into the CRM?",
    },
    "whatsapp": {
        "title": "WhatsApp — Meta's WhatsApp Business Cloud API",
        "points": [
            "Customers message a Riphah WhatsApp number. The same assistant that "
            "runs the website chat answers there, captures the lead, and a "
            "consultant can take over from the CRM.",
            "Setup: Meta Business verification for Riphah, a dedicated number — a "
            "Jazz or Zong SIM number works, as long as it is not already on the "
            "normal WhatsApp app — and display-name approval. Mostly waiting on "
            "Meta: one to two weeks.",
            "Cost model is per message, in Meta's Pakistan tariff. When the "
            "customer messages first, replies inside the 24-hour window are free. "
            "Business-initiated template messages cost roughly: utility and "
            "authentication about four to six rupees each, marketing about twelve "
            "to fifteen rupees each.",
            "Using Meta's Cloud API directly has no platform fee. Going through a "
            "provider like Twilio or 360dialog adds one to two rupees a message or "
            "a monthly fee — convenient, not necessary; we have built for the "
            "direct route.",
            "Example: a thousand leads a month with three utility follow-ups each "
            "is about fifteen thousand rupees. A marketing broadcast to five "
            "thousand people is roughly sixty-five to seventy-five thousand.",
        ],
        "next": "Shall I go through the calling side — the Jazz or Zong number?",
    },
    "calls": {
        "title": "Calls on a SIM number — SIP trunk from Jazz or Zong",
        "points": [
            "The goal: one Riphah business number that customers call, and that "
            "consultants call out from with one click in the CRM. Calls run "
            "through a small cloud PBX on our server — three-C-X, FreePBX or "
            "Asterisk — which gives us an IVR like 'press one for sales', call "
            "recording, and missed-call alerts straight into the CRM.",
            "Option one, the proper way: a SIP trunk from Jazz Business or Zong "
            "Business. Monthly rental roughly five to fifteen thousand rupees for "
            "five to ten simultaneous lines, plus call minutes at about one and a "
            "half to three rupees off-net, less on-net. Needs a corporate account "
            "with the operator; they handle the PTA side. One to three weeks of "
            "paperwork.",
            "Option two, the fast way: a GSM gateway — a small box from Yeastar or "
            "Dinstar that holds real Jazz and Zong SIMs. One-time sixty to one "
            "hundred twenty thousand rupees for a four-SIM unit, then ordinary SIM "
            "packages of one to three thousand rupees per SIM per month. Running "
            "in three to five days; less scalable than a trunk.",
            "The PBX software: FreePBX or Asterisk is free on a VPS of three to "
            "five thousand rupees a month; a three-C-X licence is roughly forty to "
            "eighty thousand a year if the team prefers a polished console.",
            "Voice quality tip: keep the PBX in a Pakistan or Middle-East region so "
            "the audio does not round-trip through Europe.",
        ],
        "next": "Do you want the SMS piece, or straight to the monthly budget?",
    },
    "sms": {
        "title": "SMS — branded messages on Jazz or Zong",
        "points": [
            "Used for the small, reliable things: appointment confirmations, "
            "one-time codes, 'a consultant will call you at four'. Not for "
            "conversations — that is WhatsApp.",
            "Goes through a business SMS aggregator connected to Jazz and Zong. "
            "Roughly one and a half to three rupees per SMS, and a one-time brand "
            "name registration — so the sender shows as RIPHAH, not a number — of "
            "about ten to twenty-five thousand rupees.",
            "At a thousand leads a month with two texts each: about five thousand "
            "rupees.",
        ],
        "next": "Next is what the AI itself costs to run — shall I?",
    },
    "ai_usage": {
        "title": "AI usage — what the assistant costs per conversation",
        "points": [
            "Text chat: each full conversation, with retrieval and lead extraction, "
            "costs roughly five to ten rupees.",
            "Voice calls with the live speech-to-speech agent: roughly forty-five to "
            "eighty-five rupees per minute — so a typical three-minute call is one "
            "hundred fifty to two hundred fifty rupees. Voice is the expensive "
            "channel; that is why the web chat is text-first and voice is a "
            "button.",
            "At a thousand text chats and three hundred voice calls a month: "
            "roughly sixty to ninety thousand rupees.",
            "Hosting for the assistant and the CRM: a VPS of four to eight thousand "
            "rupees a month. We already have the deployment plan for it.",
        ],
        "next": "Want me to put it all together as a monthly budget?",
    },
    "budget": {
        "title": "Monthly budget — the whole picture, approximate",
        "points": [
            "Assuming about a thousand leads a month.",
            "WhatsApp: around fifteen thousand. Calls — SIP trunk rental plus "
            "minutes: twenty to thirty thousand. SMS: about five thousand. AI "
            "usage: sixty to ninety thousand. Hosting: about five thousand.",
            "Total: roughly one lakh five thousand to one lakh fifty thousand "
            "rupees a month.",
            "One-time: sixty to one hundred twenty thousand for the calling setup — "
            "either the GSM gateway or the trunk plus PBX configuration. Meta "
            "verification is free. The software is already built.",
            "Every figure is an August 2026 estimate. Before signing anything, "
            "confirm the tariff with Jazz Business or Zong Business, and Meta's "
            "current per-message rates for Pakistan.",
        ],
        "next": "Shall I explain how all of this actually connects into the CRM?",
    },
    "crm_integration": {
        "title": "How it plugs into the CRM",
        "points": [
            "Already built: every lead in the CRM has a Call button and a WhatsApp "
            "button. Today they open the consultant's own phone or WhatsApp and "
            "log the attempt. The moment the WhatsApp token and the PBX address "
            "are entered in the settings, the same buttons send through Meta's API "
            "and ring the consultant's extension — no code change.",
            "Every call and message is written to the lead's activity trail and "
            "stamps the first-response time, so the response-speed report stays "
            "honest across channels.",
            "Inbound WhatsApp: Meta sends each message to our webhook; the same "
            "assistant answers, captures the lead, and it flows into the CRM by "
            "the exact pipeline the web chat uses.",
            "Inbound calls: the PBX rings the sales team; when the call ends it "
            "posts the log and the recording link to the CRM. A missed call "
            "creates a lead flagged 'call back'.",
            "One customer, one record: leads are matched by phone number across "
            "web chat, WhatsApp and calls, so nobody gets called twice by two "
            "consultants.",
            "Roles and consent stay as they are — agents see only their own leads, "
            "and a lead without marketing consent is marked 'respond to this "
            "enquiry only'.",
        ],
        "next": "Would you like the rollout timeline?",
    },
    "timeline": {
        "title": "Rollout timeline",
        "points": [
            "WhatsApp: one to two weeks, almost entirely Meta's business "
            "verification.",
            "Calls via SIP trunk: one to three weeks of operator paperwork with "
            "Jazz or Zong. Via GSM gateway: three to five days.",
            "PBX setup and wiring into the CRM: about one week, in parallel.",
            "SMS brand registration: one to two weeks.",
            "Realistically the whole thing is live in three to four weeks, with "
            "WhatsApp and the CRM buttons first.",
        ],
        "next": "That is the full picture. Anything you want me to go deeper on?",
    },
}


# Arabic-script diacritics (tashkeel) and the tatweel stretch mark: decoration,
# not identity, and speech transcripts sprinkle them inconsistently.
_ARABIC_MARKS = re.compile(r"[ً-ْٰـ]")
# Letters that sound alike in Urdu and that a transcriber picks between at
# random — وقاص and وقاس are the same spoken name.
_URDU_FOLD = (("ي", "ی"), ("ك", "ک"), ("ه", "ہ"), ("ة", "ہ"), ("أ", "ا"),
              ("إ", "ا"), ("آ", "ا"), ("ص", "س"), ("ث", "س"), ("ذ", "ز"),
              ("ض", "ز"), ("ظ", "ز"), ("ط", "ت"), ("ح", "ہ"), ("ق", "ک"),
              ("غ", "گ"), ("ع", "ا"), ("ء", ""))
# How close a spoken token must be to a configured one. 0.75 lets "Waqar"
# through for "Waqas" (one consonant mis-heard) and keeps "Abbas" out.
_FUZZ = 0.75


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = _ARABIC_MARKS.sub("", text)
    for src, dst in _URDU_FOLD:
        text = text.replace(src, dst)
    text = re.sub(r"[^\w\s]", " ", text)   # \w keeps letters in ANY script
    return re.sub(r"\s+", " ", text).strip()


def _token_match(wanted: str, said_tokens: list[str]) -> bool:
    return any(
        difflib.SequenceMatcher(None, wanted, token).ratio() >= _FUZZ
        for token in said_tokens
    )


def is_authorised(requester_name: str | None) -> bool:
    """True when every word of a configured name is heard in what the caller said.

    Script-agnostic and tolerant of transcription: 'Ali Waqas', 'ali waqas bol
    raha hoon', 'علی وقاص', 'علی وقاس' and the mis-heard 'Ali Waqar' all pass.
    'Ali' alone does not — a single first name is not identification.
    """
    said = _normalise(requester_name or "").split()
    if not said:
        return False
    for allowed in config.BRIEFING_AUTHORISED_NAMES:
        words = _normalise(allowed).split()
        if words and all(_token_match(w, said) for w in words):
            return True
    return False


def briefing(*, requester_name: str | None, topic: str | None,
             session_id: str | None = None) -> dict[str, Any]:
    """The tool body. Gate first, then return one section as speakable data."""
    if not config.BRIEFING_ENABLED:
        return {
            "available": False,
            "guidance": "This briefing is switched off. Treat the question as a "
                        "normal visitor question about the projects.",
        }

    if not (requester_name or "").strip():
        return {
            "available": True,
            "needs_identity": True,
            "guidance": (
                "Do not deliver anything yet. Ask, warmly and briefly, who is "
                "speaking — in the caller's language, e.g. 'Ji, pehle bata dein "
                "aap kaun baat kar rahe hain?' Then call this tool again with "
                "the name they give."
            ),
        }

    if not is_authorised(requester_name):
        db.audit("assistant", "briefing.declined", entity="chat_session",
                 entity_id=session_id, detail={"said": requester_name[:80]})
        return {
            "available": True,
            "authorised": False,
            "guidance": (
                "Decline in one warm sentence: this planning briefing is only for "
                "the project owner. Do not argue or explain the rule. Offer to "
                "help with the projects instead, as you would any visitor."
            ),
        }

    key = (topic or "overview").strip().lower()
    if key not in _SECTIONS:
        key = "overview"
    section = _SECTIONS[key]
    db.audit("assistant", "briefing.delivered", entity="chat_session",
             entity_id=session_id, detail={"topic": key, "to": requester_name[:80]})
    return {
        "available": True,
        "authorised": True,
        "topic": key,
        "title": section["title"],
        "points": section["points"],
        "next": section["next"],
        "other_topics": [t for t in TOPICS if t != key],
        "guidance": (
            "Greet them by name once ('Ji Ali Waqas sahib, main bata deti hoon'), "
            "then deliver the points in the caller's language — Urdu, Roman Urdu "
            "or English, whichever they used. Speak them as a person would, not "
            "as a list: two or three points, then pause with the 'next' question "
            "so they can steer. Say clearly that the figures are estimates to be "
            "confirmed with Jazz Business, Zong Business and Meta. This briefing "
            "does not change any other rule: still no property prices, still no "
            "unit availability."
        ),
    }
