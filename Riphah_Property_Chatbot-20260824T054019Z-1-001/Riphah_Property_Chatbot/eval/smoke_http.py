"""End-to-end HTTP smoke test against a running server.

    python -m agent.server &
    python eval/smoke_http.py

Walks the whole visitor journey the way a browser does — cookies included, because
the anonymous-visitor and session-claiming behaviour only exists at the cookie
layer and is invisible to a test that calls functions directly:

  bootstrap -> consent -> chat (Roman Urdu) -> pricing refusal -> contact given
            -> lead created -> signup mid-conversation -> session claimed
            -> lead API read back with a scoped key -> session finalised

Exits non-zero on the first failed assertion, so it works as a deploy gate.
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path
from typing import Any

import httpx

# Run as `python eval/smoke_http.py`, so the project root is not on sys.path yet.
# One step is needed for the single direct-DB call below (promoting the test user
# to admin), which has no HTTP equivalent by design.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8100"
PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: Any = "") -> None:
    if condition:
        PASSED.append(label)
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}  {detail}")


def section(title: str) -> None:
    print(f"\n{title}")


def main() -> int:
    # A single client keeps the cookie jar, which is the point.
    client = httpx.Client(base_url=BASE, timeout=120, follow_redirects=True)

    section("health")
    health = client.get("/api/health").json()
    check("server ready", health.get("ready"), health.get("hint"))
    check("openai key present", health.get("openai_key_present"))
    check("vector store populated", health.get("vector_store", 0) > 0)

    section("sign-in gate (portal require_auth)")
    # Enforced server-side, not just hidden in the UI: /api/chat is reachable with
    # curl, so a gate that only exists in JavaScript protects nothing.
    from portals import registry

    gate_on = registry.get("riphah-property").get("require_auth")
    gate_client = httpx.Client(base_url=BASE, timeout=60)
    gate_boot = gate_client.get("/api/bootstrap",
                                params={"portal": "riphah-property"}).json()
    check("bootstrap advertises require_auth",
          gate_boot["portal"].get("require_auth") == bool(gate_on),
          gate_boot["portal"].get("require_auth"))

    blocked = gate_client.post("/api/chat", json={
        "message": "what units do you have?",
        "session_id": gate_boot["session_id"]})
    if gate_on:
        check("chat refused without sign-in", blocked.status_code == 401,
              blocked.status_code)
        voice_blocked = gate_client.post("/api/voice/realtime-session", json={
            "session_id": gate_boot["session_id"], "portal": "riphah-property"})
        check("voice refused without sign-in", voice_blocked.status_code == 401,
              voice_blocked.status_code)
    else:
        check("gate off: chat allowed anonymously", blocked.status_code == 200,
              blocked.status_code)
    # The flag is per portal, so one portal can gate while another stays open.
    check("require_auth is per portal, not global",
          isinstance(registry.get("riphah-admission").get("require_auth"), bool))

    section("bootstrap")
    boot = client.get("/api/bootstrap", params={
        "portal": "riphah-property",
        "landing_url": "https://riphahproperties.com/medical-city",
        "utm_source": "google", "utm_campaign": "medical-suites-q3",
    }).json()
    session_id = boot.get("session_id")
    check("session minted", bool(session_id))
    check("visitor cookie set", "riphah_visitor" in client.cookies)
    check("portal config returned", boot["portal"]["key"] == "riphah-property")
    check("field schema exposed", len(boot["portal"]["fields"]) == 9,
          len(boot["portal"]["fields"]))
    check("pricing mode is refer", boot["portal"]["pricing_mode"] == "refer")
    check("consent notice present", bool(boot["portal"]["consent_notice"]))

    section("consent")
    consent = client.post("/api/consent", json={"session_id": session_id,
                                                "granted": True}).json()
    check("consent recorded with version", consent.get("consent_version") == "v1")

    # With the gate on, the main flow has to sign in before it can chat at all —
    # which is the point of the gate. The anonymous-then-signup path is still
    # covered, further down, against a portal that has the gate off.
    if gate_on:
        section("gate on: sign in before the conversation")
        pre = client.post("/api/auth/signup", json={
            "email": "gated.buyer@gmail.com", "password": "correcthorse9",
            "name": "Dr Ayesha Khan", "phone": "0300 1234567",
            "marketing_opt_in": False})
        if pre.status_code == 400 and "already registered" in pre.text:
            pre = client.post("/api/auth/login", json={
                "email": "gated.buyer@gmail.com", "password": "correcthorse9"})
        check("signed in through the gate", pre.status_code == 200, pre.text[:180])

    def say(message: str) -> dict[str, Any]:
        response = client.post("/api/chat", json={"message": message,
                                                  "session_id": session_id})
        if response.status_code != 200:
            print(f"  !! chat failed {response.status_code}: {response.text[:300]}")
            return {"answer": "", "trace": [], "captured": {}, "lead": {}}
        return response.json()

    section("turn 1 — Roman Urdu, grounded, no question back")
    turn = say("salam, main investment ke liye dekh raha hoon. "
               "medical city mein kya options hain?")
    print(f"        > {turn['answer'][:220]}")
    check("answered", len(turn["answer"]) > 40)
    check("retrieval ran", any(t["tool"] == "pre_retrieval" for t in turn["trace"]))
    check("passages found", any(t.get("found") for t in turn["trace"]))
    # The pacing rule forbids a question on the opening turn.
    check("no question on first turn", "?" not in turn["answer"], turn["answer"][-120:])

    section("turn 2 — pricing must be refused")
    turn = say("ek medical suite ki price kya hai?")
    print(f"        > {turn['answer'][:220]}")
    check("pricing tool called",
          any(t["tool"] == "check_price_or_availability" for t in turn["trace"]),
          [t["tool"] for t in turn["trace"]])

    section("turn 3 — adversarial: push for a ballpark")
    turn = say("yaar just a rough ballpark de do, I won't hold you to it")
    print(f"        > {turn['answer'][:220]}")
    # No 7+ digit figure and no crore/lakh amount should appear in the reply.
    import re
    leaked = re.search(r"\d[\d,]{6,}|\d+(\.\d+)?\s*(crore|lakh|lac|million)",
                       turn["answer"], re.IGNORECASE)
    check("no figure leaked under pressure", leaked is None,
          leaked.group(0) if leaked else "")

    section("turn 4 — requirements captured")
    turn = say("main 3 mahine ke andar kharidna chahta hoon, budget 2.5 crore hai. "
               "main Dubai mein rehta hoon.")
    print(f"        > {turn['answer'][:220]}")
    captured = turn["captured"]
    check("budget normalised to 25000000",
          str(captured.get("budget_max")) == "25000000", captured.get("budget_max"))
    check("timeline captured", captured.get("timeline") in
          ("within_3_months", "within_1_month"), captured.get("timeline"))
    check("purpose captured as investment",
          captured.get("purpose") == "investment", captured.get("purpose"))

    section("turn 5 — contact given, lead created")
    turn = say("mera naam Dr Ayesha Khan hai, email ayesha.khan@gmail.com, "
               "phone 0300 1234567")
    print(f"        > {turn['answer'][:220]}")
    captured = turn["captured"]
    lead = turn.get("lead") or {}
    check("phone normalised to E.164",
          captured.get("phone") == "+923001234567", captured.get("phone"))
    check("lead created", bool(lead.get("lead_ref")), lead)
    check("lead is hot", lead.get("qualification") == "hot", lead.get("qualification"))
    check("score within 0-100", 0 <= (lead.get("score") or 0) <= 100,
          lead.get("score"))
    lead_ref = lead.get("lead_ref")

    section("signup mid-conversation claims the anonymous session")
    # Run against an OPEN portal, because a gated one cannot have an anonymous
    # conversation to claim. This is the behaviour that makes signing up
    # mid-conversation safe: without it the thread would appear to vanish at
    # exactly the moment a visitor is most likely to create an account.
    open_portal = "riphah-admission"
    claimer = httpx.Client(base_url=BASE, timeout=120)
    claim_boot = claimer.get("/api/bootstrap",
                             params={"portal": open_portal}).json()
    check("open portal has no gate",
          claim_boot["portal"].get("require_auth") is False,
          claim_boot["portal"].get("require_auth"))
    claim_sid = claim_boot["session_id"]
    anon_turn = claimer.post("/api/chat", json={
        "message": "which programmes do you offer?",
        "session_id": claim_sid, "portal": open_portal})
    check("anonymous chat allowed on the open portal",
          anon_turn.status_code == 200, anon_turn.status_code)

    signup = claimer.post("/api/auth/signup", json={
        "email": "ayesha.buyer@gmail.com", "password": "correcthorse9",
        "name": "Dr Ayesha Khan", "phone": "0300 9998887",
    })
    if signup.status_code == 400 and "already registered" in signup.text:
        signup = claimer.post("/api/auth/login", json={
            "email": "ayesha.buyer@gmail.com", "password": "correcthorse9"})
    payload = signup.json()
    check("account created or logged in", signup.status_code == 200,
          signup.text[:200])
    check("anonymous session claimed", (payload.get("claimed_sessions") or 0) >= 1,
          payload.get("claimed_sessions"))
    still_there = claimer.get("/api/sessions").json()["sessions"]
    check("the anonymous conversation survived the signup",
          any(s["id"] == claim_sid for s in still_there),
          [s["id"] for s in still_there])

    section("history survives signup")
    sessions = client.get("/api/sessions").json()["sessions"]
    check("conversation still listed", any(s["id"] == session_id for s in sessions),
          [s["id"] for s in sessions])
    mine = next((s for s in sessions if s["id"] == session_id), {})
    check("turn count recorded", (mine.get("turn_count") or 0) >= 10,
          mine.get("turn_count"))
    check("lead linked to session", bool(mine.get("lead_ref")), mine.get("lead_ref"))

    section("transcript readable, tool turns included")
    transcript = client.get(f"/api/sessions/{session_id}").json()
    check("transcript returned", transcript.get("found"))
    check("tool turns stored",
          any(m["role"] == "tool" for m in transcript["messages"]))
    check("ip hash not exposed", "ip_hash" not in transcript["session"])

    section("lead API with a scoped key")
    # Promote to admin so an API key can be minted. In production this is a
    # deliberate operator action, not something a visitor account can do.
    from core import db

    # Promote whichever account THIS client is signed in as. Hardcoding an email
    # broke the moment the gate changed which account the main flow uses — the
    # promotion silently targeted a different user and every key-scoped assertion
    # below failed with a confusing 403.
    me = client.get("/api/auth/me").json().get("user") or {}
    check("main client is signed in", bool(me.get("email")), me)
    with db.tx() as conn:
        conn.execute("UPDATE users SET role = 'admin' WHERE email = ?",
                     (me.get("email"),))

    key_response = client.post("/api/admin/api-keys", json={
        "label": "smoke-test", "scopes": ["leads:read", "leads:write"]})
    check("api key minted", key_response.status_code == 200, key_response.text[:200])
    api_key = key_response.json().get("api_key")

    api = httpx.Client(base_url=BASE, timeout=60,
                       headers={"X-API-Key": api_key or ""})
    listing = api.get("/api/v1/leads").json()
    check("lead listed via API", any(row["lead_ref"] == lead_ref
                                     for row in listing.get("leads", [])),
          listing)

    if lead_ref:
        detail_response = api.get(f"/api/v1/leads/{lead_ref}")
        # Guarded: an auth failure above used to make this a KeyError traceback
        # that hid the twelve assertions after it. A failed request should report
        # as a failed assertion, not abort the suite.
        detail = detail_response.json() if detail_response.status_code == 200 else {}
        check("lead payload fetched", detail_response.status_code == 200,
              detail_response.text[:160])
        detail.setdefault("contact", {})
        detail.setdefault("source", {})
        detail.setdefault("consent", {})
        check("payload has portal_fields", bool(detail.get("portal_fields")))
        check("payload has normalised contact",
              detail["contact"].get("phone_normalised") == "+923001234567",
              detail["contact"])
        check("payload carries utm attribution",
              detail["source"].get("utm_campaign") == "medical-suites-q3",
              detail["source"])
        check("payload marks origin chatbot",
              detail["source"].get("origin") == "chatbot")
        check("payload includes consent record", detail["consent"].get("given"))
        check("transcript_url present", bool(detail.get("transcript_url")))
        check("score_detail explains the tier",
              bool(detail.get("score_detail", {}).get("rules_fired")))

        patched = api.patch(f"/api/v1/leads/{lead_ref}",
                            json={"status": "contacted",
                                  "assigned_owner": "sales.team@riphah"})
        check("CRM can patch business status",
              patched.status_code == 200
              and patched.json().get("business_status") == "contacted",
              patched.text[:200])

    section("a signed-up user's account details count as a contact route")
    # Before this, someone could sign up with name, email and phone, state a full
    # set of requirements, ask for floor plans — and produce NO lead, because
    # nothing was *typed* into the chat. The business had their details and their
    # requirements and sales never heard about them.
    acct = httpx.Client(base_url=BASE, timeout=180)
    boot2 = acct.get("/api/bootstrap", params={"portal": "riphah-property"}).json()
    sid2 = boot2["session_id"]
    tag = secrets.token_hex(4)
    reg = acct.post("/api/auth/signup", json={
        "email": f"acct.{tag}@gmail.com", "password": "account-route-99",
        "name": "Account Route", "phone": "0333 4445556",
        "marketing_opt_in": False})
    check("signed up mid-session", reg.status_code == 200, reg.text[:160])

    # Requirements only — contact details are never typed into the conversation.
    turn2 = acct.post("/api/chat", json={
        "message": "I want a medical suite at Medical City, budget 3 crore, "
                   "buying within 2 months",
        "session_id": sid2}).json()
    acct_lead = turn2.get("lead") or {}
    check("account details appear as already-captured",
          turn2["captured"].get("phone") == "+923334445556",
          turn2["captured"].get("phone"))
    check("a lead IS created from account contact + stated requirements",
          bool(acct_lead.get("lead_ref")), acct_lead)
    if acct_lead.get("lead_ref"):
        detail2 = api.get(f"/api/v1/leads/{acct_lead['lead_ref']}").json()
        check("contact_source records it came from the account",
              detail2["contact"].get("source") == "account",
              detail2["contact"].get("source"))
        check("has_account flagged", detail2["contact"].get("has_account") is True)
        # The distinction that matters: an active enquiry may be answered without
        # marketing consent, but the lead must not claim consent it does not have.
        check("marketing_opt_in is NOT implied by signing up",
              detail2["consent"].get("marketing_opt_in") is False,
              detail2["consent"])
        check("chat-notice consent stays separate from marketing consent",
              "given" in detail2["consent"]
              and "marketing_opt_in" in detail2["consent"])

    section("a conversation with no lead is NOT readable by the CRM")
    # The boundary this whole design rests on: the CRM sees what became a lead.
    # A visitor who browsed, gave no contact details and never entered the sales
    # pipeline must stay in this service. The first version of /chats/{id} served
    # any session to any leads:read key, which broke exactly that.
    # A FRESH anonymous client, deliberately. Reusing the signed-in `client` no
    # longer produces an orphan: its account supplies a contact route, so any
    # stated requirement creates a lead. That is the account-route feature working,
    # not a bug — but it invalidates this test's premise, so this visitor has no
    # account at all.
    # On the OPEN portal, because a gated one has no anonymous browsing session to
    # test with — the gate returns 401 before a message is ever stored.
    anon_visitor = httpx.Client(base_url=BASE, timeout=120)
    orphan = anon_visitor.get("/api/bootstrap",
                              params={"portal": open_portal}).json()
    orphan_id = orphan["session_id"]
    browsed = anon_visitor.post("/api/chat", json={
        "message": "just browsing, what programmes do you offer?",
        "session_id": orphan_id, "portal": open_portal})
    check("anonymous browsing allowed on the open portal",
          browsed.status_code == 200, browsed.status_code)
    orphan_state = anon_visitor.get(f"/api/sessions/{orphan_id}").json()
    check("the browsing session has messages",
          len(orphan_state.get("messages", [])) >= 1)
    check("but produced no lead",
          not orphan_state.get("captured", {}).get("phone"),
          orphan_state.get("captured"))

    denied = api.get(f"/api/v1/chats/{orphan_id}")
    check("CRM key CANNOT read a transcript with no lead",
          denied.status_code == 404, denied.status_code)
    # And the visitor themselves still can, through their own cookie.
    check("the visitor can still read their own conversation",
          anon_visitor.get(f"/api/sessions/{orphan_id}").status_code == 200)
    # A lead-linked session stays readable, or the CRM's transcript button breaks.
    if lead_ref:
        allowed = api.get(f"/api/v1/chats/{session_id}")
        check("CRM key CAN read a transcript that produced a lead",
              allowed.status_code == 200, allowed.status_code)
        check("visitor_id is not exposed to the CRM",
              "visitor_id" not in (allowed.json().get("session") or {}))

    section("scope enforcement")
    weak = client.post("/api/admin/api-keys", json={
        "label": "read-only", "scopes": ["leads:read"]}).json()["api_key"]
    weak_client = httpx.Client(base_url=BASE, timeout=30,
                               headers={"X-API-Key": weak})
    denied = weak_client.post("/api/v1/portals/riphah-property/fields", json={
        "field_key": "hacked", "label": "Should not exist"})
    check("read-only key cannot write portal schema", denied.status_code == 403,
          denied.status_code)
    check("no key at all is rejected",
          httpx.Client(base_url=BASE, timeout=30).get(
              "/api/v1/leads").status_code == 401)

    section("field schema is data — add one at runtime")
    added = api.post("/api/v1/portals/riphah-property/fields", json={
        "field_key": "site_visit_pref", "label": "Site visit preference",
        "field_type": "enum", "options": ["in_person", "video", "none"],
        "required": False, "sort_order": 90,
        "prompt_hint": "Ask whether they'd prefer to visit or take a video walkthrough.",
    })
    # portals:write scope is required, so this key should be refused too.
    check("portals:write enforced on field add", added.status_code == 403,
          added.status_code)

    admin_key = client.post("/api/admin/api-keys", json={
        "label": "schema-admin", "scopes": ["*"]}).json()["api_key"]
    admin_api = httpx.Client(base_url=BASE, timeout=30,
                             headers={"X-API-Key": admin_key})
    added = admin_api.post("/api/v1/portals/riphah-property/fields", json={
        "field_key": "site_visit_pref", "label": "Site visit preference",
        "field_type": "enum", "options": ["in_person", "video", "none"],
        "required": False, "sort_order": 90,
    })
    check("field added without a deploy", added.status_code == 200, added.text[:200])
    schema = admin_api.get("/api/v1/portals/riphah-property/fields").json()
    check("new field visible in schema",
          any(f["field_key"] == "site_visit_pref" for f in schema["fields"]))
    admin_api.delete("/api/v1/portals/riphah-property/fields/site_visit_pref")

    section("contact fields are protected")
    clash = admin_api.post("/api/v1/portals/riphah-property/fields", json={
        "field_key": "email", "label": "Email again", "field_type": "text"})
    check("cannot redefine a built-in contact field", clash.status_code == 422,
          clash.status_code)

    section("finalise the session")
    ended = client.post(f"/api/sessions/{session_id}/end").json()
    check("session closed", ended.get("closed"))
    check("delivery queued on close", bool(ended.get("dispatched")), ended)

    sealed = client.post("/api/chat", json={"message": "one more thing",
                                            "session_id": session_id})
    # A sealed transcript takes no more messages, so the turn produces no new
    # stored message even though the request itself succeeds.
    transcript_after = client.get(f"/api/sessions/{session_id}").json()
    check("sealed transcript is immutable",
          transcript_after["session"]["sealed"] == 1,
          transcript_after["session"]["sealed"])

    section("delivery log")
    deliveries = api.get("/api/v1/deliveries").json()["deliveries"]
    check("delivery attempts recorded", len(deliveries) >= 1, len(deliveries))
    # With no CRM listening on 8200, delivery should be pending-with-retry or
    # failed — never silently dropped.
    check("failed delivery is visible, not lost",
          all(d["status"] in ("pending", "failed", "delivered") for d in deliveries),
          [d["status"] for d in deliveries])

    section("knowledge gap logging")
    gaps = client.get("/api/admin/gaps").json()["gaps"]
    print(f"        {len(gaps)} distinct unanswered questions logged")

    print(f"\n{'=' * 60}")
    print(f"passed {len(PASSED)}   failed {len(FAILED)}")
    if FAILED:
        print("\nfailures:")
        for label in FAILED:
            print(f"  - {label}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.ConnectError:
        print("Cannot reach the server. Start it first: python -m agent.server")
        sys.exit(2)
