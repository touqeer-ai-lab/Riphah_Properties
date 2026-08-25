"""Stage 1: turn Riphah-supplied documents into `kb_documents` rows.

Accepts markdown, plain text, and PDF. Each document carries a classification
(spec s6.1) that governs what the assistant may do with it:

  public     — quoted freely
  reference  — informs an answer, not quoted in detail
  volatile   — never stated as fact (prices, availability, inventory)
  restricted — refused at this boundary and never stored

`restricted` is enforced here rather than at query time on purpose. Filtering at
retrieval means the text still lives in the corpus, one prompt-injection or one
buggy filter away from being quoted. Refusing at ingest means it was never there.

Documents are versioned. Re-ingesting changed content supersedes the previous
version rather than overwriting it, so "what had the assistant been told on 3
August" stays answerable (spec s6 step 12).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import config
from core import db

# Front matter at the top of a content file:
#   ---
#   title: Riphah Medical City — Overview
#   project: riphah-medical-city
#   classification: public
#   ---
_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

CLASSIFICATIONS = ("public", "reference", "volatile", "restricted")

TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | PDF_SUFFIXES


class RestrictedDocument(ValueError):
    """Raised when a document is classified `restricted`. Not an error condition —
    the caller should report it as a deliberate refusal."""


def parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    match = _FRONT_MATTER.match(raw)
    if not match:
        return {}, raw
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    return meta, raw[match.end():]


def extract_pdf_text(data: bytes) -> str:
    """PDF text via PyMuPDF, with OCR left as an explicit gap.

    Riphah's brochures are likely to include scanned pages. Rather than silently
    producing an empty document for those, this returns what the text layer has
    and the caller reports a low character count — which is the signal that a
    document needs OCR before it is useful.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "PDF ingest needs PyMuPDF: pip install pymupdf"
        ) from exc

    pages = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            pages.append(page.get_text("text"))
    return "\n\n".join(pages)


def normalise_text(text: str) -> str:
    """Collapse the whitespace damage that PDF and Word exports leave behind.

    Blank-line structure is preserved because the chunker uses it to find
    heading boundaries; runs of three or more collapse to two.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Soft hyphens and non-breaking spaces from PDF exports break FTS tokens.
    text = text.replace("­", "").replace(" ", " ")
    return text.strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def store_document(*, portal_key: str, slug: str, title: str, text: str,
                   project: str | None = None, source: str | None = None,
                   classification: str = "public",
                   publish: bool = False,
                   actor: str = "system") -> dict[str, Any]:
    """Insert or supersede one document. Returns a summary dict.

    Behaviour by case:
      * content unchanged  -> no-op, `status='unchanged'` (so nothing re-embeds)
      * content changed    -> new version row; the previous version is retired
      * restricted         -> RestrictedDocument, nothing written
    """
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"classification must be one of {CLASSIFICATIONS}")
    if classification == "restricted":
        raise RestrictedDocument(
            f"'{title}' is classified restricted and will not be added to the "
            f"knowledge base."
        )

    text = normalise_text(text)
    if not text:
        raise ValueError(f"'{title}' has no extractable text (scanned PDF? needs OCR)")

    digest = content_hash(text)
    stamp = db.now()

    with db.tx() as conn:
        current = conn.execute(
            "SELECT id, version, content_hash, published FROM kb_documents "
            " WHERE portal_key = ? AND slug = ? AND retired_at IS NULL "
            " ORDER BY version DESC LIMIT 1",
            (portal_key, slug),
        ).fetchone()

        if current and current["content_hash"] == digest:
            # Nothing changed. Still honour a publish request on the existing row,
            # because "publish this" and "re-upload this" are different intents.
            if publish and not current["published"]:
                conn.execute(
                    "UPDATE kb_documents SET published = 1, published_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (stamp, stamp, current["id"]),
                )
                status = "published"
            else:
                status = "unchanged"
            return {"id": current["id"], "version": current["version"],
                    "status": status, "slug": slug, "chars": len(text)}

        version = (current["version"] + 1) if current else 1
        if current:
            # Retiring the old version drops its passages from retrieval the
            # moment the new one publishes (spec s6 step 13).
            conn.execute(
                "UPDATE kb_documents SET retired_at = ?, updated_at = ? WHERE id = ?",
                (stamp, stamp, current["id"]),
            )

        cur = conn.execute(
            """
            INSERT INTO kb_documents (portal_key, slug, title, project, source,
                                      classification, text, char_count,
                                      content_hash, version, published,
                                      published_at, created_at, updated_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (portal_key, slug, title, project, source, classification, text,
             len(text), digest, version, int(publish),
             stamp if publish else None, stamp, stamp),
        )
        document_id = cur.lastrowid

    db.audit(actor, "kb.document.store", entity="kb_document", entity_id=document_id,
             detail={"slug": slug, "version": version, "classification": classification,
                     "published": publish, "chars": len(text)})
    return {"id": document_id, "version": version,
            "status": "created" if version == 1 else "superseded",
            "slug": slug, "chars": len(text)}


