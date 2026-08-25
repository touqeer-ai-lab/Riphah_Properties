"""Generate demo leads so the dashboard has something to show.

    python eval/seed_demo.py            # ~70 leads over 45 days
    python eval/seed_demo.py --count 200 --days 90
    python eval/seed_demo.py --clear    # remove demo leads only

Every lead goes through the real `ingest.upsert()` and the real source adapters —
Meta leads through `meta.normalise()` with genuine payload shapes. So this is not
just fixture rows in a table: it exercises the same path a live lead takes, which
means a bug in mapping or scoring shows up here rather than in production.

Demo rows are identifiable (`external_id` starts with `DEMO-` or `9000`), so
`--clear` removes them without touching real leads.
"""
from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db  # noqa: E402
from crm import ingest  # noqa: E402
from sources import meta  # noqa: E402
from sources.base import NormalisedLead  # noqa: E402

FIRST = ["Ayesha", "Bilal", "Hamza", "Sana", "Usman", "Fatima", "Imran", "Zainab",
         "Kamran", "Nida", "Faisal", "Hina", "Tariq", "Mariam", "Adnan", "Rabia",
         "Shahid", "Amna", "Junaid", "Saima", "Waqar", "Iqra", "Salman", "Noor"]
LAST = ["Khan", "Ahmed", "Malik", "Sheikh", "Butt", "Qureshi", "Raza", "Hussain",
        "Chaudhry", "Siddiqui", "Farooq", "Javed", "Iqbal", "Baig", "Awan"]
TITLES = ["Dr ", "Dr ", "", "", "", "Prof "]

CITIES = ["Islamabad, PK", "Lahore, PK", "Rawalpindi, PK", "Karachi, PK",
          "Dubai, AE", "Riyadh, SA", "London, UK", "Toronto, CA", "Faisalabad, PK"]
PROJECTS = ["Riphah Medical City", "Riphah Medical City", "Riphah Medical City",
            "DHA Business District", "DHA Business District", "Undecided"]
TYPES = ["medical_suite", "medical_suite", "apartment", "commercial_shop",
         "office", "plot"]
TIMELINES = ["within_1_month", "within_3_months", "within_3_months",
             "within_6_months", "within_12_months", "just_exploring",
             "just_exploring"]
PURPOSES = ["end_use", "investment", "investment", "both", "unsure"]
PAY = ["full_payment", "installments_3y", "installments_3y", "installments_4y_plus",
       "bank_finance", "undecided"]
CAMPAIGNS = ["medical-suites-q3", "medical-suites-q3", "dha-commercial-launch",
             "overseas-investors", "brand-search", None]
OWNERS = ["admin@riphah.local", "sales1@riphah.local", "sales2@riphah.local", None]
STATUSES = ["new", "new", "new", "contacted", "contacted", "qualified",
            "converted", "lost"]

# Budgets in rupees, weighted towards the realistic middle of the market.
BUDGETS = [4_500_000, 8_000_000, 12_000_000, 15_000_000, 18_000_000, 22_000_000,
           25_000_000, 25_000_000, 30_000_000, 35_000_000, 45_000_000, 60_000_000,
           90_000_000, 140_000_000]


def name() -> str:
    return f"{random.choice(TITLES)}{random.choice(FIRST)} {random.choice(LAST)}"


def stamp(days_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=days_ago)).isoformat(timespec="seconds")


def score_chatbot(contact: dict, fields: dict, turns: int) -> tuple[str, int]:
    """Mirror the chatbot's scoring shape closely enough for realistic tiers.

    Not an import: the chatbot's scorer reads its rules from that service's
    database. This is a demo-data approximation and is only used to fill the
    dashboard — real tiers always come from the source.
    """
    points = 0
    if contact.get("phone"):
        points += 18
    if contact.get("email"):
        points += 12
    if contact.get("name"):
        points += 5
    required = ["project", "purpose", "property_type", "budget_max", "timeline"]
    filled = sum(1 for k in required if fields.get(k))
    points += int(20 * filled / len(required))
    points += {"within_1_month": 22, "within_3_months": 16, "within_6_months": 8,
               "within_12_months": 3, "just_exploring": 0}.get(
                   fields.get("timeline", ""), 0)
    points += 8 if fields.get("purpose") in ("end_use", "investment", "both") else 0
    budget = fields.get("budget_max")
    if budget and 4_000_000 <= int(budget) <= 250_000_000:
        points += 15
    points += min(turns * 2, 12)
    points = min(100, points)

    contactable = bool(contact.get("phone") or contact.get("email"))
    if points >= 70 and contactable:
        return "hot", points
    if points >= 35 and contactable:
        return "warm", points
    return "cold", points


