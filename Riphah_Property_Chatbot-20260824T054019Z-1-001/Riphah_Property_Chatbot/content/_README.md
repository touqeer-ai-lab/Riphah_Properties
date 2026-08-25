# Knowledge base content

Everything in this directory is ingested and **published** by `python -m kb.build`,
on the basis that a file committed here has been reviewed. Admin uploads through
the API take the other path: they land unpublished and stay invisible to the
assistant until someone publishes them.

This file is ignored by the ingester (leading `_`).

## ⚠️ These documents are PLACEHOLDERS

The content here is illustrative structure, not Riphah data. Figures, unit types,
plan names and dates were written to exercise the pipeline and are **not
Riphah-supplied facts**. Every document says so in its own body text, so if one
ever does reach a visitor the disclaimer travels with it.

Before launch, replace all of it with the Riphah-supplied brochures, project
descriptions, payment-plan summaries and FAQ sheets listed as a stage-3
dependency in the scope document. Delete these files at that point — don't leave
them alongside real content, because retrieval cannot tell the difference.

## Front matter

```
---
title: Riphah Medical City — Overview
slug: rmc-overview
project: riphah-medical-city
classification: public
---
```

`slug` identifies a document across versions: re-ingesting the same slug with
changed text creates version 2 and retires version 1.

## Classification (scope document s6.1)

| Value | Effect |
|---|---|
| `public` | quoted freely in answers |
| `reference` | informs an answer; the prompt forbids quoting it in detail |
| `volatile` | withheld from the model entirely unless the portal's `pricing_mode` permits it |
| `restricted` | **refused at ingest.** Never stored, never embedded |

`restricted` is enforced at the ingest boundary rather than at query time. A
document filtered at query time still exists in the corpus, one bug or one prompt
injection away from being quoted; a document refused at ingest was never there.
`internal-cost-sheet.md` in this directory exists to prove that path runs — a
build reports it as `refused`, and it is the only file here that never reaches
the database.
