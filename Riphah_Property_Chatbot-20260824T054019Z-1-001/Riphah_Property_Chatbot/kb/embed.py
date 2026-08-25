"""Stage 3: embed passages with OpenAI and store the vectors in SQLite.

Vectors are float32 BLOBs on the chunk row, normalised at write time so
retrieval is a dot product rather than a cosine calculation. `dimensions=1536`
uses the model's native Matryoshka truncation, which halves storage against the
3072 default with no measurable retrieval loss at this corpus size.
"""
from __future__ import annotations

from typing import Any

import numpy as np

import config
from core import db

BATCH_SIZE = 96
MAX_CHARS = 8000        # ~2k tokens; over-long passages are truncated, not rejected


def client():
    from openai import OpenAI

    return OpenAI(api_key=config.openai_key())


def embed_texts(texts: list[str]) -> list[list[float]]:
    payload = [(t[:MAX_CHARS] if t.strip() else " ") for t in texts]
    response = client().embeddings.create(
        model=config.EMBED_MODEL,
        input=payload,
        dimensions=config.EMBED_DIMENSIONS,
    )
    return [item.embedding for item in response.data]


def embed_query(text: str) -> list[float]:
    """One query vector. Separate from `embed_texts` only for call-site clarity."""
    return embed_texts([text])[0]


def to_blob(vector: list[float] | np.ndarray) -> bytes:
    array = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(array))
    if norm > 0:
        array = array / norm
    return array.tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def run(*, rebuild: bool = False, portal_key: str | None = None,
        batch_size: int = BATCH_SIZE, verbose: bool = True) -> dict[str, Any]:
    """Embed everything missing a vector, or embedded under a different model.

    The model check matters: switching EMBED_MODEL without re-embedding leaves a
    corpus of mixed vector spaces, where similarity scores are meaningless and
    retrieval silently degrades instead of failing.
    """
    conn = db.connect()
    embedded = 0
    try:
        if rebuild:
            conn.execute("UPDATE kb_chunks SET embedding = NULL, embed_model = NULL")
            conn.commit()

        clauses = ["(embedding IS NULL OR embed_model IS NOT ?)"]
        params: list[Any] = [config.EMBED_MODEL]
        if portal_key:
            clauses.append("portal_key = ?")
            params.append(portal_key)

        pending = conn.execute(
            f"SELECT id, text FROM kb_chunks WHERE {' AND '.join(clauses)} ORDER BY id",
            params,
        ).fetchall()

        if not pending:
            return {"pending": 0, "embedded": 0, "model": config.EMBED_MODEL}

        if verbose:
            print(f"  embed: {len(pending)} passages pending", flush=True)

        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            vectors = embed_texts([row["text"] for row in batch])
            for row, vector in zip(batch, vectors):
                conn.execute(
                    "UPDATE kb_chunks SET embedding = ?, embed_model = ? WHERE id = ?",
                    (to_blob(vector), config.EMBED_MODEL, row["id"]),
                )
            embedded += len(batch)
            # Commit per batch: a rate limit halfway through a large ingest should
            # cost the current batch, not the whole run.
            conn.commit()
            if verbose:
                print(f"  embed: {embedded}/{len(pending)}", flush=True)
    finally:
        conn.close()

    return {"pending": len(pending), "embedded": embedded, "model": config.EMBED_MODEL}


if __name__ == "__main__":
    db.migrate()
    print(run())
