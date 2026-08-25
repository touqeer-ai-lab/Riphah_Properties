"""Knowledge base build orchestrator.

    python -m kb.build                    # seed portals, ingest content/, chunk, embed
    python -m kb.build --status           # what's in there
    python -m kb.build --only chunk       # one stage
    python -m kb.build --skip embed       # everything except the paid stage
    python -m kb.build --rebuild          # re-chunk and re-embed from stored text
    python -m kb.build --gaps             # the content backlog

Stages run in dependency order: seed -> ingest -> chunk -> embed. Only `embed`
costs money, and it is the last stage, so an ingest or chunker change can be
iterated on for free.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

import config
from core import db
from kb import chunk, embed, ingest, retrieve
from kb.vector_store import STORE

STAGES = ("seed", "ingest", "chunk", "embed")


def stage_seed(**_: Any) -> dict[str, Any]:
    from portals import seed

    return seed.run()


def stage_ingest(*, portal_key: str, **_: Any) -> dict[str, Any]:
    results = ingest.ingest_directory(portal_key=portal_key, publish=True)
    summary: dict[str, Any] = {}
    for item in results:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    problems = [r for r in results if r["status"] in ("error", "refused")]
    for problem in problems:
        print(f"    ! {problem['slug']}: {problem['reason']}", file=sys.stderr)
    return {"documents": len(results), **summary}


def stage_chunk(*, portal_key: str | None = None, rebuild: bool = False,
                **_: Any) -> dict[str, Any]:
    return chunk.run(portal_key=portal_key, rebuild=rebuild)


def stage_embed(*, portal_key: str | None = None, rebuild: bool = False,
                **_: Any) -> dict[str, Any]:
    if not config.has_openai_key():
        print("    ! OPENAI_API_KEY not set — skipping embeddings. Retrieval will "
              "fall back to keyword search only.", file=sys.stderr)
        return {"skipped": "no api key"}
    return embed.run(portal_key=portal_key, rebuild=rebuild)


RUNNERS = {
    "seed": stage_seed,
    "ingest": stage_ingest,
    "chunk": stage_chunk,
    "embed": stage_embed,
}


def status() -> None:
    counts = db.counts()
    print("knowledge base")
    for key in ("portals", "portal_fields", "documents", "documents_published",
                "chunks", "chunks_embedded"):
        print(f"  {key:22} {counts[key]:>6}")
    print("runtime")
    for key in ("users", "sessions", "messages", "leads", "leads_hot",
                "webhooks_pending", "knowledge_gaps_open"):
        print(f"  {key:22} {counts[key]:>6}")

    try:
        print(f"  {'vector store':22} {STORE.reload():>6} live passages")
    except Exception as exc:  # noqa: BLE001
        print(f"  vector store           unavailable: {exc}")

    documents = ingest.listing()
    if documents:
        print("\ndocuments")
        for doc in documents:
            state = "published" if doc["published"] else "draft"
            if doc["retired_at"]:
                state = "retired"
            print(f"  [{state:9}] {doc['portal_key']:18} v{doc['version']} "
                  f"{doc['chunks_embedded']:>4}/{doc['chunks']:<4} passages  "
                  f"{doc['title'][:52]}")

    if not counts["chunks_embedded"]:
        print("\nnot ready. run: python -m kb.build")


def gaps() -> None:
    rows = retrieve.gap_report()
    if not rows:
        print("no unanswered questions logged")
        return
    print(f"{'asked':>6}  {'similarity':>10}  question")
    for row in rows:
        print(f"{row['times_asked']:>6}  {row['avg_similarity'] or 0:>10}  "
              f"{row['question'][:90]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the property knowledge base")
    parser.add_argument("--only", choices=STAGES, action="append",
                        help="run only these stages")
    parser.add_argument("--skip", choices=STAGES, action="append", default=[],
                        help="skip these stages")
    parser.add_argument("--rebuild", action="store_true",
                        help="re-chunk and re-embed from stored document text")
    parser.add_argument("--portal", default=config.DEFAULT_PORTAL)
    parser.add_argument("--status", action="store_true", help="report and exit")
    parser.add_argument("--gaps", action="store_true",
                        help="show unanswered questions and exit")
    args = parser.parse_args()

    db.migrate()

    if args.status:
        status()
        return
    if args.gaps:
        gaps()
        return

    selected = [s for s in (args.only or STAGES) if s not in args.skip]
    print(f"stages: {' -> '.join(selected)}")

    for name in selected:
        print(f"\n[{name}]")
        try:
            result = RUNNERS[name](portal_key=args.portal, rebuild=args.rebuild)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        for key, value in result.items():
            print(f"  {key:24} {value}")

    print()
    status()


if __name__ == "__main__":
    main()