def ingest_file(path: Path, *, portal_key: str, publish: bool = False,
                actor: str = "system") -> dict[str, Any]:
    """Ingest one file, taking metadata from front matter when present."""
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"unsupported file type '{suffix}' ({path.name})")

    if suffix in PDF_SUFFIXES:
        meta, body = {}, extract_pdf_text(path.read_bytes())
    else:
        meta, body = parse_front_matter(path.read_text(encoding="utf-8", errors="replace"))

    # A markdown H1 is a better title than a filename when front matter is absent.
    heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    title = meta.get("title") or (heading.group(1).strip() if heading else path.stem)

    return store_document(
        portal_key=meta.get("portal") or portal_key,
        slug=meta.get("slug") or path.stem,
        title=title,
        text=body,
        project=meta.get("project"),
        source=path.name,
        classification=meta.get("classification", "public"),
        publish=publish,
        actor=actor,
    )


def ingest_directory(directory: Path | None = None, *,
                     portal_key: str | None = None,
                     publish: bool = True,
                     actor: str = "system") -> list[dict[str, Any]]:
    """Ingest every supported file under `directory` (default: content/).

    `publish=True` here because files committed to `content/` are Riphah-approved
    by definition — they went through review to get into the repo. Admin uploads
    take the other path and stay unpublished until someone clicks publish.
    """
    directory = directory or config.CONTENT_DIR
    portal_key = portal_key or config.DEFAULT_PORTAL
    results: list[dict[str, Any]] = []

    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        if path.name.startswith((".", "_")):
            continue
        try:
            results.append(ingest_file(path, portal_key=portal_key,
                                       publish=publish, actor=actor))
        except RestrictedDocument as exc:
            results.append({"slug": path.stem, "status": "refused", "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001
            # One malformed brochure must not abort a 200-document ingest.
            results.append({"slug": path.stem, "status": "error",
                            "reason": f"{type(exc).__name__}: {exc}"})
    return results


def publish(document_id: int, *, actor: str = "admin") -> bool:
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE kb_documents SET published = 1, published_at = ?, updated_at = ? "
            " WHERE id = ? AND retired_at IS NULL",
            (db.now(), db.now(), document_id),
        )
    if cur.rowcount:
        db.audit(actor, "kb.document.publish", entity="kb_document", entity_id=document_id)
    return cur.rowcount > 0


def retire(document_id: int, *, actor: str = "admin") -> bool:
    """Retire a document. Its passages leave retrieval immediately — the vector
    store filters on the live-document join, so no re-embed is needed."""
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE kb_documents SET retired_at = ?, updated_at = ? WHERE id = ?",
            (db.now(), db.now(), document_id),
        )
    if cur.rowcount:
        db.audit(actor, "kb.document.retire", entity="kb_document", entity_id=document_id)
    return cur.rowcount > 0


def listing(portal_key: str | None = None, *,
            include_retired: bool = False) -> list[dict[str, Any]]:
    clauses, params = [], []
    if portal_key:
        clauses.append("d.portal_key = ?")
        params.append(portal_key)
    if not include_retired:
        clauses.append("d.retired_at IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return db.query(
        f"""
        SELECT d.id, d.portal_key, d.slug, d.title, d.project, d.classification,
               d.char_count, d.version, d.published, d.published_at, d.retired_at,
               d.updated_at,
               (SELECT COUNT(*) FROM kb_chunks c WHERE c.document_id = d.id) AS chunks,
               (SELECT COUNT(*) FROM kb_chunks c WHERE c.document_id = d.id
                  AND c.embedding IS NOT NULL) AS chunks_embedded
          FROM kb_documents d {where}
         ORDER BY d.portal_key, d.slug, d.version DESC
        """,
        params,
    )