def make_chatbot_lead(index: int, days: int) -> NormalisedLead:
    age = random.random() ** 0.7 * days      # skewed towards recent
    person = name()
    handle = person.split()[-1].lower()

    # A realistic share of visitors never give a contact route.
    contactable = random.random() > 0.28
    voice = random.random() < 0.18
    # Includes single-turn visitors on purpose: a real portal produces plenty of
    # "asked one thing and left", and without them the engagement chart has an
    # empty "brief look" bucket and reads as though every visitor converses.
    turns = random.choice([1, 1, 2, 2, 3, 4, 5, 6, 8, 10, 12, 14])

    contact = {"name": person if contactable or random.random() > 0.5 else None}
    if contactable:
        contact["phone"] = (f"+9230{random.randint(0, 9)}"
                            f"{random.randint(1000000, 9999999)}")
        if random.random() > 0.35:
            contact["email"] = f"{handle}{random.randint(1, 99)}@gmail.com"

    fields: dict = {}
    # Field completeness rises with conversation depth — which is exactly what
    # progressive capture produces.
    depth = min(1.0, turns / 10)
    if random.random() < depth + 0.25:
        fields["project"] = random.choice(PROJECTS)
    if random.random() < depth + 0.2:
        fields["timeline"] = random.choice(TIMELINES)
    if random.random() < depth + 0.1:
        fields["purpose"] = random.choice(PURPOSES)
    if random.random() < depth:
        fields["property_type"] = random.choice(TYPES)
    if random.random() < depth - 0.1:
        fields["budget_max"] = str(random.choice(BUDGETS))
    if random.random() < depth - 0.25:
        fields["payment_pref"] = random.choice(PAY)
    location = random.choice(CITIES)
    if random.random() < depth:
        fields["buyer_location"] = location
        if not location.endswith("PK"):
            fields["is_overseas"] = "true"

    tier, points = score_chatbot(contact, fields, turns)
    if random.random() < 0.04:
        tier, points = "spam", 0

    captured = stamp(age)
    status = "new" if tier in ("cold", "spam") else random.choice(STATUSES)
    owner = None if tier == "cold" else random.choice(OWNERS)
    if status != "new" and not owner:
        owner = random.choice([o for o in OWNERS if o])

    return NormalisedLead(
        source_key="chatbot",
        external_id=f"DEMO-LD-{9000 + index}",
        portal="riphah-property",
        name=contact.get("name"),
        email=contact.get("email"),
        phone=contact.get("phone"),
        qualification=tier,
        score=points,
        status=status,
        assigned_owner=owner,
        language=random.choice(["en", "en", "en", "ur"]),
        fields=fields,
        needs_confirmation=["budget_max"] if fields.get("budget_max")
                           and random.random() < 0.15 else [],
        utm_source=random.choice(["google", "google", "facebook", "direct", None]),
        utm_medium=random.choice(["cpc", "organic", "paid_social", None]),
        utm_campaign=random.choice(CAMPAIGNS),
        referrer=random.choice(["google", "instagram", "direct", None]),
        device=random.choice(["mobile", "mobile", "mobile", "desktop"]),
        region=location.split(", ")[-1],
        landing_url=random.choice([
            "https://riphahproperties.com/medical-city",
            "https://riphahproperties.com/dha-business-district",
            "https://riphahproperties.com/",
        ]),
        channel="voice" if voice else "text",
        consent_given=random.random() > 0.15,
        consent_version="v1",
        message_count=turns * 2,
        session_count=1 if random.random() > 0.12 else 2,
        transcript_url=f"/api/v1/chats/demo-{index}",
        captured_at=captured,
        source_updated_at=captured,
        raw_payload={"demo": True, "session_id": f"demo-session-{index}"},
    )


