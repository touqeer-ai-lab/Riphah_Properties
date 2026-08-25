"""Outbound communications: WhatsApp messages and SIP click-to-call.

Built on the same principle as the Meta lead source: the full path — endpoints,
UI buttons, activity logging, first-response tracking — exists and is exercised
now; only the credentials are missing. `status()` names exactly which variables
each channel is waiting on, so "pending" is information rather than a hole.

Two channels, and each has a fallback that works with no credentials at all:

- **WhatsApp** — live mode POSTs to the Business Cloud API
  (`graph.facebook.com/{version}/{phone_number_id}/messages`); the fallback is a
  `wa.me` deep link the browser opens, which reaches the same person through the
  consultant's own WhatsApp. Both are logged as contact attempts.
- **SIP** — live mode POSTs to the PBX's originate endpoint (Asterisk ARI, 3CX,
  Twilio, or any SIP trunk provider's REST bridge); the fallback is a `sip:`/
  `tel:` URI the consultant's softphone answers.

Every attempt — live or fallback — stamps `first_response_at` on the lead,
because the median-response analytic measures when sales first reached out, not
which transport happened to carry it.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

import config
from core import db


class CommsPending(Exception):
    """The channel is wired but has no credentials. `.missing` names them."""

    def __init__(self, channel: str, missing: list[str]):
        self.channel = channel
        self.missing = missing
        super().__init__(
            f"{channel} is pending — set {', '.join(missing)} in .env to go live."
        )


# ---------------------------------------------------------------------- status

def _whatsapp_missing() -> list[str]:
    return [name for name, value in (
        ("WHATSAPP_ACCESS_TOKEN", config.WHATSAPP_ACCESS_TOKEN),
        ("WHATSAPP_PHONE_NUMBER_ID", config.WHATSAPP_PHONE_NUMBER_ID),
    ) if not value]


def _sip_missing() -> list[str]:
    return [name for name, value in (
        ("SIP_ORIGINATE_URL", config.SIP_ORIGINATE_URL),
    ) if not value]


def status() -> dict[str, Any]:
    """Channel state for the UI — mirrors the shape of /api/sources."""
    wa_missing, sip_missing = _whatsapp_missing(), _sip_missing()
    return {
        "whatsapp": {
            "key": "whatsapp",
            "display_name": "WhatsApp Business API",
            "status": "pending" if wa_missing else "live",
            "missing_config": wa_missing,
            "detail": (
                "Send and log WhatsApp messages from the lead drawer. The token "
                "and phone-number id come from the same Meta app as lead ads "
                "(developers.facebook.com → WhatsApp → API Setup). Until then "
                "the button opens the consultant's own WhatsApp via wa.me — "
                "the attempt is still logged."
                if wa_missing else
                "Messages are sent through the Business Cloud API and logged "
                "to the lead's activity trail."
            ),
        },
        "sip": {
            "key": "sip",
            "display_name": "SIP click-to-call",
            "status": "pending" if sip_missing else "live",
            "missing_config": sip_missing,
            "detail": (
                "One click rings the consultant's extension, then dials the "
                "lead. Point SIP_ORIGINATE_URL at the PBX's originate endpoint "
                "(Asterisk ARI, 3CX, Twilio or a trunk provider's REST bridge). "
                "Until then the button opens the consultant's softphone via a "
                "sip:/tel: link — the attempt is still logged."
                if sip_missing else
                "Calls are originated through the PBX and logged to the lead's "
                "activity trail."
            ),
        },
    }


# ----------------------------------------------------------------- normalising

def dialable(phone: str | None) -> str | None:
    """E.164-ish digits for wa.me and PBX APIs. Pakistani local numbers get the
    country code, because '0333 1112223' dialled internationally reaches nobody."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "92" + digits[1:]
    return digits


# ------------------------------------------------------------------- recording

