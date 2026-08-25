"""Stage 4: hybrid retrieval — dense vectors fused with FTS5 keyword hits.

Why both. Vectors handle paraphrase ("somewhere I can set up my practice" →
medical suites) but miss exact tokens, because a 1536-dim embedding of "Block C"
sits very near "Block D". FTS5 handles the exact tokens (`3-bed`, `Block C`,
`Pharm-D`, a plan name) but misses paraphrase entirely. Reciprocal rank fusion
combines the two rankings without needing their score scales to be comparable,
which they are not.

The degradation path is deliberate: with no OpenAI key, or if embeddings fail,
`search()` logs the miss and returns keyword hits alone. Noticeably worse on
paraphrase, fine on names and codes — and the assistant keeps working, which
matters more than a clean failure when a key lapses on a Friday.
"""
from __future__ import annotations

import re
from typing import Any

import config
from core import db
from kb import embed
from kb.vector_store import STORE

# FTS5 treats these as syntax. A visitor question is not a query language, so
# they are stripped rather than escaped.
_FTS_SYNTAX = re.compile(r'[^\w\s؀-ۿ-]')
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "what", "which", "how", "do", "does",
    "can", "i", "we", "you", "my", "me", "of", "in", "on", "for", "to", "and",
    "or", "it", "at", "be", "have", "has", "there", "any", "about", "tell",
}


def _fts_query(text: str) -> str:
    """Visitor question -> a safe FTS5 MATCH expression.

    OR rather than AND: a six-word question rarely has all six words in the one
    right passage, and AND turns a good near-match into zero results. Prefix
    matching on longer tokens catches plurals and Urdu-transliterated variants.
    """
    cleaned = _FTS_SYNTAX.sub(" ", text.lower())
    tokens = [t for t in cleaned.split() if len(t) > 2 and t not in _STOPWORDS]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"*' if len(t) > 3 else f'"{t}"' for t in tokens[:12])


def keyword_search(query: str, *, portal_key: str | None = None,
                   project: str | None = None,
                   top_k: int = config.DEFAULT_TOP_K) -> list[dict[str, Any]]:
    """FTS5 half. Same shape of result dict as the vector half, so RRF can fuse them."""
    match = _fts_query(query)
    if not match:
        return []

    clauses = ["d.published = 1", "d.retired_at IS NULL"]
    params: list[Any] = [match]
    if portal_key:
        clauses.append("c.portal_key = ?")
        params.append(portal_key)
    if project:
        clauses.append("(c.project = ? OR c.project IS NULL)")
        params.append(project)
    params.append(top_k)

    try:
        rows = db.query(
            f"""
            SELECT c.id AS chunk_id, c.heading, c.text, c.portal_key, c.project,
                   c.classification, d.id AS document_id, d.title AS document,
                   d.slug, d.version, d.published_at,
                   bm25(kb_chunks_fts) AS rank
              FROM kb_chunks_fts
              JOIN kb_chunks c ON c.id = kb_chunks_fts.rowid
              JOIN kb_documents d ON d.id = c.document_id
             WHERE kb_chunks_fts MATCH ?
               AND {' AND '.join(clauses)}
             ORDER BY rank
             LIMIT ?
            """,
            params,
        )
    except Exception as exc:  # noqa: BLE001
        # A malformed MATCH is a bug in _fts_query, not a reason to fail a query.
        print(f"[retrieve] FTS query failed, falling back to vectors only: {exc}")
        return []

    for row in rows:
        # bm25 is negative-better in SQLite. Flipped so callers never have to know.
        row["bm25"] = -float(row.pop("rank") or 0.0)
    return rows


def vector_search(query: str, **kwargs: Any) -> list[dict[str, Any]]:
    return STORE.search(embed.embed_query(query), **kwargs)


def fuse(rankings: list[list[dict[str, Any]]], *, k: int = config.RRF_K,
         weights: list[float] | None = None) -> list[dict[str, Any]]:
    """Reciprocal rank fusion: score = Σ weight / (k + rank).

    Rank-based, so the vector similarities (0–1) and bm25 scores (unbounded) never
    have to be put on a common scale — which is the whole reason to use RRF here
    rather than a weighted sum of normalised scores.
    """
    weights = weights or [1.0] * len(rankings)
    pooled: dict[int, dict[str, Any]] = {}
    fused: dict[int, float] = {}

    for ranking, weight in zip(rankings, weights):
        for rank, item in enumerate(ranking, start=1):
            chunk_id = item["chunk_id"]
            fused[chunk_id] = fused.get(chunk_id, 0.0) + weight / (k + rank)
            # First writer wins on the shared fields; the vector half runs first,
            # so a fused hit keeps its `similarity` when it has one.
            pooled.setdefault(chunk_id, {}).update(
                {kk: vv for kk, vv in item.items() if kk not in pooled.get(chunk_id, {})}
            )

    out = []
    for chunk_id, score in sorted(fused.items(), key=lambda kv: -kv[1]):
        item = pooled[chunk_id]
        item["rrf_score"] = round(score, 6)
        out.append(item)
    return out


