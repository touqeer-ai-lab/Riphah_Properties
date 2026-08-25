"""Outbound lead delivery: signed webhooks with durable retry (spec stage 10).

The requirement is "a CRM outage never loses a lead". That rules out fire-and-
forget HTTP from the request path, so delivery is a queue: every event becomes a
`webhook_deliveries` row before any network call, and a background sweeper works
the backlog with exponential backoff. If the CRM is down for six hours, the leads
are still there when it comes back.

Signing is not optional. With no `WEBHOOK_SECRET`, delivery is **disabled** rather
than sent unsigned — an unsigned lead webhook is an unauthenticated write into the
CRM, and the failure mode of "we sent it but nobody could verify it" is worse than
"we queued it and told you why it didn't go".

The payload is frozen at enqueue time and stored verbatim, then signed on each
attempt. That matters for retries: a lead enriched between attempt 1 and attempt 4
must not change the bytes under a signature, and the CRM should receive the state
that generated the event, not a later one.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

import config
from core import db, security
from leads import store

EVENTS = ("lead.created", "lead.updated")


def enabled() -> tuple[bool, str | None]:
    """(can_deliver, reason_if_not). Checked before every enqueue so the reason is
    recorded against the delivery row rather than discovered in a log."""
    if not config.WEBHOOK_URL:
        return False, "LEAD_WEBHOOK_URL is not configured"
    if not config.WEBHOOK_SECRET:
        return False, "WEBHOOK_SECRET is not configured; refusing to send unsigned"
    return True, None


def enqueue(lead_id: int, event: str, *, target_url: str | None = None) -> int | None:
    """Queue one event. Returns the delivery row id, or None if there's nothing to send.

    A delivery row is created even when delivery is disabled, with status `failed`
    and the reason — so a misconfigured deployment shows up as visible failed
    deliveries in the log instead of silence.
    """
    if event not in EVENTS:
        raise ValueError(f"event must be one of {EVENTS}")

    body = store.payload(lead_id)
    if not body:
        return None
    body = {"event": event, "sent_at": db.now(), "data": body}

    url = target_url or config.WEBHOOK_URL
    can_send, reason = enabled()

    with db.tx() as conn:
        cur = conn.execute(
            """
            INSERT INTO webhook_deliveries (lead_id, event, target_url, payload,
                                            status, next_attempt_at, last_error,
                                            created_at)
                 VALUES (?,?,?,?,?,?,?,?)
            """,
            (lead_id, event, url or "(unset)", db.dumps(body),
             "pending" if can_send else "failed",
             db.now() if can_send else None,
             None if can_send else reason, db.now()),
        )
        return cur.lastrowid


def _attempt(row: dict[str, Any]) -> dict[str, Any]:
    """One HTTP attempt. Never raises — the outcome is recorded, not thrown."""
    import httpx

    body = row["payload"].encode()
    try:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "RiphahPropertyAssistant/1.0",
            "X-Riphah-Event": row["event"],
            "X-Riphah-Delivery": str(row["id"]),
            **security.sign_payload(body),
        }
    except RuntimeError as exc:
        return {"ok": False, "status_code": None, "error": str(exc), "terminal": True}

    try:
        response = httpx.post(row["target_url"], content=body, headers=headers,
                              timeout=config.WEBHOOK_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status_code": None,
                "error": f"{type(exc).__name__}: {exc}"[:500], "terminal": False}

    ok = 200 <= response.status_code < 300
    # 4xx other than 408/429 is the receiver saying the request is wrong. Retrying
    # an unacceptable payload just burns the backoff schedule, so stop.
    terminal = (
        not ok
        and 400 <= response.status_code < 500
        and response.status_code not in (408, 429)
    )
    return {
        "ok": ok,
        "status_code": response.status_code,
        "error": None if ok else f"HTTP {response.status_code}: {response.text[:300]}",
        "terminal": terminal,
    }


def flush(*, limit: int = 25) -> dict[str, int]:
    """Work the due backlog once. Returns counts of what happened."""
    due = db.query(
        "SELECT * FROM webhook_deliveries "
        " WHERE status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
        " ORDER BY id LIMIT ?",
        (db.now(), limit),
    )
    delivered = failed = retrying = 0

    for row in due:
        outcome = _attempt(row)
        attempts = row["attempts"] + 1

        if outcome["ok"]:
            with db.tx() as conn:
                conn.execute(
                    "UPDATE webhook_deliveries SET status='delivered', attempts=?, "
                    "last_status_code=?, last_error=NULL, delivered_at=?, "
                    "next_attempt_at=NULL WHERE id=?",
                    (attempts, outcome["status_code"], db.now(), row["id"]),
                )
            delivered += 1
            continue

        exhausted = attempts >= config.WEBHOOK_MAX_ATTEMPTS or outcome["terminal"]
        if exhausted:
            with db.tx() as conn:
                conn.execute(
                    "UPDATE webhook_deliveries SET status='failed', attempts=?, "
                    "last_status_code=?, last_error=?, next_attempt_at=NULL "
                    "WHERE id=?",
                    (attempts, outcome["status_code"], outcome["error"], row["id"]),
                )
            failed += 1
            print(f"[delivery] giving up on {row['event']} delivery {row['id']}: "
                  f"{outcome['error']}")
            continue

        delay = config.WEBHOOK_RETRY_DELAYS[
            min(attempts - 1, len(config.WEBHOOK_RETRY_DELAYS) - 1)
        ]
        with db.tx() as conn:
            conn.execute(
                "UPDATE webhook_deliveries SET attempts=?, last_status_code=?, "
                "last_error=?, next_attempt_at=? WHERE id=?",
                (attempts, outcome["status_code"], outcome["error"],
                 db.seconds_from_now(delay), row["id"]),
            )
        retrying += 1

    return {"attempted": len(due), "delivered": delivered, "failed": failed,
            "retrying": retrying}


def retry(delivery_id: int) -> bool:
    """Re-queue a failed delivery. The manual button in the delivery log."""
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE webhook_deliveries SET status='pending', attempts=0, "
            "next_attempt_at=?, last_error=NULL WHERE id=? AND status='failed'",
            (db.now(), delivery_id),
        )
    return cur.rowcount > 0


def log(*, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    """The delivery log the dashboard shows (spec stage 10)."""
    clauses, params = ["1 = 1"], []
    if status:
        clauses.append("d.status = ?")
        params.append(status)
    params.append(limit)
    return db.query(
        f"""
        SELECT d.id, d.event, d.status, d.attempts, d.last_status_code,
               d.last_error, d.next_attempt_at, d.created_at, d.delivered_at,
               d.target_url, l.lead_ref
          FROM webhook_deliveries d
          LEFT JOIN leads l ON l.id = d.lead_id
         WHERE {' AND '.join(clauses)}
         ORDER BY d.id DESC LIMIT ?
        """,
        params,
    )


def alert_hot_lead(lead_id: int) -> dict[str, Any]:
    """Immediate notification for a Hot lead (spec stage 10).

    Email and WhatsApp/SMS are both open items in the scope document: nobody has
    confirmed the channel, the sender identity, or who owns the number. So this
    records the alert as a note and returns what *would* be sent, rather than
    guessing at a provider. Wiring a real channel is one function body, and the
    delivery record already exists to hang it off.
    """
    body = store.payload(lead_id)
    if not body:
        return {"sent": False, "reason": "no such lead"}

    contact = body["contact"]
    summary = (
        f"HOT LEAD {body['lead_id']} — "
        f"{contact.get('name') or 'name not given'}, "
        f"{contact.get('phone') or contact.get('email') or 'no contact'}. "
        f"Score {body['score']}. "
        f"Fields: {json.dumps(body['portal_fields'], ensure_ascii=False)[:300]}"
    )

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO notes (lead_id, author, body, created_at) VALUES (?,?,?,?)",
            (lead_id, "system", f"[hot lead alert] {summary}", db.now()),
        )
    db.audit("system", "lead.hot_alert", entity="lead", entity_id=lead_id,
             detail={"channel": "pending_client_decision"})

    return {
        "sent": False,
        "reason": "no alert channel configured (email/WhatsApp/SMS is a client "
                  "decision — see scope document s13)",
        "would_send": summary,
        "callback_target": body["action"].get("callback_target"),
    }


# ------------------------------------------------------------------ background

class Sweeper(threading.Thread):
    """Background thread that flushes the delivery queue on an interval.

    A daemon thread rather than an external worker because the whole point of the
    SQLite deployment is that there is one process to run. If this grows into
    multiple workers, the `next_attempt_at` claim needs a lock — noted here because
    it is the thing that will silently double-send.
    """

    def __init__(self, interval_seconds: int = 30) -> None:
        super().__init__(daemon=True, name="webhook-sweeper")
        self.interval = interval_seconds
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                result = flush()
                if result["attempted"]:
                    print(f"[delivery] {result}")
            except Exception as exc:  # noqa: BLE001
                print(f"[delivery] sweeper error: {type(exc).__name__}: {exc}")

    def stop(self) -> None:
        self._stop.set()


def dispatch(result: dict[str, Any]) -> dict[str, Any]:
    """Turn a `store.apply_extraction` result into outbound events.

    Called from the chat path. Enqueues rather than sends, so a slow or dead CRM
    never shows up as latency in the visitor's chat.
    """
    if not result or not result.get("lead_id"):
        return {}

    out: dict[str, Any] = {}
    if result.get("created"):
        out["delivery_id"] = enqueue(result["lead_id"], "lead.created")
    elif result.get("changed_fields") or result.get("tier_changed"):
        out["delivery_id"] = enqueue(result["lead_id"], "lead.updated")

    # Alert on the transition into hot, not on every turn while it stays hot.
    if result.get("qualification") == "hot" and result.get("tier_changed"):
        out["alert"] = alert_hot_lead(result["lead_id"])

    return out