def make_meta_lead(index: int, days: int) -> NormalisedLead:
    """A Meta lead built as a real Graph payload, then run through the adapter.

    Deliberately not constructed as a NormalisedLead directly — going through
    `meta.normalise()` means the demo data exercises the field mapping and the
    conservative scoring, so a mapping regression shows up here.
    """
    person = name()
    handle = person.split()[-1].lower()
    age = random.random() ** 0.7 * days

    answers = [
        {"name": "full_name", "values": [person]},
        {"name": "phone_number",
         "values": [f"+92 3{random.randint(10, 45)} {random.randint(1000000, 9999999)}"]},
    ]
    if random.random() > 0.4:
        answers.append({"name": "email",
                        "values": [f"{handle}{random.randint(1, 99)}@gmail.com"]})
    if random.random() > 0.3:
        answers.append({"name": "city", "values": [random.choice(CITIES).split(",")[0]]})
    if random.random() > 0.25:
        answers.append({"name": "which_project_are_you_interested_in?",
                        "values": [random.choice(PROJECTS)]})
    if random.random() > 0.35:
        answers.append({"name": "when_are_you_planning_to_buy?",
                        "values": [random.choice(
                            ["Immediately", "1-3 months", "3-6 months",
                             "6-12 months", "Just looking"])]})
    if random.random() > 0.45:
        answers.append({"name": "what_is_your_budget?",
                        "values": [random.choice(
                            ["1-2 crore", "2 to 3 crore", "50 lakh", "3-5 crore",
                             "under 1 crore"])]})
    if random.random() > 0.5:
        answers.append({"name": "are_you_buying_for_investment_or_personal_use?",
                        "values": [random.choice(["Investment", "Personal use"])]})

    raw = {
        "id": f"DEMO-META-{9000 + index}",
        "created_time": (dt.datetime.now(dt.timezone.utc)
                         - dt.timedelta(days=age)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
        "form_id": "998877665544332",
        "campaign_name": random.choice(
            ["medical-suites-august", "dha-commercial-launch", "overseas-investors"]),
        "ad_name": "RMC Suites — Carousel A",
        "platform": random.choice(["fb", "ig"]),
        "field_data": answers,
    }
    lead = meta.SOURCE.normalise(raw)
    lead.status = random.choice(["new", "new", "new", "contacted", "lost"])
    if lead.status != "new":
        lead.assigned_owner = random.choice([o for o in OWNERS if o])
    return lead


def clear() -> int:
    with db.tx() as conn:
        cur = conn.execute("DELETE FROM leads WHERE external_id LIKE 'DEMO-%'")
        conn.execute("UPDATE sources SET leads_received = 0")
    return cur.rowcount


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed demo leads")
    parser.add_argument("--count", type=int, default=70)
    parser.add_argument("--days", type=int, default=45)
    parser.add_argument("--meta-share", type=float, default=0.3,
                        help="fraction of leads from Meta (default 0.3)")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--seed", type=int, default=20260803,
                        help="RNG seed, so the demo set is reproducible")
    args = parser.parse_args()

    db.migrate()
    ingest.register_source("chatbot", "AI Property Assistant", status="live")
    ingest.register_source("meta", "Meta Lead Ads (Facebook / Instagram)",
                           status="pending")
    ingest.register_source("manual", "Manually entered", status="live")

    if args.clear:
        print(f"removed {clear()} demo leads")
        return 0

    random.seed(args.seed)
    meta_count = int(args.count * args.meta_share)
    chatbot_count = args.count - meta_count
    created = 0

    for index in range(chatbot_count):
        result = ingest.upsert(make_chatbot_lead(index, args.days), actor="demo-seed")
        created += result.get("created", False)

    for index in range(meta_count):
        result = ingest.upsert(make_meta_lead(index, args.days), actor="demo-seed")
        created += result.get("created", False)

    # Backfill first_response_at for leads that are past 'new', so the response-time
    # and SLA analytics have something real to measure.
    touched = 0
    for row in db.query("SELECT id, captured_at FROM leads "
                        " WHERE status != 'new' AND first_response_at IS NULL"):
        delay = random.choice([0.3, 0.8, 1.5, 4, 9, 20, 30, 52, 90])
        responded = (dt.datetime.fromisoformat(row["captured_at"])
                     + dt.timedelta(hours=delay)).isoformat(timespec="seconds")
        with db.tx() as conn:
            conn.execute("UPDATE leads SET first_response_at = ? WHERE id = ?",
                         (responded, row["id"]))
        touched += 1

    # Backdate `updated_at` to sit near the capture date. Every row was just
    # inserted, so without this every lead looks touched-today and the dormant
    # bucket is permanently zero — which would hide the one pipeline signal a
    # sales manager most needs to see.
    with db.tx() as conn:
        conn.execute(
            "UPDATE leads SET updated_at = COALESCE(first_response_at, captured_at) "
            " WHERE external_id LIKE 'DEMO-%'"
        )

    counts = db.counts()
    print(f"created {created} demo leads ({chatbot_count} chatbot, {meta_count} meta)")
    print(f"backfilled first response on {touched}")
    print(f"  hot {counts['hot']}  warm {counts['warm']}  cold {counts['cold']}  "
          f"spam {counts['spam']}  unassigned {counts['unassigned']}")
    print("\nOpen the dashboard: python -m crm.server")
    return 0


if __name__ == "__main__":
    sys.exit(main())
