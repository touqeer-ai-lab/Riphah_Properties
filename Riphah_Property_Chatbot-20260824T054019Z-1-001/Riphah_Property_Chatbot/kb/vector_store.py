"""In-process vector store: the live embedding matrix held in RAM.

Deliberately not a vector database. A few thousand passages × 1536 dims × 4 bytes
is well under 50 MB, and one numpy matmul over that is single-digit milliseconds
— inside the latency budget of a chat widget with room to spare, and with no
extra service to deploy or keep in sync.

`search()` is the only surface callers use. To move to pgvector or Qdrant later,
reimplement that one method.

One thing worth knowing: the store loads **only live passages** — published and
not retired. Retiring a document therefore removes it from retrieval on the next
reload without touching a single embedding, which is what makes the spec's
"superseded documents are retired, removing their passages immediately"
requirement cheap.
"""
from __future__ import annotations

import threading
from typing import Any

import numpy as np

import config
from core import db
from kb import embed

# SQL for the live corpus. The join to kb_documents is the enforcement point for
# publish state, so there is no way to retrieve an unpublished passage.
_LIVE_SQL = """
SELECT c.id, c.heading, c.text, c.portal_key, c.project, c.classification,
       d.id AS document_id, d.title, d.slug, d.version, d.published_at,
       c.embedding
  FROM kb_chunks c
  JOIN kb_documents d ON d.id = c.document_id
 WHERE c.embedding IS NOT NULL
   AND d.published = 1
   AND d.retired_at IS NULL
 ORDER BY c.id
"""


class VectorStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._matrix: np.ndarray | None = None
        self._ids: list[int] = []
        self._meta: dict[int, dict[str, Any]] = {}
        # Parallel arrays for the metadata filters, so masking never touches the
        # per-row dicts inside the hot path.
        self._portals: np.ndarray | None = None
        self._projects: np.ndarray | None = None

    # ------------------------------------------------------------------- loading

    def load(self, *, force: bool = False) -> None:
        with self._lock:
            if self._matrix is not None and not force:
                return
            conn = db.connect()
            try:
                rows = conn.execute(_LIVE_SQL).fetchall()
            finally:
                conn.close()

            if not rows:
                self._matrix = np.zeros((0, config.EMBED_DIMENSIONS), dtype=np.float32)
                self._ids, self._meta = [], {}
                self._portals = np.array([], dtype=object)
                self._projects = np.array([], dtype=object)
                return

            # A dimension mismatch means EMBED_MODEL or EMBED_DIMENSIONS changed
            # without a re-embed. Say so rather than letting numpy raise a shape
            # error four frames deeper.
            widths = {len(row["embedding"]) // 4 for row in rows}
            if len(widths) > 1 or widths.pop() != config.EMBED_DIMENSIONS:
                raise RuntimeError(
                    "Stored embeddings do not match EMBED_DIMENSIONS="
                    f"{config.EMBED_DIMENSIONS}. Run: python -m kb.build --only embed "
                    "--rebuild"
                )

            self._matrix = np.vstack([embed.from_blob(r["embedding"]) for r in rows])
            self._ids = [r["id"] for r in rows]
            self._meta = {
                r["id"]: {
                    "chunk_id": r["id"],
                    "document_id": r["document_id"],
                    "document": r["title"],
                    "slug": r["slug"],
                    "version": r["version"],
                    "heading": r["heading"],
                    "text": r["text"],
                    "portal_key": r["portal_key"],
                    "project": r["project"],
                    "classification": r["classification"],
                    "published_at": r["published_at"],
                }
                for r in rows
            }
            self._portals = np.array([r["portal_key"] for r in rows], dtype=object)
            self._projects = np.array([r["project"] or "" for r in rows], dtype=object)

    def reload(self) -> int:
        """Rebuild the matrix from the database. Called after a publish or retire."""
        self.load(force=True)
        return self.size

    @property
    def size(self) -> int:
        with self._lock:
            return 0 if self._matrix is None else int(self._matrix.shape[0])

    # ------------------------------------------------------------------ querying

    def search(self, query_vector: list[float] | np.ndarray, *,
               portal_key: str | None = None,
               project: str | None = None,
               top_k: int = config.DEFAULT_TOP_K,
               min_similarity: float | None = None) -> list[dict[str, Any]]:
        """Cosine similarity over the live matrix, newest-first on ties.

        Filters are applied by masking scores rather than slicing the matrix, so
        the hot path stays a single contiguous matmul regardless of how many
        portals share the corpus.
        """
        self.load()
        with self._lock:
            if self._matrix is None or self._matrix.shape[0] == 0:
                return []
            matrix, ids, meta = self._matrix, self._ids, self._meta
            portals, projects = self._portals, self._projects

        query = np.asarray(query_vector, dtype=np.float32)
        norm = float(np.linalg.norm(query))
        if norm > 0:
            query = query / norm
        if query.shape[0] != matrix.shape[1]:
            raise ValueError(
                f"query vector has {query.shape[0]} dims, corpus has {matrix.shape[1]}"
            )

        scores = matrix @ query

        if portal_key is not None:
            scores = np.where(portals == portal_key, scores, -1.0)
        if project:
            # Passages with no project tag stay eligible: a general payment-terms
            # document is relevant to a question about a specific project.
            scores = np.where(
                (projects == project) | (projects == ""), scores, -1.0
            )

        floor = config.MIN_SIMILARITY if min_similarity is None else min_similarity
        count = min(max(top_k, 1), len(scores))
        top = np.argpartition(-scores, count - 1)[:count]
        top = top[np.argsort(-scores[top])]

        results = []
        for index in top:
            score = float(scores[index])
            if score < floor:
                continue
            item = dict(meta[ids[index]])
            item["similarity"] = round(score, 4)
            results.append(item)
        return results

    def top_similarity(self, query_vector: list[float] | np.ndarray, **kwargs: Any) -> float:
        """Best score regardless of the threshold — the number logged against a
        knowledge gap, so the content team can tell 'nearly matched' from
        'nothing remotely close'."""
        hits = self.search(query_vector, min_similarity=-1.0, top_k=1, **kwargs)
        return hits[0]["similarity"] if hits else 0.0


STORE = VectorStore()
