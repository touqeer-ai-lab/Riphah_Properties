"""Signature verification, staff passwords, and PII normalisation.

⚠️ **The normalisation functions here must stay byte-identical in behaviour to
`Riphah_Property_Chatbot/core/security.py`.**

This is the one place where duplicating code between the two services is a real
hazard rather than a mild one. Deduplication depends on both sides agreeing that
`0300 1234567` and `+92 300 1234567` are the same person. If the chatbot
normalises to `+923001234567` and the CRM to `923001234567`, every lead that
arrives by both webhook and pull becomes two rows, and the sales team calls the
same buyer twice.

`eval/test_parity.py` asserts the two implementations agree on a shared case list.
If you change one, change both and run that test.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

import config

_MIN_PASSWORD = 8


class WeakPassword(ValueError):
    pass


# ------------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    if len(password or "") < _MIN_PASSWORD:
        raise WeakPassword(f"Password must be at least {_MIN_PASSWORD} characters.")
    salt = secrets.token_bytes(16)
    iterations = config.PBKDF2_ITERATIONS
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join(["pbkdf2", str(iterations),
                     base64.b64encode(salt).decode(),
                     base64.b64encode(digest).decode()])


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode(),
                                     base64.b64decode(salt_b64), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(hash_b64))


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------- signature checking

def verify_chatbot_signature(body: bytes, timestamp: str | None,
                             signature: str | None) -> tuple[bool, str | None]:
    """Verify an inbound `lead.created` / `lead.updated` webhook.

    Returns (ok, reason_if_not) so the caller can record *why* a delivery was
    rejected. A silent 401 leaves whoever is debugging the integration with
    nothing — and the two likely causes (mismatched secret, clock skew) need
    completely different fixes.

    The timestamp is inside the signed string, not merely alongside it, so a
    captured request cannot be replayed later with a fresh header.
    """
    if not config.WEBHOOK_SECRET:
        return False, "WEBHOOK_SECRET is not configured on the CRM"
    if not signature:
        return False, "missing X-Riphah-Signature header"
    if not timestamp:
        return False, "missing X-Riphah-Timestamp header"
    try:
        age = abs(int(time.time()) - int(timestamp))
    except (TypeError, ValueError):
        return False, "malformed timestamp header"
    if age > config.WEBHOOK_MAX_AGE_SECONDS:
        return False, (f"timestamp is {age}s old (limit "
                       f"{config.WEBHOOK_MAX_AGE_SECONDS}s) — replay, or clock skew "
                       f"between the two services")

    expected = hmac.new(config.WEBHOOK_SECRET.encode(),
                        f"{timestamp}.".encode() + body,
                        hashlib.sha256).hexdigest()
    provided = signature.removeprefix("sha256=")
    if not hmac.compare_digest(expected, provided):
        return False, "signature mismatch — WEBHOOK_SECRET differs between services"
    return True, None


def verify_meta_signature(body: bytes, header: str | None) -> tuple[bool, str | None]:
    """Verify Meta's `X-Hub-Signature-256`.

    Meta signs the raw body with the app secret and no timestamp — a different
    scheme from the chatbot's, which is exactly why each source gets its own
    verifier rather than one generic one.
    """
    if not config.META_APP_SECRET:
        return False, "META_APP_SECRET is not configured (Meta source is pending)"
    if not header:
        return False, "missing X-Hub-Signature-256 header"
    expected = hmac.new(config.META_APP_SECRET.encode(), body,
                        hashlib.sha256).hexdigest()
    provided = header.removeprefix("sha256=")
    if not hmac.compare_digest(expected, provided):
        return False, "signature mismatch against META_APP_SECRET"
    return True, None


# ----------------------------------------------------------- PII normalisation
# KEEP IN SYNC with the chatbot. See the module docstring.

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")
_DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "yopmail.com", "trashmail.com", "sharklasers.com", "throwawaymail.com",
    "example.com", "test.com",
}


def normalise_email(email: str | None) -> str | None:
    if not email:
        return None
    cleaned = email.strip().strip(".,;:").lower()
    return cleaned if _EMAIL_RE.match(cleaned) else None


def is_disposable_email(email: str | None) -> bool:
    norm = normalise_email(email)
    return bool(norm) and norm.split("@", 1)[1] in _DISPOSABLE_DOMAINS


def normalise_phone(phone: str | None, *, default_country: str = "92") -> str | None:
    if not phone:
        return None
    digits = re.sub(r"[^\d+]", "", phone)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("+"):
        body = digits[1:]
    elif digits.startswith("0"):
        body = default_country + digits[1:]
    elif digits.startswith(default_country) and len(digits) >= 12:
        body = digits
    else:
        body = default_country + digits if len(digits) == 10 else digits

    if not body.isdigit() or not (10 <= len(body) <= 15):
        return None
    if body.startswith("92") and len(body) != 12:
        return None
    return "+" + body


def person_key(email: str | None, phone: str | None) -> str | None:
    """Stable cross-source identity for one human.

    Phone is preferred over email because it is the field a property sales team
    actually works from, and because one person routinely has several email
    addresses but rarely several mobile numbers. Returns None when neither is
    usable, which keeps un-contactable leads from all collapsing into one
    "person".
    """
    normalised_phone = normalise_phone(phone)
    if normalised_phone:
        return f"tel:{normalised_phone}"
    normalised_email = normalise_email(email)
    if normalised_email:
        return f"mail:{normalised_email}"
    return None
