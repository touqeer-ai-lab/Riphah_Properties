"""Session status lifecycle: active -> idle -> inactive (spec stage 9).

A visitor never tells you they have left. They close the tab, or open another one,
or put the phone down mid-sentence. So the transition is driven by silence, swept
on an interval, with both thresholds in config because five and thirty minutes are
proposals in the scope document rather than decisions.

What happens at `inactive` is the part that matters commercially: the session is
closed, the transcript is sealed, the lead is rescored a final time, and the
outbound payload is assembled and queued. That is the moment a half-finished
conversation becomes a lead the sales team can work — so getting it wrong means
either leads that arrive while the visitor is still typing, or leads that never
arrive at all.

Idle is deliberately not sealed. A visitor who comes back after eight minutes and
keeps typing should continue the same conversation, not start a second one that
splits their lead in two.
"""
from __future__ import annotations

import threading
from typing import Any

import config
from agent import conversations
from core import db
from leads import delivery, store


def sweep() -> dict[str, Any]:
    """One pass over stale sessions. Idempotent; safe to run as often as you like."""
    idle_cutoff = db.minutes_ago(config.IDLE_AFTER_MINUTES)
    inactive_cutoff = db.minutes_ago(config.INACTIVE_AFTER_MINUTES)

    # active -> idle
    with db.tx() as conn:
        idled = conn.execute(
            "UPDATE chat_sessions SET status = 'idle' "
            " WHERE status = 'active' AND last_activity_at < ? AND closed_at IS NULL",
            (idle_cutoff,),
        ).rowcount

    # Everything past the second threshold gets finalised, whether it reached
    # 'idle' first or jumped straight past it (a long gap between sweeps).
    stale = db.query(
        "SELECT id, portal_key, turn_count FROM chat_sessions "
        " WHERE status IN ('active','idle') AND last_activity_at < ? "
        "   AND closed_at IS NULL",
        (inactive_cutoff,),
    )

    finalised = 0
    delivered: list[dict[str, Any]] = []
    for session in stale:
        result = finalise(session["id"])
        finalised += 1
        if result.get("dispatched"):
            delivered.append(result["dispatched"])

    return {
        "idled": idled,
        "finalised": finalised,
        "dispatched": len(delivered),
        "idle_after_minutes": config.IDLE_AFTER_MINUTES,
        "inactive_after_minutes": config.INACTIVE_AFTER_MINUTES,
    }


def finalise(session_id: str) -> dict[str, Any]:
    """Close one session: seal it, rescore its lead, queue outbound delivery.

    Also called directly when a visitor explicitly ends a chat, which is why it is
    separate from `sweep()` — an explicit end should not wait thirty minutes for a
    lead to reach the CRM.
    """
    session = conversations.get(session_id)
    if not session or session["closed_at"]:
        return {"session_id": session_id, "closed": False, "reason": "already closed"}

    # Zero-turn sessions are noise from a widget load, not conversations.
    if not session["turn_count"]:
        conversations.delete(session_id)
        return {"session_id": session_id, "closed": True, "pruned": True}

    conversations.close(session_id, seal=True)

    lead = store.lead_for_session(session_id)
    if not lead:
        # No contact route was ever given. Recorded as a Cold outcome for
        # analytics — the capture-rate denominator needs these, and "how many
        # conversations produced nothing" is the number that tells you whether the
        # qualification pacing is working.
        db.audit("system", "session.closed_without_lead", entity="chat_session",
                 entity_id=session_id, detail={"turns": session["turn_count"]})
        return {"session_id": session_id, "closed": True, "lead": None}

    result = store.rescore(lead["id"])
    dispatched = None
    # Final delivery on close. `lead.updated` rather than `lead.created` when the
    # CRM already has it — creation fires as soon as the lead exists, so the sales
    # team sees a hot lead within seconds rather than half an hour later.
    already_sent = db.scalar(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE lead_id = ? "
        " AND event = 'lead.created'", (lead["id"],)
    )
    if not already_sent:
        dispatched = {"delivery_id": delivery.enqueue(lead["id"], "lead.created")}
        if result.get("qualification") == "hot":
            dispatched["alert"] = delivery.alert_hot_lead(lead["id"])
    else:
        dispatched = {"delivery_id": delivery.enqueue(lead["id"], "lead.updated")}

    return {
        "session_id": session_id,
        "closed": True,
        "lead": result,
        "dispatched": dispatched,
    }


class Sweeper(threading.Thread):
    """Background lifecycle sweeper.

    Interval comes from config and is much shorter than the idle threshold, so a
    session becomes idle within a minute of actually going quiet rather than at the
    next multiple of the threshold.
    """

    def __init__(self, interval_seconds: int | None = None) -> None:
        super().__init__(daemon=True, name="lifecycle-sweeper")
        self.interval = interval_seconds or config.LIFECYCLE_SWEEP_SECONDS
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                result = sweep()
                if result["idled"] or result["finalised"]:
                    print(f"[lifecycle] {result}")
            except Exception as exc:  # noqa: BLE001
                print(f"[lifecycle] sweeper error: {type(exc).__name__}: {exc}")

    def stop(self) -> None:
        self._stop.set()


if __name__ == "__main__":
    db.migrate()
    print(sweep())
