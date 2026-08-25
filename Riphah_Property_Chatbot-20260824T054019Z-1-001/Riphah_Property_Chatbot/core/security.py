"""Password hashing, token minting, HMAC signing, and PII normalisation.

Stdlib only. PBKDF2-SHA256 rather than bcrypt/argon2 so there is no native build
step in the deployment; the iteration count is in config and is the thing to
raise over time, not the algorithm.

Normalisation lives here rather than in `leads/` because two callers need to
agree exactly: the deduplicator and the outbound payload builder. If they
disagree about whether "0300 1234567" is the same person as "+92 300 1234567",
the CRM gets duplicates.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

import config

# --------------------------------------------------------------------- passwords

_MIN_PASSWORD = 8


class WeakPassword(ValueError):
    pass


def hash_password(password: str) -> str:
    """`pbkdf2$<iterations>$<salt_b64>$<hash_b64>` — self-describing, so raising
    the iteration count later doesn't invalidate existing hashes."""
    if len(password or "") < _MIN_PASSWORD:
        raise WeakPassword(f"Password must be at least {_MIN_PASSWORD} characters.")
    salt = secrets.token_bytes(16)
    iterations = config.PBKDF2_ITERATIONS
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join([
        "pbkdf2",
        str(iterations),
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    ])


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode(),
            base64.b64decode(salt_b64), int(iterations),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(hash_b64))


def needs_rehash(encoded: str) -> bool:
    """True when a stored hash predates the current iteration count.

    Called on successful login so hashes migrate forward as the constant rises,
    without forcing a password reset.
    """
    try:
        _, iterations, _, _ = encoded.split("$")
        return int(iterations) < config.PBKDF2_ITERATIONS
    except (ValueError, TypeError):
        return True


# ------------------------------------------------------------------------ tokens

def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    """Sessions and API keys are stored hashed, so a DB dump yields no live
    credentials. Plain SHA-256 is correct here — the input is already
    high-entropy, so a slow KDF would only add latency."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_api_key() -> tuple[str, str, str]:
    """Returns (key, key_hash, prefix). The key is shown to the operator once."""
    raw = "rip_" + secrets.token_urlsafe(32)
    return raw, token_hash(raw), raw[:12]


def hash_ip(ip: str | None) -> str | None:
    """Rate limiting needs to distinguish visitors; it does not need their address.

    Salted with the webhook secret when one exists so the hashes are not a
    rainbow-table lookup away from the original IPs.
    """
    if not ip:
        return None
    salt = config.WEBHOOK_SECRET or "riphah-static-salt"
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:32]


# ------------------------------------------------------------------ HMAC signing

def sign_payload(body: bytes, *, secret: str | None = None,
                 timestamp: str | None = None) -> dict[str, str]:
    """Signature headers for an outbound webhook (spec s9.2).

    The timestamp is inside the signed string, not just alongside it, so a
    captured request cannot be replayed later with a fresh timestamp header.
    """
    key = secret if secret is not None else config.WEBHOOK_SECRET
    if not key:
        raise RuntimeError("WEBHOOK_SECRET is not configured; refusing to send unsigned")
    ts = timestamp or str(int(__import__("time").time()))
    signed = f"{ts}.".encode() + body
    mac = hmac.new(key.encode(), signed, hashlib.sha256).hexdigest()
    return {
        "X-Riphah-Timestamp": ts,
        "X-Riphah-Signature": f"sha256={mac}",
    }


def verify_signature(body: bytes, timestamp: str, signature: str, *,
                     secret: str, max_age_seconds: int = 300) -> bool:
    """Receiver-side check. Used by the CRM; kept here so both sides share code."""
    import time

    try:
        age = abs(int(time.time()) - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > max_age_seconds:
        return False
    expected = hmac.new(secret.encode(),
                        f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    provided = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


# ----------------------------------------------------------- PII normalisation

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")
# Obvious throwaways. Not exhaustive and not meant to be — it exists to stop the
# most common junk from being scored as a contactable lead.
_DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "yopmail.com", "trashmail.com", "sharklasers.com", "throwawaymail.com",
    "example.com", "test.com",
}


def normalise_email(email: str | None) -> str | None:
    if not email:
        return None
    cleaned = email.strip().strip(".,;:").lower()
    if not _EMAIL_RE.match(cleaned):
        return None
    return cleaned


def is_disposable_email(email: str | None) -> bool:
    norm = normalise_email(email)
    return bool(norm) and norm.split("@", 1)[1] in _DISPOSABLE_DOMAINS


def normalise_phone(phone: str | None, *, default_country: str = "92") -> str | None:
    """Pakistani mobile numbers to E.164.

    Handles the four forms people actually type: `0300 1234567`,
    `+92 300 1234567`, `92 300 1234567`, and the bare `3001234567`. Returns None
    for anything that isn't a plausible number rather than guessing — a wrong
    phone number in a CRM costs a sales call.
    """
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
        # Bare local number: only treat it as PK if the length fits a mobile.
        body = default_country + digits if len(digits) == 10 else digits

    if not body.isdigit() or not (10 <= len(body) <= 15):
        return None
    # A PK mobile is 92 + 3XXXXXXXXX. Reject 92-prefixed values of the wrong
    # length so "923" doesn't survive as a contactable number.
    if body.startswith("92") and len(body) != 12:
        return None
    return "+" + body


def looks_like_real_name(name: str | None) -> bool:
    """Cheap junk filter for the name field. Two letters, no digits, not a URL."""
    if not name:
        return False
    cleaned = name.strip()
    if len(cleaned) < 2 or len(cleaned) > 80:
        return False
    if any(ch.isdigit() for ch in cleaned):
        return False
    return bool(re.match(r"^[\w\s.'\-À-ɏ؀-ۿ]+$", cleaned, re.UNICODE))