def _record_attempt(lead: dict[str, Any], *, channel: str, actor: str,
                    mode: str, detail: dict[str, Any] | None = None) -> None:
    """Log the attempt and stamp first_response_at.

    The stamp uses COALESCE so only the *first* outreach sets it — the
    median-response analytic measures time-to-first-touch, and a later call
    must not overwrite the honest earlier timestamp.
    """
    stamp = db.now()
    with db.tx() as conn:
        conn.execute(
            "UPDATE leads SET first_response_at = COALESCE(first_response_at, ?), "
            "updated_at = ? WHERE id = ?",
            (stamp, stamp, lead["id"]),
        )
    db.log_activity(lead["id"], actor, "contacted",
                    {"channel": channel, "mode": mode, **(detail or {})})


# -------------------------------------------------------------------- whatsapp

def send_whatsapp(lead: dict[str, Any], message: str, *, actor: str) -> dict[str, Any]:
    """Send a WhatsApp message through the Business Cloud API."""
    to = dialable(lead.get("phone"))
    if not to:
        raise ValueError("This lead has no phone number.")
    missing = _whatsapp_missing()
    if missing:
        raise CommsPending("WhatsApp Business API", missing)

    url = (f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/"
           f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }
    with httpx.Client(timeout=20) as client:
        response = client.post(url, json=payload, headers={
            "Authorization": f"Bearer {config.WHATSAPP_ACCESS_TOKEN}"})
    if response.status_code >= 300:
        # The Graph error body says *why* (expired token, unopened 24h window,
        # unverified number) — surfacing it beats a generic failure.
        try:
            reason = response.json().get("error", {}).get("message", response.text)
        except Exception:  # noqa: BLE001
            reason = response.text
        raise RuntimeError(f"WhatsApp API refused the message: {reason}")

    message_id = None
    try:
        message_id = (response.json().get("messages") or [{}])[0].get("id")
    except Exception:  # noqa: BLE001
        pass
    _record_attempt(lead, channel="whatsapp", actor=actor, mode="api",
                    detail={"message_id": message_id, "preview": message[:120]})
    return {"ok": True, "mode": "api", "to": to, "message_id": message_id}


def log_whatsapp_link(lead: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """The pending-mode fallback: the browser opened wa.me; record the attempt."""
    to = dialable(lead.get("phone"))
    if not to:
        raise ValueError("This lead has no phone number.")
    _record_attempt(lead, channel="whatsapp", actor=actor, mode="wa.me")
    return {"ok": True, "mode": "wa.me", "to": to,
            "link": f"https://wa.me/{to}"}


# ------------------------------------------------------------------------ call

def originate_call(lead: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """Ask the PBX to bridge the consultant and the lead."""
    to = dialable(lead.get("phone"))
    if not to:
        raise ValueError("This lead has no phone number.")
    missing = _sip_missing()
    if missing:
        raise CommsPending("SIP click-to-call", missing)

    headers = {}
    if config.SIP_ORIGINATE_TOKEN:
        headers["Authorization"] = f"Bearer {config.SIP_ORIGINATE_TOKEN}"
    payload = {
        "to": f"+{to}",
        "caller_id": config.SIP_CALLER_ID or None,
        "lead_ref": lead.get("external_id"),
        "agent": actor,
    }
    with httpx.Client(timeout=20) as client:
        response = client.post(config.SIP_ORIGINATE_URL, json=payload,
                               headers=headers)
    if response.status_code >= 300:
        raise RuntimeError(
            f"The PBX refused the originate request "
            f"({response.status_code}): {response.text[:300]}")

    _record_attempt(lead, channel="call", actor=actor, mode="sip-originate")
    return {"ok": True, "mode": "sip-originate", "to": f"+{to}"}


def log_call_link(lead: dict[str, Any], *, actor: str) -> dict[str, Any]:
    """The pending-mode fallback: the browser opened sip:/tel:; record it."""
    to = dialable(lead.get("phone"))
    if not to:
        raise ValueError("This lead has no phone number.")
    _record_attempt(lead, channel="call", actor=actor, mode="softphone-link")
    return {"ok": True, "mode": "softphone-link", "to": f"+{to}",
            "sip": f"sip:+{to}", "tel": f"tel:+{to}"}
