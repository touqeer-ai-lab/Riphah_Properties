---
title: Internal Cost Sheet and Margin Analysis
slug: internal-cost-sheet
classification: restricted
---

# Internal Cost Sheet and Margin Analysis

> **This file exists to be refused.**
>
> It is classified `restricted`, so `kb.ingest` raises `RestrictedDocument` and
> nothing below this line is ever written to the database, chunked, or embedded.
> A build reports it as `refused` and continues.
>
> It is the fixture for the s6.1 test: the scope document lists internal cost
> sheets, legal files and NOC documentation as content that must be excluded from
> the knowledge base *entirely* — not filtered at query time, where the text still
> exists and one bug stands between it and a visitor.
>
> `eval/run_eval.py --pipeline` asserts this document has no row in
> `kb_documents`. If someone changes the ingest path so that restricted content
> gets stored-but-hidden instead of rejected, that check fails.

## Cost basis per unit type

Land cost apportionment, construction cost per square foot, financing cost,
marketing allocation and target margin by unit type would go here in a real
internal document.

## Discount authority

Discount ceilings by role — what a consultant may offer without approval, what
needs a manager, what needs the director — would go here.

## Competitor margin comparison

Commercially sensitive comparison against neighbouring developments would go here.
