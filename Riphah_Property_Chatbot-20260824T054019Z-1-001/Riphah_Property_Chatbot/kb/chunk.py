"""Stage 2: split documents into retrievable passages.

Two things this does beyond naive splitting, both of which showed up as real
retrieval failures in the sibling project:

**Headings ride along.** A passage that reads "Payments are due quarterly from
possession" is useless without knowing it came from *DHA Business District →
Payment Plans*. The heading path is prepended to the embedded text, so the
retrieved passage carries its own context and the model can cite it.

**Template blocks are collapsed.** Brochure boilerplate — the same "all prices
subject to confirmation" footer on 30 documents — produces 30 near-identical
passages that outrank the one genuinely relevant page by sheer duplicate mass.
Body text repeated verbatim across `TEMPLATE_BLOCK_THRESHOLD` documents is kept
once and dropped elsewhere.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

import config
from core import db

# Markdown ATX headings, plus the ALL-CAPS lines that PDF exports produce where
# a heading used to be.
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_CAPS_HEADING = re.compile(r"^([A-Z][A-Z0-9 &/,'’\-()]{6,70})$")


def _heading_level(line: str) -> tuple[int, str] | None:
    match = _MD_HEADING.match(line)
    if match:
        return len(match.group(1)), match.group(2).strip()
    if _CAPS_HEADING.match(line.strip()) and len(line.split()) <= 10:
        # Treat an isolated caps line as an H2 — enough to segment, not enough to
        # outrank real markdown structure.
        return 2, line.strip().title()
    return None


def split_sections(text: str) -> list[tuple[str, str]]:
    """Document text -> [(heading_path, body)], in document order."""
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            path = " › ".join(name for _, name in stack)
            sections.append((path, body))
        buffer.clear()

    for line in text.splitlines():
        found = _heading_level(line)
        if found:
            flush()
            level, name = found
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, name))
        else:
            buffer.append(line)
    flush()
    return sections


def _split_paragraphs(body: str) -> list[str]:
    """Paragraphs, with a table kept whole.

    A markdown table split down the middle loses its header row, which turns a
    payment schedule into a column of unlabelled numbers.
    """
    blocks: list[str] = []
    table: list[str] = []
    for block in body.split("\n\n"):
        lines = block.strip().splitlines()
        is_table = bool(lines) and sum(1 for ln in lines if "|" in ln) >= max(1, len(lines) - 1)
        if is_table:
            table.append(block.strip())
            continue
        if table:
            blocks.append("\n\n".join(table))
            table = []
        if block.strip():
            blocks.append(block.strip())
    if table:
        blocks.append("\n\n".join(table))
    return blocks


def _pack(paragraphs: Iterable[str], *, target: int, overlap: int) -> list[str]:
    """Greedily fill passages to `target` chars, overlapping by `overlap`."""
    passages: list[str] = []
    current = ""

    for para in paragraphs:
        if not current:
            current = para
        elif len(current) + len(para) + 2 <= target:
            current = f"{current}\n\n{para}"
        else:
            passages.append(current)
            # Carry the tail of the previous passage so a sentence spanning the
            # boundary is still retrievable from both sides.
            tail = current[-overlap:] if overlap and len(current) > overlap else ""
            if tail:
                tail = tail.split("\n\n")[-1]
            current = f"{tail}\n\n{para}".strip() if tail else para

    if current:
        passages.append(current)

    # A single paragraph longer than the target (a long table, a wall of legal
    # text) still has to be broken, or it will exceed the embedding limit.
    out: list[str] = []
    hard_cap = target * 2
    for passage in passages:
        if len(passage) <= hard_cap:
            out.append(passage)
            continue
        for start in range(0, len(passage), target):
            piece = passage[start:start + target].strip()
            if piece:
                out.append(piece)
    return out


def _norm_for_dedupe(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()[:24]


def chunk_document(*, title: str, text: str,
                   target: int = config.CHUNK_TARGET_CHARS,
                   overlap: int = config.CHUNK_OVERLAP_CHARS) -> list[dict[str, Any]]:
    """One document -> passages with heading context baked into the embed text."""
    out: list[dict[str, Any]] = []
    for heading_path, body in split_sections(text):
        for passage in _pack(_split_paragraphs(body), target=target, overlap=overlap):
            context = " › ".join(p for p in (title, heading_path) if p)
            out.append({
                "heading": heading_path or None,
                # What gets embedded and what the model sees. The prefix is part
                # of the text, not metadata, so similarity search benefits from it.
                "text": f"[{context}]\n{passage}" if context else passage,
                "body": passage,
            })
    return out


def dedupe_across_documents(
    chunks: list[dict[str, Any]], *,
    threshold: int = config.TEMPLATE_BLOCK_THRESHOLD,
) -> tuple[list[dict[str, Any]], int]:
    """Drop template blocks: identical body text appearing in `threshold`+ documents.

    Counted by distinct document, not by occurrence — a phrase repeated three
    times inside one brochure is emphasis, while the same phrase on eight
    different brochures is a footer.
    """
    seen_docs: dict[str, set[Any]] = {}
    for chunk in chunks:
        seen_docs.setdefault(_norm_for_dedupe(chunk["body"]), set()).add(
            chunk.get("document_id")
        )

    kept: list[dict[str, Any]] = []
    emitted: set[str] = set()
    dropped = 0
    for chunk in chunks:
        digest = _norm_for_dedupe(chunk["body"])
        if len(seen_docs[digest]) >= threshold:
            if digest in emitted:
                dropped += 1
                continue
            emitted.add(digest)
        kept.append(chunk)
    return kept, dropped


def run(*, portal_key: str | None = None, rebuild: bool = False) -> dict[str, int]:
    """Chunk every published, non-retired document that has no chunks yet.

    Chunking is idempotent per document: a document either has its passages or it
    doesn't. `rebuild=True` clears and redoes them, which is what you want after
    changing the chunker itself.
    """
    clauses = ["retired_at IS NULL", "published = 1"]
    params: list[Any] = []
    if portal_key:
        clauses.append("portal_key = ?")
        params.append(portal_key)

    documents = db.query(
        f"SELECT id, portal_key, title, project, classification, text "
        f"  FROM kb_documents WHERE {' AND '.join(clauses)} ORDER BY id",
        params,
    )

    if rebuild:
        ids = [d["id"] for d in documents]
        if ids:
            with db.tx() as conn:
                conn.execute(
                    f"DELETE FROM kb_chunks WHERE document_id IN "
                    f"({','.join('?' * len(ids))})",
                    ids,
                )

    staged: list[dict[str, Any]] = []
    skipped = 0
    for doc in documents:
        existing = db.scalar(
            "SELECT COUNT(*) FROM kb_chunks WHERE document_id = ?", (doc["id"],)
        )
        if existing:
            skipped += 1
            continue
        for index, piece in enumerate(chunk_document(title=doc["title"], text=doc["text"])):
            staged.append({
                **piece,
                "document_id": doc["id"],
                "portal_key": doc["portal_key"],
                "project": doc["project"],
                "classification": doc["classification"],
                "ordinal": index,
            })

    kept, dropped = dedupe_across_documents(staged)

    # Ordinals are re-numbered per document after dedupe, because the UNIQUE
    # (document_id, ordinal) constraint would otherwise reject the gaps.
    counters: dict[int, int] = {}
    stamp = db.now()
    with db.tx() as conn:
        for chunk in kept:
            doc_id = chunk["document_id"]
            ordinal = counters.get(doc_id, 0)
            counters[doc_id] = ordinal + 1
            conn.execute(
                """
                INSERT OR REPLACE INTO kb_chunks
                    (document_id, ordinal, heading, text, portal_key, project,
                     classification, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (doc_id, ordinal, chunk["heading"], chunk["text"], chunk["portal_key"],
                 chunk["project"], chunk["classification"], stamp),
            )

    return {
        "documents": len(documents),
        "documents_skipped": skipped,
        "chunks": len(kept),
        "template_blocks_dropped": dropped,
    }


if __name__ == "__main__":
    db.migrate()
    print(run())