def search(query: str, *, portal_key: str | None = None,
           project: str | None = None,
           top_k: int = config.DEFAULT_TOP_K,
           include_volatile: bool = False) -> dict[str, Any]:
    """The retrieval entry point the agent tools call.

    Returns a dict rather than a bare list because callers need three things the
    passages alone don't carry: whether anything cleared the relevance threshold,
    the best similarity (logged against a knowledge gap when it didn't), and which
    retrieval modes actually ran.

    `include_volatile=False` withholds passages classified volatile — prices,
    availability, inventory. They stay out of the model's context entirely under
    the default pricing mode, because a passage present in context is a passage
    that can be quoted, whatever the prompt says.
    """
    modes: list[str] = []
    vector_hits: list[dict[str, Any]] = []
    top_similarity = 0.0
    degraded = False

    if config.has_openai_key():
        try:
            query_vector = embed.embed_query(query)
            vector_hits = STORE.search(query_vector, portal_key=portal_key,
                                       project=project, top_k=top_k * 2)
            top_similarity = STORE.top_similarity(
                query_vector, portal_key=portal_key, project=project
            )
            modes.append("vector")
        except Exception as exc:  # noqa: BLE001
            print(f"[retrieve] embedding unavailable, keyword-only: {exc}")
            degraded = True
    else:
        degraded = True

    keyword_hits = keyword_search(query, portal_key=portal_key, project=project,
                                  top_k=top_k * 2)
    if keyword_hits:
        modes.append("keyword")

    # Vectors are weighted slightly higher because paraphrase is the common case
    # in a chat window; keyword hits are there to rescue the exact-token queries
    # that vectors get wrong.
    fused = fuse([vector_hits, keyword_hits], weights=[1.0, 0.7])

    if not include_volatile:
        fused = [h for h in fused if h.get("classification") != "volatile"]

    passages = fused[:top_k]

    # "Found" means a *vector* hit cleared the floor, or — when vectors are
    # unavailable — that keyword search returned anything at all. A keyword hit
    # on its own is weaker evidence, so it does not suppress the gap log while
    # vectors are working.
    if degraded:
        found = bool(passages)
    else:
        found = any(p.get("similarity", 0.0) >= config.MIN_SIMILARITY for p in passages)

    return {
        "found": found,
        "passages": passages,
        "top_similarity": round(top_similarity, 4),
        "modes": modes,
        "degraded": degraded,
        "query": query,
    }


def format_passages(passages: list[dict[str, Any]], *, max_chars: int = 6000) -> str:
    """Render passages for the model's context, each labelled with its source.

    The label is not decoration: the prompt requires every factual claim to be
    traceable to a passage, and a passage the model cannot name is a passage it
    cannot cite.
    """
    if not passages:
        return "NO_MATCHING_PASSAGES"

    blocks, used = [], 0
    for index, passage in enumerate(passages, start=1):
        source = passage.get("document") or passage.get("slug") or "unknown document"
        heading = passage.get("heading")
        label = f"{source} › {heading}" if heading else source
        note = ""
        if passage.get("classification") == "reference":
            # spec s6.1: informs an answer, must not be quoted in detail.
            note = " [reference only — summarise, do not quote verbatim]"
        block = (f"[passage {index} | {label}"
                 f" | relevance {passage.get('similarity', 'n/a')}]{note}\n"
                 f"{passage['text']}")
        if used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n---\n\n".join(blocks)


def log_gap(question: str, *, portal_key: str, session_id: str | None = None,
            top_similarity: float = 0.0, language: str | None = None) -> None:
    """Record a question the corpus could not answer (spec stage 12).

    This table is the content backlog, in the visitor's own words. Deduplicated
    loosely — the same question from twenty visitors is one gap with a stronger
    case, but recording all twenty is what shows the strength, so they are all
    kept and grouped at read time.
    """
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO knowledge_gaps (portal_key, session_id, question, "
            "top_similarity, language, created_at) VALUES (?,?,?,?,?,?)",
            (portal_key, session_id, question.strip()[:500], top_similarity,
             language, db.now()),
        )


def gap_report(*, portal_key: str | None = None, limit: int = 50,
               include_resolved: bool = False) -> list[dict[str, Any]]:
    """Grouped knowledge gaps, most-asked first — the review agenda for spec s12."""
    clauses, params = [], []
    if portal_key:
        clauses.append("portal_key = ?")
        params.append(portal_key)
    if not include_resolved:
        clauses.append("resolved_at IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    return db.query(
        f"""
        SELECT LOWER(TRIM(question)) AS question, COUNT(*) AS times_asked,
               ROUND(AVG(top_similarity), 3) AS avg_similarity,
               MAX(created_at) AS last_asked
          FROM knowledge_gaps {where}
         GROUP BY LOWER(TRIM(question))
         ORDER BY times_asked DESC, last_asked DESC
         LIMIT ?
        """,
        params,
    )
