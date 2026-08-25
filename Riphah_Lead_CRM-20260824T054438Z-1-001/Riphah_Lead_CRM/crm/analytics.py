"""Lead analytics.

The brief asked for analytics on active versus inactive users and on the different
lead types arriving from the chatbot API. That is the core of what is here, plus
the two figures a property sales manager actually acts on: how fast hot leads get
called, and where the good leads come from.

Every number is computed from `captured_at` — when the *source* captured the lead —
rather than from `created_at`, which records when this service happened to be
running. A CRM restart backfilling four hours of webhooks would otherwise show as a
spike of leads at 3am.

Two definitions worth being explicit about, because "active user" is ambiguous:

* **Engaged** — the visitor held a real conversation (more than a trivial number of
  messages). This is a property of the visitor.
* **In play** — the lead is still open in the sales process: not converted, not
  lost, and touched recently. This is a property of the sales pipeline.

They are different questions and the dashboard reports both. Conflating them is how
you end up with an "active users" chart nobody can act on.
"""
from __future__ import annotations

from typing import Any

import config
from core import db

# A conversation of this many messages or more is treated as engagement rather
# than a bounce. Two is the floor — one question and one answer is a look, not a
# conversation.
ENGAGED_MESSAGE_THRESHOLD = 4


def _window(days: int | None) -> tuple[str, list[Any]]:
    if not days:
        return "1 = 1", []
    return "captured_at >= ?", [db.days_ago(days)]


