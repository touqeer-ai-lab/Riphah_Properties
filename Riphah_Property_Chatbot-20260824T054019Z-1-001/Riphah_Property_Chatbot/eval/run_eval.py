"""Guardrail regression runner.

    python eval/run_eval.py                    # every case
    python eval/run_eval.py --retrieval        # KB assertions only, no API calls, free
    python eval/run_eval.py --id price-refused-under-pressure
    python eval/run_eval.py --filter price     # every case whose id contains 'price'
    python eval/run_eval.py --json out.json

Assertions are mechanical — regex and field equality — not model-graded. That is a
deliberate choice: the properties being defended (never state a price, never invent
a document requirement, never leak the prompt) are exactly the ones an LLM judge is
worst at, because the judge shares the generator's blind spots. A regex that says
"no seven-digit number appeared in this reply" cannot be talked round.

Run `--retrieval` while iterating on content or the chunker. If a fact is not in
the corpus, no amount of prompt work will produce it, so a failure there is always
the ingest pipeline's problem and never the model's.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from agent import chat, conversations  # noqa: E402
from core import db  # noqa: E402
from kb import retrieve  # noqa: E402
from kb.vector_store import STORE  # noqa: E402

CASES_PATH = Path(__file__).parent / "test_cases.json"
PORTAL = "riphah-property"


# --------------------------------------------------------------- retrieval set

# Free assertions over the knowledge base. Each is (query, must-appear-in-a-passage).
# These check the corpus, not the model — a failure here is a content or chunker
# problem.
RETRIEVAL_CHECKS: list[tuple[str, str]] = [
    ("what documents does an overseas buyer need", "NICOP"),
    ("can I set up a clinic there", "medical suite"),
    ("how do instalment payments work", "quarterly"),
    ("what is shell and core", "shell-and-core"),
    ("can I sell before completing payments", "resale"),
    ("what charges are outside the unit price", "transfer"),
    ("what happens at handover", "possession"),
    ("bank financing available", "bank"),
    ("what is DHA Business District", "DHA"),
    ("who maintains the roads and security", "DHA"),
    ("cancellation and refund", "cancellation"),
    ("power of attorney for overseas buyers", "attorney"),
]


def run_retrieval() -> tuple[int, int]:
    """KB assertions. No API calls beyond the query embedding; no model calls."""
    print("retrieval checks (corpus, not model)\n")
    passed = failed = 0

    try:
        size = STORE.reload()
    except Exception as exc:  # noqa: BLE001
        print(f"  vector store unavailable: {exc}")
        size = 0
    print(f"  vector store: {size} live passages")
    if not size:
        print("  ! corpus is empty — run: python -m kb.build")
        return 0, len(RETRIEVAL_CHECKS)

    for query, needle in RETRIEVAL_CHECKS:
        result = retrieve.search(query, portal_key=PORTAL)
        blob = " ".join(p["text"] for p in result["passages"]).lower()
        ok = result["found"] and needle.lower() in blob
        passed += ok
        failed += not ok
        marker = "ok  " if ok else "FAIL"
        print(f"  {marker} {query[:52]:54} sim={result['top_similarity']:.3f}"
              + ("" if ok else f"  (missing {needle!r})"))

    # The volatile document must never surface under the default pricing mode.
    priced = retrieve.search("indicative price band medical suite", portal_key=PORTAL)
    leaked = any(p.get("classification") == "volatile" for p in priced["passages"])
    passed += not leaked
    failed += leaked
    print(f"  {'ok  ' if not leaked else 'FAIL'} volatile passages withheld under "
          f"pricing_mode=refer")

    # And the restricted document must not be in the database at all.
    restricted = db.scalar(
        "SELECT COUNT(*) FROM kb_documents WHERE classification = 'restricted' "
        "   OR slug = 'internal-cost-sheet'"
    )
    ok = restricted == 0
    passed += ok
    failed += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} restricted document refused at ingest, not "
          f"stored{'' if ok else f' ({restricted} rows found)'}")

    return passed, failed


# ------------------------------------------------------------------- agent set

def assert_case(case: dict[str, Any], reply: str, result: dict[str, Any],
                captured: dict[str, Any], money_re: re.Pattern) -> list[str]:
    """Return a list of failure descriptions; empty means the case passed."""
    problems: list[str] = []
    lowered = reply.lower()

    if case.get("min_length") and len(reply) < case["min_length"]:
        problems.append(f"reply too short ({len(reply)} < {case['min_length']})")

    if case.get("forbid_money"):
        hit = money_re.search(reply)
        if hit:
            problems.append(f"money leaked: {hit.group(0)!r}")

    if case.get("forbid_question") and "?" in reply:
        tail = reply[max(0, reply.rfind("?") - 60):reply.rfind("?") + 1]
        problems.append(f"asked a question on the opening turn: …{tail!r}")

    if case.get("forbid_pattern"):
        hit = re.search(case["forbid_pattern"], reply, re.IGNORECASE)
        if hit:
            problems.append(f"forbidden pattern matched: {hit.group(0)!r}")

    if case.get("require_pattern"):
        if not re.search(case["require_pattern"], reply, re.IGNORECASE):
            problems.append(f"required pattern absent: {case['require_pattern']!r}")

    if case.get("require_tool"):
        tools = [t["tool"] for t in result.get("trace", [])]
        if case["require_tool"] not in tools:
            problems.append(f"tool {case['require_tool']!r} not called (saw {tools})")

    for field_key, expected in (case.get("require_field") or {}).items():
        actual = captured.get(field_key)
        if str(actual) != str(expected):
            problems.append(f"field {field_key}: got {actual!r}, want {expected!r}")

    for field_key in (case.get("forbid_field") or []):
        if captured.get(field_key) not in (None, ""):
            problems.append(
                f"field {field_key} was set to {captured[field_key]!r} but the "
                f"visitor only asked about it")

    del lowered
    return problems


def run_case(case: dict[str, Any], money_re: re.Pattern,
             verbose: bool = False) -> dict[str, Any]:
    session_id = conversations.create(
        portal_key=PORTAL, visitor_id=f"eval-{case['id']}"
    )["id"]

    started = time.time()
    result: dict[str, Any] = {}
    reply = ""
    try:
        for turn in case["turns"]:
            result = chat.answer(turn, session_id=session_id, portal_key=PORTAL)
            reply = result["answer"]
            if verbose:
                print(f"      > {turn}")
                print(f"      < {reply[:220]}")
    except Exception as exc:  # noqa: BLE001
        return {"id": case["id"], "ok": False, "elapsed": time.time() - started,
                "problems": [f"{type(exc).__name__}: {exc}"], "reply": ""}

    captured = result.get("captured", {})
    problems = assert_case(case, reply, result, captured, money_re)

    # Sessions are deleted so a re-run is clean and the eval does not pollute the
    # lead table with fictional buyers.
    conversations.delete(session_id)

    return {
        "id": case["id"],
        "ok": not problems,
        "elapsed": round(time.time() - started, 1),
        "problems": problems,
        "reply": reply,
        "tools": [t["tool"] for t in result.get("trace", [])],
        "captured": captured,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Guardrail regression suite")
    parser.add_argument("--retrieval", action="store_true",
                        help="corpus assertions only — free, no model calls")
    parser.add_argument("--id", action="append", help="run only these case ids")
    parser.add_argument("--filter", help="run cases whose id contains this")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--json", help="write full results to this path")
    args = parser.parse_args()

    db.migrate()
    spec = json.loads(CASES_PATH.read_text())
    money_re = re.compile(spec["money_pattern"], re.IGNORECASE)

    r_passed, r_failed = run_retrieval()
    if args.retrieval:
        print(f"\nretrieval: {r_passed} passed, {r_failed} failed")
        return 1 if r_failed else 0

    if not config.has_openai_key():
        print("\nOPENAI_API_KEY is not set — cannot run the agent cases.")
        return 2

    cases = spec["cases"]
    if args.id:
        cases = [c for c in cases if c["id"] in args.id]
    if args.filter:
        cases = [c for c in cases if args.filter in c["id"]]
    if not cases:
        print("no cases matched")
        return 1

    print(f"\nagent cases ({len(cases)}) · model {config.CHAT_MODEL}\n")
    results = []
    for index, case in enumerate(cases, start=1):
        print(f"  [{index:2}/{len(cases)}] {case['id']}", flush=True)
        if args.verbose:
            print(f"      {case['description']}")
        outcome = run_case(case, money_re, verbose=args.verbose)
        results.append(outcome)
        if outcome["ok"]:
            print(f"      ok   ({outcome['elapsed']}s)")
        else:
            print(f"      FAIL ({outcome['elapsed']}s)")
            for problem in outcome["problems"]:
                print(f"        - {problem}")
            if not args.verbose:
                print(f"        reply: {outcome['reply'][:220]}")

    passed = sum(1 for r in results if r["ok"])
    failed = len(results) - passed

    print(f"\n{'=' * 62}")
    print(f"retrieval  {r_passed} passed  {r_failed} failed")
    print(f"agent      {passed} passed  {failed} failed")
    if failed:
        print("\nfailed cases:")
        for outcome in results:
            if not outcome["ok"]:
                print(f"  - {outcome['id']}: {outcome['problems'][0]}")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "model": config.CHAT_MODEL,
            "retrieval": {"passed": r_passed, "failed": r_failed},
            "agent": {"passed": passed, "failed": failed},
            "results": results,
        }, indent=2, ensure_ascii=False))
        print(f"\nwrote {args.json}")

    return 1 if (failed or r_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
