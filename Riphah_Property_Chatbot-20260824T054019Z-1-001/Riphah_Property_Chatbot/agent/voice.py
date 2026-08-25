"""Voice: OpenAI Realtime over WebRTC, with a transcribe/speak fallback.

Two paths, because they fail differently.

**Realtime (preferred).** Speech in, speech out, ~500 ms turns, native language
switching. Audio and the data channel run directly browser↔OpenAI, so function
calls surface in the browser — which is why the browser posts them back to
`/api/tools/{name}` and returns the result over its data channel. The browser
never holds the API key, only a short-lived credential minted here with the
instructions and tools already baked in, so a client cannot weaken the guardrails
by editing its own session.

**Transcribe + speak (fallback).** Whisper for speech→text, the text chat path,
TTS for text→speech. Three round-trips instead of one, so it is noticeably slower
— but it works on browsers without WebRTC, behind restrictive corporate proxies,
and when the Realtime model is unavailable. It also reuses the text path exactly,
which means the guardrails and lead capture are identical rather than
reimplemented.
"""
from __future__ import annotations

from typing import Any

import config
from agent import conversations, prompts, tools
from portals import registry


def _client():
    from openai import OpenAI

    return OpenAI(api_key=config.openai_key())


# --------------------------------------------------------------- realtime path

async def mint_session(*, portal_key: str, session_id: str,
                       voice: str | None = None,
                       language_hint: str | None = None) -> dict[str, Any]:
    """Mint an ephemeral Realtime credential for the browser.

    Instructions and tools are set server-side. The credential is short-lived by
    design, so a leaked one is a bounded problem rather than a billing incident.
    """
    import httpx

    portal = registry.get(portal_key)
    api_key = config.openai_key()

    extras: list[str] = [prompts.greeting_instruction(portal)]
    if language_hint:
        extras.append(
            f"The visitor is expected to start in language code '{language_hint}'. "
            f"Follow whatever they actually speak."
        )
    # The Realtime API keeps no state across connections, so a reconnect needs the
    # prior thread replayed into its instructions or it restarts the conversation.
    resumed = prompts.resume_block(conversations.history(session_id, limit=12))
    if resumed:
        extras.append(resumed)

    captured = _captured(session_id)
    session_body: dict[str, Any] = {
        "type": "realtime",
        "model": config.REALTIME_MODEL,
        "instructions": prompts.system_prompt(
            portal,
            captured=captured,
            outstanding=_outstanding(portal_key, captured),
            turn_count=conversations.turn_count(session_id),
            has_contact=_has_contact(captured),
            channel="voice",
            extra="\n\n".join(extras),
        ),
        "tools": tools.realtime_tools(),
        "tool_choice": "auto",
        "audio": {
            "input": {
                # Transcription feeds the on-screen transcript and the stored
                # history. `language` is deliberately unset — pinning it would
                # break mid-call switching between Urdu and English.
                "transcription": {"model": config.TRANSCRIBE_MODEL},
                "turn_detection": {"type": "semantic_vad"},
            },
            "output": {"voice": voice or config.REALTIME_VOICE},
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # Bound to the token server-side, so the browser never sends it.
                "OpenAI-Safety-Identifier": f"riphah-{portal_key}",
            },
            json={"session": session_body},
        )

    if response.status_code >= 400:
        # Surfaced verbatim: OpenAI names the offending field, which is exactly
        # what you need when a session key is renamed upstream.
        raise RuntimeError(f"OpenAI rejected the session: {response.text[:600]}")

    data = response.json()
    return {
        "client_secret": (
            data.get("value") or data.get("client_secret", {}).get("value")
        ),
        "expires_at": data.get("expires_at"),
        "model": config.REALTIME_MODEL,
        "sdp_url": "https://api.openai.com/v1/realtime/calls",
        "session_id": session_id,
        "resumed": bool(resumed),
    }


# --------------------------------------------------------------- fallback path

# Whisper rejects an unknown extension, and browsers hand over whatever their
# MediaRecorder produced. Mapping the common content types keeps the fallback
# working on Safari (mp4) as well as Chrome and Firefox (webm/ogg).
_AUDIO_EXTENSIONS = {
    "audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav",
    "audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/mp4": "mp4",
    "audio/m4a": "m4a", "audio/x-m4a": "m4a", "video/webm": "webm",
}


def transcribe(audio: bytes, *, content_type: str | None = None,
               language: str | None = None) -> dict[str, Any]:
    """Speech -> text. Language is left unset unless the caller pins it."""
    extension = _AUDIO_EXTENSIONS.get((content_type or "").split(";")[0], "webm")
    kwargs: dict[str, Any] = {}
    if language:
        kwargs["language"] = language

    result = _client().audio.transcriptions.create(
        model=config.TRANSCRIBE_MODEL,
        file=(f"audio.{extension}", audio, content_type or "audio/webm"),
        **kwargs,
    )
    return {
        "text": (getattr(result, "text", "") or "").strip(),
        "language": getattr(result, "language", None) or language,
    }


def speak(text: str, *, voice: str | None = None) -> bytes:
    """Text -> speech, as MP3 bytes.

    Markdown is stripped first. The reply the visitor reads is styled for a chat
    window; handing its asterisks and pipe characters to a TTS model gets them
    read aloud.
    """
    import re

    clean = re.sub(r"^\s*\|.*\|\s*$", " ", text, flags=re.MULTILINE)
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)
    clean = re.sub(r"[*_#`>]+", "", clean)
    clean = re.sub(r"^\s*[-•]\s*", "", clean, flags=re.MULTILINE)
    clean = " ".join(clean.split())[:4000]

    response = _client().audio.speech.create(
        model=config.TTS_MODEL,
        voice=voice or config.REALTIME_VOICE,
        input=clean or "Sorry, there was nothing to read out.",
        response_format="mp3",
    )
    return response.read()


# ------------------------------------------------------------------- helpers

def _captured(session_id: str) -> dict[str, Any]:
    from leads import store

    return store.captured_for_session(session_id)


def _outstanding(portal_key: str, captured: dict[str, Any]) -> list[dict[str, Any]]:
    from leads import store

    return store.outstanding_for(portal_key, captured)


def _has_contact(captured: dict[str, Any]) -> bool:
    from leads import store

    return store.has_contact(captured)