def overview(*, days: int | None = None) -> dict[str, Any]:
    """Headline figures for the top of the dashboard."""
    days = days if days is not None else config.DEFAULT_ANALYTICS_DAYS
    where, params = _window(days)

    def n(extra: str = "", extra_params: list[Any] | None = None) -> int:
        clause = f"{where}{extra}"
        return int(db.scalar(
            f"SELECT COUNT(*) FROM leads WHERE {clause}",
            [*params, *(extra_params or [])],
        ) or 0)

    total = n()
    actionable = n(" AND qualification != 'spam'")
    hot = n(" AND qualification = 'hot'")
    warm = n(" AND qualification = 'warm'")
    cold = n(" AND qualification = 'cold'")
    spam = n(" AND qualification = 'spam'")
    contactable = n(" AND (email_norm IS NOT NULL OR phone_norm IS NOT NULL)")
    converted = n(" AND status = 'converted'")
    lost = n(" AND status = 'lost'")
    contacted = n(" AND first_response_at IS NOT NULL")
    unassigned = n(" AND assigned_owner IS NULL AND qualification IN ('hot','warm')")

    # Median rather than mean: one lead that sat for three weeks over a holiday
    # would drag a mean into meaninglessness, and the sales floor cares about the
    # typical case.
    response_times = [
        row["hours"] for row in db.query(
            f"SELECT ROUND((JULIANDAY(first_response_at) - JULIANDAY(captured_at)) "
            f"       * 24, 2) AS hours "
            f"  FROM leads WHERE {where} AND first_response_at IS NOT NULL "
            f"   AND qualification IN ('hot','warm')",
            params,
        ) if row["hours"] is not None
    ]
    response_times.sort()
    median_response = (
        response_times[len(response_times) // 2] if response_times else None
    )

    return {
        "window_days": days,
        "total_leads": total,
        "actionable_leads": actionable,
        "by_tier": {"hot": hot, "warm": warm, "cold": cold, "spam": spam},
        "contactable": contactable,
        # Of the leads a consultant could act on, how many have a way to be
        # reached. A low number here is a prompt-pacing problem, not a traffic one.
        "contactable_rate": round(contactable / actionable, 3) if actionable else 0.0,
        "contacted": contacted,
        "response_rate": round(contacted / actionable, 3) if actionable else 0.0,
        "converted": converted,
        "lost": lost,
        "conversion_rate": round(converted / actionable, 3) if actionable else 0.0,
        "median_response_hours": median_response,
        "unassigned_actionable": unassigned,
    }


def engagement(*, days: int | None = None) -> dict[str, Any]:
    """Active vs inactive visitors — the brief's explicit ask.

    Reported on two axes because they answer different questions. See the module
    docstring.
    """
    days = days if days is not None else config.DEFAULT_ANALYTICS_DAYS
    where, params = _window(days)
    dormant_cutoff = db.days_ago(config.DORMANT_AFTER_DAYS)

    row = db.one(
        f"""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN message_count >= ? THEN 1 ELSE 0 END) AS engaged,
          SUM(CASE WHEN message_count > 0 AND message_count < ? THEN 1 ELSE 0 END)
            AS brief,
          SUM(CASE WHEN message_count = 0 THEN 1 ELSE 0 END) AS no_conversation,
          -- Not aliased `returning`: that is a reserved keyword in SQLite 3.35+
          -- (RETURNING clause) and an unquoted alias is a syntax error.
          SUM(CASE WHEN session_count > 1 THEN 1 ELSE 0 END) AS returning_visitors,
          ROUND(AVG(message_count), 1) AS avg_messages,
          MAX(message_count) AS max_messages
          FROM leads WHERE {where} AND qualification != 'spam'
        """,
        [ENGAGED_MESSAGE_THRESHOLD, ENGAGED_MESSAGE_THRESHOLD, *params],
    ) or {}

    pipeline = db.one(
        f"""
        SELECT
          SUM(CASE WHEN status IN ('converted','lost') THEN 1 ELSE 0 END) AS closed,
          SUM(CASE WHEN status NOT IN ('converted','lost','spam')
                    AND updated_at >= ? THEN 1 ELSE 0 END) AS in_play,
          SUM(CASE WHEN status NOT IN ('converted','lost','spam')
                    AND updated_at < ? THEN 1 ELSE 0 END) AS dormant
          FROM leads WHERE {where} AND qualification != 'spam'
        """,
        [dormant_cutoff, dormant_cutoff, *params],
    ) or {}

    total = int(row.get("total") or 0)
    return {
        "window_days": days,
        "engaged_threshold_messages": ENGAGED_MESSAGE_THRESHOLD,
        "visitor": {
            "engaged": int(row.get("engaged") or 0),
            "brief": int(row.get("brief") or 0),
            "no_conversation": int(row.get("no_conversation") or 0),
            "returning": int(row.get("returning_visitors") or 0),
        },
        "pipeline": {
            "in_play": int(pipeline.get("in_play") or 0),
            "dormant": int(pipeline.get("dormant") or 0),
            "closed": int(pipeline.get("closed") or 0),
        },
        "dormant_after_days": config.DORMANT_AFTER_DAYS,
        "avg_messages": row.get("avg_messages") or 0,
        "max_messages": int(row.get("max_messages") or 0),
        "engagement_rate": (
            round(int(row.get("engaged") or 0) / total, 3) if total else 0.0
        ),
    }


def by_source(*, days: int | None = None) -> list[dict[str, Any]]:
    """Source mix, with quality per source — not just volume.

    Volume alone is misleading: a channel producing four times the leads at a third
    of the hot rate is not four times as valuable, and paid social is exactly the
    channel where that happens. Every source row therefore carries its own hot rate
    and conversion rate.
    """
    days = days if days is not None else config.DEFAULT_ANALYTICS_DAYS
    where, params = _window(days)
    rows = db.query(
        f"""
        SELECT s.key, s.display_name, s.status, s.detail, s.last_sync_at,
               COUNT(l.id) AS leads,
               SUM(CASE WHEN l.qualification = 'hot' THEN 1 ELSE 0 END) AS hot,
               SUM(CASE WHEN l.qualification = 'warm' THEN 1 ELSE 0 END) AS warm,
               SUM(CASE WHEN l.qualification = 'cold' THEN 1 ELSE 0 END) AS cold,
               SUM(CASE WHEN l.qualification = 'spam' THEN 1 ELSE 0 END) AS spam,
               SUM(CASE WHEN l.status = 'converted' THEN 1 ELSE 0 END) AS converted,
               ROUND(AVG(l.score), 1) AS avg_score
          FROM sources s
          LEFT JOIN leads l ON l.source_key = s.key AND {where.replace('captured_at', 'l.captured_at')}
         GROUP BY s.key
         ORDER BY leads DESC
        """,
        params,
    )
    for row in rows:
        actionable = (row["leads"] or 0) - (row["spam"] or 0)
        row["actionable"] = actionable
        row["hot_rate"] = round((row["hot"] or 0) / actionable, 3) if actionable else 0.0
        row["conversion_rate"] = (
            round((row["converted"] or 0) / actionable, 3) if actionable else 0.0
        )
    return rows


def timeseries(*, days: int = 30, bucket: str = "day") -> list[dict[str, Any]]:
    """Daily lead volume split by tier — the trend chart.

    Gaps are filled with zeroes. A line chart that skips quiet days compresses
    them out of existence and makes a flat week look like steady growth.
    """
    import datetime as dt

    fmt = "%Y-%W" if bucket == "week" else "%Y-%m-%d"
    rows = db.query(
        """
        SELECT STRFTIME(?, captured_at) AS period,
               COUNT(*) AS total,
               SUM(CASE WHEN qualification = 'hot' THEN 1 ELSE 0 END) AS hot,
               SUM(CASE WHEN qualification = 'warm' THEN 1 ELSE 0 END) AS warm,
               SUM(CASE WHEN qualification = 'cold' THEN 1 ELSE 0 END) AS cold,
               SUM(CASE WHEN qualification = 'spam' THEN 1 ELSE 0 END) AS spam
          FROM leads WHERE captured_at >= ?
         GROUP BY period ORDER BY period
        """,
        (fmt, db.days_ago(days)),
    )
    found = {row["period"]: row for row in rows}

    out: list[dict[str, Any]] = []
    today = dt.datetime.now(dt.timezone.utc).date()
    if bucket == "day":
        for offset in range(days, -1, -1):
            key = (today - dt.timedelta(days=offset)).isoformat()
            row = found.get(key)
            out.append(row or {"period": key, "total": 0, "hot": 0, "warm": 0,
                               "cold": 0, "spam": 0})
    else:
        out = rows
    return out


def by_field(field_key: str, *, days: int | None = None,
             limit: int = 12) -> list[dict[str, Any]]:
    """Distribution of one captured field — budget band, project, timeline.

    Works for any field key from any source, because captured fields are rows. That
    is what lets this same function chart `programme_of_interest` the day the
    admission portal goes live, with no change here.
    """
    days = days if days is not None else config.DEFAULT_ANALYTICS_DAYS
    where, params = _window(days)
    return db.query(
        f"""
        SELECT f.value AS value, COUNT(*) AS leads,
               SUM(CASE WHEN l.qualification = 'hot' THEN 1 ELSE 0 END) AS hot,
               ROUND(AVG(l.score), 1) AS avg_score
          FROM lead_fields f JOIN leads l ON l.id = f.lead_id
         WHERE f.field_key = ? AND l.qualification != 'spam'
           AND {where.replace('captured_at', 'l.captured_at')}
         GROUP BY f.value ORDER BY leads DESC LIMIT ?
        """,
        [field_key, *params, limit],
    )


def budget_bands(*, days: int | None = None) -> list[dict[str, Any]]:
    """Budget distribution in Pakistani bands, which is how the sales team thinks.

    Bucketed in SQL rather than in Python so the chart works over any volume, and
    labelled in crore/lakh rather than in raw rupees — "25000000" is not a label a
    sales manager reads.
    """
    days = days if days is not None else config.DEFAULT_ANALYTICS_DAYS
    where, params = _window(days)
    rows = db.query(
        f"""
        SELECT CAST(f.value AS INTEGER) AS amount, l.qualification
          FROM lead_fields f JOIN leads l ON l.id = f.lead_id
         WHERE f.field_key IN ('budget_max','fee_budget')
           AND l.qualification != 'spam'
           AND CAST(f.value AS INTEGER) > 0
           AND {where.replace('captured_at', 'l.captured_at')}
        """,
        params,
    )

    bands = [
        ("Under 50 lakh", 0, 5_000_000),
        ("50 lakh – 1 crore", 5_000_000, 10_000_000),
        ("1 – 2 crore", 10_000_000, 20_000_000),
        ("2 – 3 crore", 20_000_000, 30_000_000),
        ("3 – 5 crore", 30_000_000, 50_000_000),
        ("5 crore +", 50_000_000, 10**15),
    ]
    out = [{"band": label, "leads": 0, "hot": 0} for label, _, _ in bands]
    for row in rows:
        for index, (_, low, high) in enumerate(bands):
            if low <= row["amount"] < high:
                out[index]["leads"] += 1
                if row["qualification"] == "hot":
                    out[index]["hot"] += 1
                break
    return out


def owner_performance(*, days: int | None = None) -> list[dict[str, Any]]:
    """Per-owner load and speed. Answers "who is accountable" from s13."""
    days = days if days is not None else config.DEFAULT_ANALYTICS_DAYS
    where, params = _window(days)
    rows = db.query(
        f"""
        SELECT assigned_owner AS owner, COUNT(*) AS assigned,
               SUM(CASE WHEN status = 'converted' THEN 1 ELSE 0 END) AS converted,
               SUM(CASE WHEN status = 'new' THEN 1 ELSE 0 END) AS untouched,
               SUM(CASE WHEN qualification = 'hot' THEN 1 ELSE 0 END) AS hot,
               ROUND(AVG(CASE WHEN first_response_at IS NOT NULL
                    THEN (JULIANDAY(first_response_at) - JULIANDAY(captured_at)) * 24
                    END), 1) AS avg_response_hours
          FROM leads
         WHERE {where} AND assigned_owner IS NOT NULL AND qualification != 'spam'
         GROUP BY assigned_owner ORDER BY assigned DESC
        """,
        params,
    )
    for row in rows:
        row["conversion_rate"] = (
            round((row["converted"] or 0) / row["assigned"], 3)
            if row["assigned"] else 0.0
        )
    return rows


def sla_breaches(*, limit: int = 25) -> dict[str, Any]:
    """Leads past their response target, worst first.

    The targets are placeholders — the scope document lists hot-lead response time
    as an open item — so the response says so explicitly. A dashboard number that
    looks agreed when it is not causes arguments later.
    """
    rows = db.query(
        """
        SELECT id, source_key, external_id, name, phone, email, qualification,
               score, status, assigned_owner, captured_at, first_response_at,
               ROUND((JULIANDAY('now') - JULIANDAY(captured_at)) * 24, 1) AS age_hours
          FROM leads
         WHERE qualification IN ('hot','warm')
           AND first_response_at IS NULL
           AND status = 'new'
         ORDER BY CASE qualification WHEN 'hot' THEN 0 ELSE 1 END, captured_at
         LIMIT ?
        """,
        (limit * 4,),
    )

    breached = []
    for row in rows:
        target = config.SLA_HOURS.get(row["qualification"], 0)
        if target and (row["age_hours"] or 0) > target:
            row["sla_target_hours"] = target
            row["hours_over"] = round((row["age_hours"] or 0) - target, 1)
            breached.append(row)

    return {
        "breaches": breached[:limit],
        "count": len(breached),
        "targets_hours": config.SLA_HOURS,
        "targets_provisional": True,
        "note": ("Response targets are placeholders. The scope document lists hot-"
                 "lead response time and accountability as an open item (s13)."),
    }


def attribution(*, days: int | None = None, limit: int = 12) -> list[dict[str, Any]]:
    """Campaign performance, so spend can be pointed at what produces hot leads."""
    days = days if days is not None else config.DEFAULT_ANALYTICS_DAYS
    where, params = _window(days)
    rows = db.query(
        f"""
        SELECT COALESCE(utm_campaign, referrer, 'direct') AS campaign,
               COALESCE(utm_source, 'direct') AS source,
               COUNT(*) AS leads,
               SUM(CASE WHEN qualification = 'hot' THEN 1 ELSE 0 END) AS hot,
               SUM(CASE WHEN status = 'converted' THEN 1 ELSE 0 END) AS converted,
               ROUND(AVG(score), 1) AS avg_score
          FROM leads WHERE {where} AND qualification != 'spam'
         GROUP BY campaign, source ORDER BY leads DESC LIMIT ?
        """,
        [*params, limit],
    )
    for row in rows:
        row["hot_rate"] = round((row["hot"] or 0) / row["leads"], 3) if row["leads"] else 0.0
    return rows


def channel_mix(*, days: int | None = None) -> list[dict[str, Any]]:
    """Text vs voice vs form. Tells you whether building voice was worth it."""
    days = days if days is not None else config.DEFAULT_ANALYTICS_DAYS
    where, params = _window(days)
    return db.query(
        f"""
        SELECT COALESCE(channel, 'unknown') AS channel, COUNT(*) AS leads,
               SUM(CASE WHEN qualification = 'hot' THEN 1 ELSE 0 END) AS hot,
               ROUND(AVG(message_count), 1) AS avg_messages
          FROM leads WHERE {where} AND qualification != 'spam'
         GROUP BY channel ORDER BY leads DESC
        """,
        params,
    )


def funnel(*, days: int | None = None) -> list[dict[str, Any]]:
    """Captured -> contactable -> contacted -> qualified -> converted.

    Each stage is a subset of the one above, so the biggest single drop is the
    thing to fix. Usually it is contactable→contacted, which is a staffing
    problem, not a chatbot problem — and being able to show that is the point.
    """
    days = days if days is not None else config.DEFAULT_ANALYTICS_DAYS
    where, params = _window(days)

    def n(extra: str) -> int:
        return int(db.scalar(
            f"SELECT COUNT(*) FROM leads WHERE {where} AND qualification != 'spam'"
            f" {extra}", params
        ) or 0)

    captured = n("")
    stages = [
        ("Leads captured", captured),
        ("Contactable", n(" AND (email_norm IS NOT NULL OR phone_norm IS NOT NULL)")),
        ("Contacted", n(" AND first_response_at IS NOT NULL")),
        ("Qualified", n(" AND status IN ('qualified','converted')")),
        ("Converted", n(" AND status = 'converted'")),
    ]
    out = []
    for index, (label, count) in enumerate(stages):
        previous = stages[index - 1][1] if index else count
        out.append({
            "stage": label,
            "count": count,
            "of_total": round(count / captured, 3) if captured else 0.0,
            "step_conversion": round(count / previous, 3) if previous else 0.0,
            "dropped": max(0, previous - count) if index else 0,
        })
    return out


def dashboard(*, days: int | None = None) -> dict[str, Any]:
    """One call for the whole dashboard.

    Bundled because the dashboard needs all of it before it can render anything
    meaningful, and eleven parallel requests to a SQLite file is slower than one.
    """
    days = days if days is not None else config.DEFAULT_ANALYTICS_DAYS
    return {
        "overview": overview(days=days),
        "engagement": engagement(days=days),
        "sources": by_source(days=days),
        "timeseries": timeseries(days=min(days, 90)),
        "funnel": funnel(days=days),
        "budget_bands": budget_bands(days=days),
        "projects": by_field("project", days=days),
        "timelines": by_field("timeline", days=days),
        "purposes": by_field("purpose", days=days),
        "property_types": by_field("property_type", days=days),
        "channels": channel_mix(days=days),
        "attribution": attribution(days=days),
        "owners": owner_performance(days=days),
        "sla": sla_breaches(),
        "counts": db.counts(),
    }


def export_csv(*, days: int | None = None, include_spam: bool = False) -> str:
    """Flat CSV for the sales team's spreadsheet (scope document stage 11).

    Captured fields are pivoted into columns from whatever keys actually exist in
    the window, so a new portal field appears in the export automatically rather
    than needing this function edited.
    """
    import csv
    import io

    days = days if days is not None else config.DEFAULT_ANALYTICS_DAYS
    where, params = _window(days)
    spam_clause = "" if include_spam else " AND qualification != 'spam'"

    leads = db.query(
        f"SELECT * FROM leads WHERE {where}{spam_clause} ORDER BY captured_at DESC",
        params,
    )
    if not leads:
        return "no leads in window\n"

    ids = [row["id"] for row in leads]
    placeholders = ",".join("?" * len(ids))
    field_rows = db.query(
        f"SELECT lead_id, field_key, value FROM lead_fields "
        f" WHERE lead_id IN ({placeholders})", ids
    )
    by_lead: dict[int, dict[str, Any]] = {}
    field_keys: set[str] = set()
    for row in field_rows:
        by_lead.setdefault(row["lead_id"], {})[row["field_key"]] = row["value"]
        field_keys.add(row["field_key"])
    field_keys.discard("_unmapped")

    core = ["external_id", "source_key", "portal", "name", "email", "phone",
            "qualification", "score", "status", "assigned_owner", "language",
            "channel", "device", "region", "utm_source", "utm_campaign",
            "message_count", "session_count", "consent_given", "captured_at",
            "first_response_at", "response_hours"]
    columns = core + sorted(field_keys)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        row = {key: lead.get(key) for key in core}
        row["response_hours"] = db.hours_between(lead["captured_at"],
                                                 lead["first_response_at"])
        row.update(by_lead.get(lead["id"], {}))
        writer.writerow(row)
    return buffer.getvalue()
