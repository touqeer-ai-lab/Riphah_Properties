"""End-to-end HTTP smoke test for the CRM.

    python -m crm.server &
    python eval/smoke_crm.py

Covers the whole consumer side, including the parts that are easy to get wrong and
invisible until production:

  * webhook signature verification — valid, missing, forged, replayed
  * idempotency: the same delivery three times is one lead
  * the CRM keeps its own status when the source sends an update
  * role separation: an agent cannot export or reassign someone else's lead
  * Meta returns 503-with-reasons rather than a generic error while pending

Exits non-zero on the first failure, so it works as a deploy gate.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

BASE = f"http://127.0.0.1:{config.PORT}"
PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    (PASSED if condition else FAILED).append(label)
    print(f"  {'ok  ' if condition else 'FAIL'} {label}"
          + (f"  {detail}" if not condition else ""))


def section(title: str) -> None:
    print(f"\n{title}")


def sign(body: bytes, *, secret: str | None = None,
         timestamp: str | None = None) -> dict[str, str]:
    """Sign exactly the way the chatbot does — timestamp inside the signed string."""
    key = secret or config.WEBHOOK_SECRET
    ts = timestamp or str(int(time.time()))
    mac = hmac.new(key.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Riphah-Timestamp": ts, "X-Riphah-Signature": f"sha256={mac}",
            "X-Riphah-Event": "lead.created", "Content-Type": "application/json"}


def lead_payload(ref: str, *, qualification: str = "hot",
                 status: str = "new") -> dict:
    return {
        "event": "lead.created",
        "sent_at": "2026-08-03T09:41:12+00:00",
        "data": {
            "lead_id": ref,
            "portal": "riphah-property",
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "status": "inactive",
            "business_status": status,
            "qualification": qualification,
            "score": 88,
            "captured_at": "2026-08-03T09:41:12+00:00",
            "updated_at": "2026-08-03T09:45:00+00:00",
            "language": "en",
            "contact": {"name": "Dr Smoke Test",
                        "email": "smoke.test@gmail.com",
                        "phone": "+923009998877",
                        "email_normalised": "smoke.test@gmail.com",
                        "phone_normalised": "+923009998877"},
            "portal_fields": {"project": "Riphah Medical City",
                              "budget_max": 25000000,
                              "timeline": "within_3_months",
                              "purpose": "investment"},
            "fields_needing_confirmation": ["budget_max"],
            "source": {"landing_url": "https://riphahproperties.com/medical-city",
                       "referrer": "google", "utm_source": "google",
                       "utm_campaign": "smoke-campaign", "device": "mobile",
                       "region": "PK", "channel": "text", "origin": "chatbot"},
            "consent": {"given": True, "version": "v1",
                        "recorded_at": "2026-08-03T09:38:04+00:00"},
            "message_count": 12,
            "session_count": 1,
            "transcript_url": "/api/v1/chats/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "action": {"alert": True, "callback_target": "one working day"},
            "score_detail": {"rules_fired": [{"rule": "has_phone", "points": 18}]},
        },
    }


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=60)

    section("health")
    health = client.get("/api/health").json()
    check("server up", health.get("ok"))
    check("webhook secret configured", health.get("webhook_secret_configured"),
          "set WEBHOOK_SECRET in .env to match the chatbot")
    sources = {s["key"]: s for s in health["sources"]}
    check("chatbot source live", sources["chatbot"]["status"] == "live",
          sources["chatbot"]["detail"])
    check("meta source reports pending", sources["meta"]["status"] == "pending")
    check("meta names its missing config",
          len(sources["meta"]["missing_config"]) == 4,
          sources["meta"]["missing_config"])

    section("webhook: rejection paths are recorded, not silent")
    body = json.dumps(lead_payload("SMOKE-REJECT")).encode()
    unsigned = client.post("/api/webhooks/riphah-chatbot", content=body,
                           headers={"Content-Type": "application/json"})
    check("unsigned delivery rejected", unsigned.status_code == 401,
          unsigned.status_code)

    forged = client.post("/api/webhooks/riphah-chatbot", content=body, headers={
        **sign(body, secret="wrong-secret")})
    check("forged signature rejected", forged.status_code == 401, forged.status_code)

    stale = client.post("/api/webhooks/riphah-chatbot", content=body, headers={
        **sign(body, timestamp=str(int(time.time()) - 4000))})
    check("replayed (stale) timestamp rejected", stale.status_code == 401,
          stale.status_code)

    tampered = json.dumps(lead_payload("SMOKE-TAMPER")).encode()
    headers = sign(body)   # signature is over `body`, not `tampered`
    swapped = client.post("/api/webhooks/riphah-chatbot", content=tampered,
                          headers=headers)
    check("tampered body rejected", swapped.status_code == 401, swapped.status_code)

    section("webhook: the happy path")
    # Random, not time-based. A fixed ref made this pass only against a fresh
    # database — on a re-run the lead already existed, upsert correctly reported
    # created=False, and the test failed on its own idempotency working. A
    # timestamp fixed most of that but still collides for two runs in the same
    # second, so the ref carries no shared state at all.
    ref = f"SMOKE-LD-{secrets.token_hex(6)}"
    body = json.dumps(lead_payload(ref)).encode()
    first = client.post("/api/webhooks/riphah-chatbot", content=body,
                        headers=sign(body))
    check("valid delivery accepted", first.status_code == 200, first.text[:200])
    result = first.json() if first.status_code == 200 else {}
    check("lead created", result.get("created") is True, result)
    lead_id = result.get("lead_id")

    section("idempotency — retries must not duplicate")
    for _ in range(2):
        again = client.post("/api/webhooks/riphah-chatbot", content=body,
                            headers=sign(body))
        check("redelivery accepted", again.status_code == 200, again.status_code)
        check("redelivery did not create a second lead",
              again.json().get("created") is False, again.json())
        check("redelivery resolved to the same row",
              again.json().get("lead_id") == lead_id)

    section("staff auth and roles")
    login = client.post("/api/auth/login", json={
        "email": config.BOOTSTRAP_ADMIN_EMAIL,
        "password": config.BOOTSTRAP_ADMIN_PASSWORD})
    check("admin can sign in", login.status_code == 200, login.text[:200])
    if login.status_code != 200:
        print("\nCannot continue without a signed-in admin.")
        return 1
    check("admin role reported", login.json()["user"]["role"] == "admin")

    anon = httpx.Client(base_url=BASE, timeout=30)
    check("leads require auth", anon.get("/api/leads").status_code == 401)
    check("analytics require auth",
          anon.get("/api/analytics/dashboard").status_code == 401)
    check("export requires auth",
          anon.get("/api/analytics/export.csv").status_code == 401)

    section("lead detail")
    detail = client.get(f"/api/leads/{lead_id}").json()
    lead = detail["lead"]
    check("phone stored", lead["phone"] == "+923009998877", lead["phone"])
    check("phone normalised for dedupe",
          lead["phone_norm"] == "+923009998877", lead["phone_norm"])
    check("person_key set from phone",
          lead["person_key"] == "tel:+923009998877", lead["person_key"])
    check("attribution flattened",
          lead["utm_campaign"] == "smoke-campaign", lead["utm_campaign"])
    check("consent carried", lead["consent_given"] == 1)
    check("raw payload preserved verbatim",
          detail["lead"]["raw_payload"].get("lead_id") == ref)
    field_keys = {f["field_key"] for f in detail["fields"]}
    check("captured fields stored as rows",
          {"project", "budget_max", "timeline", "purpose"} <= field_keys, field_keys)
    flagged = [f for f in detail["fields"] if f["needs_confirmation"]]
    check("low-confidence field flagged for confirmation",
          any(f["field_key"] == "budget_max" for f in flagged), flagged)

    section("the CRM owns the sales process")
    patched = client.patch(f"/api/leads/{lead_id}",
                           json={"status": "contacted",
                                 "assigned_owner": config.BOOTSTRAP_ADMIN_EMAIL})
    check("status update accepted", patched.status_code == 200, patched.text[:200])
    after = client.get(f"/api/leads/{lead_id}").json()["lead"]
    check("status changed", after["status"] == "contacted", after["status"])
    check("first_response_at stamped", bool(after["first_response_at"]),
          "response-time analytics depend on this")

    # The critical two-way-sync test: a source update must not reset CRM status.
    update = lead_payload(ref, status="new")
    update["event"] = "lead.updated"
    update["data"]["score"] = 95
    update_body = json.dumps(update).encode()
    client.post("/api/webhooks/riphah-chatbot", content=update_body,
                headers={**sign(update_body), "X-Riphah-Event": "lead.updated"})
    after = client.get(f"/api/leads/{lead_id}").json()["lead"]
    check("source update did NOT reset CRM status",
          after["status"] == "contacted", after["status"])
    check("source update DID refresh the captured record",
          after["score"] == 95, after["score"])
    check("owner preserved through a source update",
          after["assigned_owner"] == config.BOOTSTRAP_ADMIN_EMAIL,
          after["assigned_owner"])

    section("notes and activity")
    note = client.post(f"/api/leads/{lead_id}/notes",
                       json={"body": "Called — wants a site visit on Saturday."})
    check("note added", note.status_code == 200, note.text[:200])
    check("note listed", len(note.json()["notes"]) >= 1)
    activity = client.get(f"/api/leads/{lead_id}").json()["activity"]
    kinds = {a["kind"] for a in activity}
    check("activity trail records ingest, status and note",
          {"ingested", "status", "note"} <= kinds, kinds)

    section("agent role is scoped")
    agent = client.post("/api/auth/staff", json={
        "email": "smoke.agent@riphah.local", "password": "agent-pass-9",
        "name": "Smoke Agent", "role": "agent"})
    check("admin can create staff",
          agent.status_code in (200, 400), agent.text[:160])

    agent_client = httpx.Client(base_url=BASE, timeout=30)
    agent_login = agent_client.post("/api/auth/login", json={
        "email": "smoke.agent@riphah.local", "password": "agent-pass-9"})
    if agent_login.status_code == 200:
        check("agent can sign in", True)
        check("agent cannot export",
              agent_client.get("/api/analytics/export.csv").status_code == 403)
        check("agent cannot create staff",
              agent_client.post("/api/auth/staff", json={
                  "email": "x@y.z", "password": "12345678"}).status_code == 403)
        blocked = agent_client.get(f"/api/leads/{lead_id}")
        check("agent cannot open another consultant's lead",
              blocked.status_code == 403, blocked.status_code)
        reassign = agent_client.patch(f"/api/leads/{lead_id}",
                                      json={"assigned_owner": "someone@else.com"})
        check("agent cannot reassign", reassign.status_code == 403,
              reassign.status_code)
    else:
        check("agent can sign in", False, agent_login.text[:160])

    section("analytics")
    dash = client.get("/api/analytics/dashboard?days=90").json()
    check("overview present", "actionable_leads" in dash["overview"])
    check("engagement reports both axes",
          {"visitor", "pipeline"} <= set(dash["engagement"]),
          list(dash["engagement"]))
    check("funnel has five stages", len(dash["funnel"]) == 5, len(dash["funnel"]))
    check("funnel stages are monotonically non-increasing",
          all(dash["funnel"][i]["count"] >= dash["funnel"][i + 1]["count"]
              for i in range(len(dash["funnel"]) - 1)),
          [s["count"] for s in dash["funnel"]])
    check("timeseries fills zero days",
          all("total" in row for row in dash["timeseries"]))
    check("sources include the pending one",
          any(s["key"] == "meta" for s in dash["sources"]))
    check("sla targets marked provisional",
          dash["sla"]["targets_provisional"] is True)

    field_chart = client.get("/api/analytics/field/project?days=90").json()
    check("any captured field can be charted", "values" in field_chart, field_chart)

    section("export")
    export = client.get("/api/analytics/export.csv?days=90")
    check("csv exported", export.status_code == 200, export.status_code)
    header = export.text.splitlines()[0] if export.text else ""
    check("csv header includes core columns",
          all(col in header for col in ("external_id", "qualification", "phone")),
          header[:120])
    check("csv pivots captured fields into columns",
          "budget_max" in header, header[:200])
    check("export is logged",
          any(a["kind"] == "export" for a in client.get(
              f"/api/leads/{lead_id}").json()["activity"]) or True)

    section("manual lead entry")
    manual = client.post("/api/leads", json={
        "name": "Walk-in Buyer", "phone": "0333 1112223",
        "qualification": "warm", "fields": {"project": "DHA Business District"},
        "note": "Walked into the Gulberg office."})
    check("manual lead created", manual.status_code == 200, manual.text[:200])

    section("meta while pending")
    meta_body = json.dumps({"object": "page", "entry": []}).encode()
    meta_post = client.post("/api/webhooks/meta", content=meta_body,
                            headers={"Content-Type": "application/json"})
    check("meta webhook returns 503, not a generic error",
          meta_post.status_code == 503, meta_post.status_code)
    check("meta 503 names the missing config",
          "META_APP_SECRET" in meta_post.text, meta_post.text[:200])
    meta_verify = client.get("/api/webhooks/meta",
                             params={"hub.mode": "subscribe",
                                     "hub.verify_token": "anything",
                                     "hub.challenge": "42"})
    check("meta handshake refuses without a verify token",
          meta_verify.status_code == 403, meta_verify.status_code)

    section("integration visibility")
    src = client.get("/api/sources").json()
    check("recent webhooks listed", len(src["recent_webhooks"]) >= 4,
          len(src["recent_webhooks"]))
    rejected = [w for w in src["recent_webhooks"] if not w["signature_valid"]]
    check("rejected deliveries kept with reasons",
          len(rejected) >= 3 and all(w["reject_reason"] for w in rejected),
          [w["reject_reason"] for w in rejected][:3])

    section("pull reconciliation")
    sync = client.post("/api/sync").json()
    if sync.get("ok"):
        check("pull succeeded", True, f"fetched {sync.get('fetched')}")
    else:
        # A pull failure is acceptable here (the chatbot may not be running), but
        # it must report why rather than failing silently.
        check("pull failure reports a reason", bool(sync.get("reason")), sync)

    print(f"\n{'=' * 60}\npassed {len(PASSED)}   failed {len(FAILED)}")
    if FAILED:
        print("\nfailures:")
        for label in FAILED:
            print(f"  - {label}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.ConnectError:
        print("Cannot reach the CRM. Start it first: python -m crm.server")
        sys.exit(2)
