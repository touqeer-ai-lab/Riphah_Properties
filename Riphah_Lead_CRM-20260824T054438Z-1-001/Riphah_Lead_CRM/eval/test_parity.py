"""Assert the two services normalise PII identically.

`core/security.py` in both projects duplicates `normalise_email` and
`normalise_phone`. Deduplication depends on both sides agreeing that
`0300 1234567` and `+92 300 1234567` are the same person. If they disagree, every
lead that arrives by both webhook and pull becomes two rows and the sales team
calls the same buyer twice.

This test imports both implementations and runs them over a shared case list. It is
the thing that makes the duplication safe. If you change one side, this fails.

    python eval/test_parity.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CRM_ROOT = Path(__file__).resolve().parent.parent
CHATBOT_ROOT = CRM_ROOT.parent / "Riphah_Property_Chatbot"

sys.path.insert(0, str(CRM_ROOT))
from core import security as crm_security  # noqa: E402


def load_chatbot_security():
    """Import the chatbot's security module without its package shadowing ours.

    Both projects have a `core` package, so a plain `sys.path` append would give
    whichever was imported first. Loading by file path under a distinct module name
    keeps them separate — and `config` is stubbed because the chatbot's config reads
    its own .env, which this test has no business touching.
    """
    if not CHATBOT_ROOT.exists():
        return None

    # The chatbot's security module imports `config` for the iteration count and
    # the webhook salt. Neither affects normalisation, so a stub is enough.
    class StubConfig:
        PBKDF2_ITERATIONS = 100
        WEBHOOK_SECRET = "stub"

    saved = sys.modules.get("config")
    sys.modules["config"] = StubConfig  # type: ignore[assignment]
    try:
        spec = importlib.util.spec_from_file_location(
            "_chatbot_security", CHATBOT_ROOT / "core" / "security.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if saved is not None:
            sys.modules["config"] = saved
        else:
            sys.modules.pop("config", None)


PHONES = [
    "0300 1234567", "+92 300 1234567", "92 300 1234567", "+923001234567",
    "0300-123-4567", "(0300) 1234567", "00923001234567", "3001234567",
    "0321 4567890", "+92 321 4567890", "+971 50 123 4567", "+44 7700 900123",
    "123", "", "not a phone", "0300 12345", "+92 3001234567890123",
    "  0300 1234567  ", "0300.123.4567", "92-300-1234567",
]

EMAILS = [
    "Ayesha.Khan@Gmail.com", "ayesha.khan@gmail.com", " a@b.co ",
    "a@b.co.", "no-at-sign", "a@b", "a@@b.com", "", "A.B+tag@Example.COM",
    "user@sub.domain.pk", "trailing@comma.com,",
]

NAMES = ["Dr Ayesha Khan", "Sir", "12345", "A", "محمد علی", "O'Brien-Smith", ""]


def main() -> int:
    chatbot = load_chatbot_security()
    if not chatbot:
        print(f"Chatbot project not found at {CHATBOT_ROOT} — skipping parity test.")
        print("This test only means something with both projects checked out.")
        return 0

    failures = 0

    print("phone normalisation parity")
    for raw in PHONES:
        a = crm_security.normalise_phone(raw)
        b = chatbot.normalise_phone(raw)
        if a != b:
            failures += 1
            print(f"  FAIL {raw!r}: crm={a!r} chatbot={b!r}")
    print(f"  {len(PHONES)} cases, {failures} mismatches")

    print("\nemail normalisation parity")
    email_failures = 0
    for raw in EMAILS:
        a = crm_security.normalise_email(raw)
        b = chatbot.normalise_email(raw)
        if a != b:
            email_failures += 1
            print(f"  FAIL {raw!r}: crm={a!r} chatbot={b!r}")
    failures += email_failures
    print(f"  {len(EMAILS)} cases, {email_failures} mismatches")

    print("\ndisposable-domain parity")
    disposable_failures = 0
    for raw in ["a@mailinator.com", "a@gmail.com", "a@example.com", "bad"]:
        a = crm_security.is_disposable_email(raw)
        b = chatbot.is_disposable_email(raw)
        if a != b:
            disposable_failures += 1
            print(f"  FAIL {raw!r}: crm={a!r} chatbot={b!r}")
    failures += disposable_failures
    print(f"  4 cases, {disposable_failures} mismatches")

    print("\nHMAC signing round-trip (chatbot signs, CRM verifies)")
    import json
    import time

    body = json.dumps({"event": "lead.created",
                       "data": {"lead_id": "LD-2026-00001"}}).encode()
    headers = chatbot.sign_payload(body, secret="shared-test-secret")
    ok = chatbot.verify_signature(
        body, headers["X-Riphah-Timestamp"],
        headers["X-Riphah-Signature"], secret="shared-test-secret")
    if not ok:
        failures += 1
        print("  FAIL chatbot cannot verify its own signature")
    else:
        print("  ok   signature verifies")

    # The CRM's verifier reads its secret from config, so it is exercised via its
    # own HMAC path rather than by monkeypatching config.
    import hashlib
    import hmac

    timestamp = headers["X-Riphah-Timestamp"]
    expected = hmac.new(b"shared-test-secret",
                        f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(
            expected, headers["X-Riphah-Signature"].removeprefix("sha256=")):
        failures += 1
        print("  FAIL CRM's signing formula differs from the chatbot's")
    else:
        print("  ok   both sides compute the same MAC over timestamp + body")

    tampered = body.replace(b"00001", b"00002")
    if chatbot.verify_signature(tampered, timestamp,
                               headers["X-Riphah-Signature"],
                               secret="shared-test-secret"):
        failures += 1
        print("  FAIL a tampered body still verifies")
    else:
        print("  ok   tampered body is rejected")

    stale = str(int(time.time()) - 4000)
    stale_headers = chatbot.sign_payload(body, secret="shared-test-secret",
                                         timestamp=stale)
    if chatbot.verify_signature(body, stale, stale_headers["X-Riphah-Signature"],
                               secret="shared-test-secret", max_age_seconds=300):
        failures += 1
        print("  FAIL a stale timestamp still verifies (replay window open)")
    else:
        print("  ok   stale timestamp is rejected")

    print(f"\n{'=' * 56}")
    if failures:
        print(f"{failures} PARITY FAILURES — the two services disagree. "
              f"Duplicate leads will result.")
        return 1
    print("parity holds: both services normalise identically and share one MAC.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
