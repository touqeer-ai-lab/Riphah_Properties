# Riphah AI Property Assistant

A RAG chatbot for **Riphah Properties**. It answers property questions in text or
voice, in English or Urdu, from Riphah-approved content only — and captures
qualified sales leads across the conversation without ever showing a form. Leads
leave over a signed webhook and a versioned REST API, which is what the
[Lead CRM](../Riphah_Lead_CRM/) consumes.

Built against `Riphah_AI_Assistant_Scope_and_Pipeline.pdf` (Draft 1.0). Section
references below (s5, s8, stage 7…) point at that document.

```
      Visitor (text or voice)
              │
              ▼
      Widget  ── shadow DOM, domain-whitelisted portal key
              │
              ▼
      Session ── uuid, survives refresh; consent + UTM recorded once
              │
              ▼
      Message store ──────────► persisted BEFORE processing
              │
              ▼
      Hybrid retrieval  (vectors ⊕ FTS5, fused with RRF)
              │
      ┌───────┴────────────────────────┐
      ▼                                ▼
  Reply generation              Field extraction
  (tool loop, guardrails)       (separate model call, portal schema)
      │                                │
      ▼                                ▼
  Visitor sees answer          Lead record + tier + audit trail
                                       │
                                       ▼
                          Signed webhook · Lead API · alerts
                                       │
                                       ▼
                                  Riphah CRM
```

## What makes this different from the reference build

The screenshot supplied with the brief showed a working transport-booking version
of this idea. Two things about it don't survive contact with a property portal, and
both are fixed here.

**1. Its lead fields were fixed in code.** Here a portal is a row in `portals` and
its capture fields are rows in `portal_fields`. The prompt, the extraction schema,
the scorer and the API all read those rows at request time — so adding
`hostel_required` to an admission portal is one API call, and the assistant starts
asking about it on the next turn. `python -m portals.seed` configures a property
portal *and* an admission portal from the same code to prove the point.

**2. It asked nine questions before showing anything.** On a live property portal
that loses the visitor. This assistant answers first and asks at most one
qualification question per turn, never on the opening reply, and never for
something already given. That is enforced from database state, not from the model's
reading of the transcript — the prompt receives the computed captured set every
turn.

## The thing this build is most careful about

**A figure stated by the assistant can be read by a buyer as a representation of
the company** (s8). So pricing is defended three ways, not one:

1. **Retrieval.** Documents classified `volatile` — prices, availability,
   inventory — are withheld from the model's context entirely while
   `pricing_mode` is `refer`. A passage that isn't in context can't be quoted,
   whatever the prompt says.
2. **A tool that returns the refusal.** `check_price_or_availability` is a tool the
   model must call for any pricing question, and under the default mode its result
   *is* the referral. The constraint arrives as data about the world rather than a
   rule the model has to remember while a visitor pushes for a ballpark.
3. **The prompt.** Explicit, with the "give the commercial reason, never the
   internal one" clause — because "I'm not permitted to" tells the visitor they're
   talking to a rulebook and leaks the configuration.

`eval/run_eval.py` includes the adversarial case where the visitor explicitly asks
for a non-binding estimate, in English and in Roman Urdu. Both must produce no
number.

The other guardrail worth naming: `restricted` documents (internal cost sheets,
legal files, NOC paperwork) are **refused at ingest**, not filtered at query time.
A document filtered at query time still exists in the corpus, one bug away from a
visitor. `content/internal-cost-sheet.md` exists to exercise that path — a build
reports it as `refused` and it is the only file in `content/` with no database row.

## Setup

```bash
cd Riphah_Property_Chatbot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # add OPENAI_API_KEY and a WEBHOOK_SECRET
python -m kb.build            # seed portals, ingest content/, chunk, embed
python doctor.py              # preflight: config, corpus, credentials, delivery
python -m agent.server        # http://127.0.0.1:8100
```

`doctor.py` is worth running first. Most of the failure modes here are silent — an
unset `WEBHOOK_SECRET` doesn't crash anything, it just means no lead ever reaches
the CRM, and you find out a week later when someone asks why the pipeline is empty.

### Knowledge base

```bash
python -m kb.build --status          # what's in there
python -m kb.build --only chunk      # one stage
python -m kb.build --skip embed      # everything except the paid stage
python -m kb.build --rebuild         # re-chunk and re-embed from stored text
python -m kb.build --gaps            # questions the corpus could not answer
```

Stages run `seed → ingest → chunk → embed`. Only `embed` costs money and it is
last, so the ingest and chunker can be iterated on for free.

> ⚠️ **`content/` ships placeholders.** The documents there are illustrative
> structure with invented figures, each saying so in its own body text. Replace them
> with the Riphah-supplied brochures, project descriptions and FAQ sheets before
> launch, and **delete them** — retrieval cannot tell placeholder content from real
> content. `doctor.py` warns while they are present.

### Verify

```bash
python eval/run_eval.py --retrieval   # 14 corpus assertions, free, no model calls
python eval/run_eval.py               # 25 guardrail cases
python eval/run_eval.py --id price-refused-under-pressure
python eval/smoke_http.py             # 52 end-to-end HTTP assertions (server must be up)
```

Run `--retrieval` while iterating on content: if a fact isn't in the corpus, no
amount of prompt work will produce it, so a failure there is always the ingest
pipeline's problem and never the model's.

The guardrail assertions are **regex and field checks, not an LLM judge**. That is
deliberate: the properties being defended — never state a price, never invent a
document requirement, never leak the prompt — are exactly the ones a model grader is
worst at, because the grader shares the generator's blind spots. A regex that says
"no seven-digit number appeared in this reply" cannot be talked round.

## Architecture

```
config.py             paths, models, thresholds, lifecycle timings. No portal specifics.
schema.sql            one schema: portals · knowledge base · identity · leads
doctor.py             preflight check with an action for every failure

core/
  db.py               connections, migration, transactions, audit log
  security.py         PBKDF2, tokens, HMAC signing, PII normalisation
portals/
  registry.py         portal + field-schema CRUD. The architectural hinge (s5)
  seed.py             property and admission portals, and their scoring rules
kb/
  ingest.py           documents → rows; classification enforced; versioned
  chunk.py            heading-aware chunking; template-block collapsing
  embed.py            OpenAI embeddings → float32 BLOBs
  vector_store.py     in-RAM matrix; loads only published, non-retired passages
  retrieve.py         hybrid search (vectors ⊕ FTS5, RRF) + knowledge-gap logging
  build.py            stage orchestrator / CLI
agent/
  prompts.py          THE product: grounding, guardrails, pacing, language, style
  tools.py            3 tools, one schema list → chat + realtime wire formats
  extraction.py       schema-driven capture, basis classification, normalisation
  chat.py             the turn loop: retrieve → answer → extract → score → queue
  conversations.py    sessions, messages, consent, rate limiting, lifecycle
  voice.py            Realtime WebRTC + a whisper/TTS fallback path
  server.py           FastAPI: chat, auth, voice, history, admin
leads/
  store.py            staging, assembly, dedupe, enrichment, payload builder
  scoring.py          configurable tiers + spam detection, with an audit trail
  lifecycle.py        active → idle → inactive sweeper; seals and dispatches
  delivery.py         signed webhooks, durable retry, hot-lead alerts
  api.py              /api/v1 — the CRM contract (s9)
auth/users.py         signup, login, and anonymous-session claiming
frontend/
  index.html          chat UI: auth, history, text + voice
  widget.js           embeddable snippet (shadow DOM + iframe)
eval/                 guardrail cases, runner, HTTP smoke test
```

### Why two model calls per turn

The conversational reply and the structured extraction are **separate calls**
(stage 6). If they were one, a prompt change aimed at making the assistant warmer
could quietly degrade budget capture, and nobody would notice until the CRM filled
with nulls. Two calls means tone changes cannot corrupt data, and the extractor runs
on a cheaper model at temperature 0 while the reply runs warmer.

### Retrieval is deliberately two-sided

`retrieve.search()` fuses dense vectors with FTS5 keyword hits using reciprocal
rank fusion. Vectors handle paraphrase ("somewhere I can set up my practice" →
medical suites) but miss exact tokens, because an embedding of "Block C" sits very
near "Block D". FTS catches the exact tokens but misses paraphrase entirely. RRF
combines the two rankings without needing their score scales to be comparable,
which they are not.

With no `OPENAI_API_KEY`, `search()` logs the miss and returns keyword hits alone —
noticeably worse on paraphrase, fine on names and codes. That path is what runs if
the key ever lapses on a Friday.

### Pre-retrieval, not just a search tool

Passages for the visitor's message are fetched **before** the model runs, every
turn, in addition to the search tool being available. Relying on the tool call alone
left a gap: on a follow-up the model often believed it already knew the answer from
three turns ago, skipped the search, and extended the remembered answer with
plausible neighbouring detail. It once listed "proof of address" among the documents
an overseas buyer needs; the corpus says "proof of remittance channel". Injecting
the passages unconditionally closed that.

### How "never re-ask" actually holds

The lead creation trigger is one contact route plus one qualification field (stage
7). That leaves a gap: a visitor who states a budget on turn 3 and gives a phone
number on turn 6 has three turns where extracted data has no lead to live on.
Discarding it there would break the one promise that matters most — and it did, in
an early version, which is why `session_field_values` exists. Extraction always
stages against the session, and the rows are promoted onto the lead the moment one
is created.

### Question-vs-statement in extraction

A visitor asking "do you have two-bed apartments?" has not chosen an apartment. The
extractor used to record `property_type: apartment` anyway — reading it off the
assistant's own reply — which in a CRM means a consultant calling about the wrong
unit.

Prompting alone did not fix it. Requiring a verbatim quote did not either, and the
reason is worth knowing: the visitor *did* say "two-bed apartments", so a quote
check passes it happily. The distinction is question-versus-requirement, which is
semantic. So the extractor now classifies each field's **basis** —
`stated` / `implied` / `asked_about` / `assistant_said` — and the last two are
dropped in code. Models are reliable at that classification when asked for it
directly, and unreliable at silently declining to fill a field.

A missing annotation is *not* treated as a rejection: it is kept at reduced
confidence and flagged for human confirmation. An earlier version dropped on
silence and threw away a stated budget.

### Session lifecycle and delivery

Sessions go `active → idle (5 min) → inactive (30 min)`, both thresholds in config
because the scope document proposes rather than fixes them. At `inactive` the
session is closed, the transcript is **sealed**, the lead is rescored and the
payload is queued. Sealing matters: once the payload has gone to the CRM, the
transcript that justified its score must not change underneath it.

Delivery is a queue, not fire-and-forget. Every event becomes a row before any
network call, and a sweeper works the backlog with exponential backoff. Verified in
practice during this build: the CRM was down for four delivery attempts, and both
leads arrived intact when it came up.

With no `WEBHOOK_SECRET`, delivery is **disabled** rather than sent unsigned. An
unsigned lead webhook is an unauthenticated write into the CRM.

### Voice

Two paths, because they fail differently.

**Realtime (WebRTC)** — speech to speech, native language switching, ~500 ms turns.
Audio and the data channel run directly browser↔OpenAI, so function calls surface
in the browser, which has no database and must never hold an API key. It posts the
call to `/api/tools/{name}`, this process runs the query, and the browser returns
the result over its data channel. The browser only ever holds a short-lived
credential minted server-side with the instructions and tools already baked in, so
a client cannot weaken the guardrails.

**Whisper + TTS fallback** — three round-trips instead of one, so noticeably
slower, but it works on any browser with a microphone and reuses the text path
exactly, which means the guardrails and lead capture are identical rather than
reimplemented. This is what the shipped UI's microphone button uses.

### Identity: optional accounts

Signing in is optional — requiring an account before a property enquiry would cost
more leads than it captures. What an account buys is continuity across devices.

The mechanism is `auth/users.py:claim_sessions()`. An anonymous visitor is tracked
by a first-party `visitor_id` cookie; on signup or login every session carrying that
id is attached to the account, **including the one they are in the middle of**.
Without that step, signing up mid-conversation would appear to erase the
conversation — which is exactly when a visitor is most likely to sign up, since they
have just been asked for contact details.

## Sign-in gate (`require_auth`)

The property portal is configured to **require sign-in before the chat opens**.
It is a per-portal flag, not a global one, so the admission portal stays open —
`portals.require_auth`, changed with one `registry.upsert` call and no deploy.

Enforced in **two** places, and the server one is the real control:

- `/api/chat`, `/api/voice/*` and the voice-turns endpoint return **401** to an
  unauthenticated caller. `curl` gets the same answer the browser does.
- The UI shows the sign-in card over a blurred, inert chat with no dismiss path —
  no "Later" button, no click-outside, no Esc.

A frontend-only gate is decoration; the chat endpoint is reachable directly.

### The trade-off, so it can be revisited with evidence

A gate raises lead *quality* and lowers lead *volume*.

Every captured lead now arrives with a name, email and phone already verified, and
`contact_source` is always `account`. Against that: visitors who wanted one answer
before committing anything will leave, and the scope document's own s2.1 correction
is precisely about not putting friction in front of the visitor's question.

Both are true. Which matters more is a commercial question, and the flag is the
place to answer it — flip it off, measure capture rate for a fortnight, compare.

```python
from portals import registry
registry.upsert("riphah-property", require_auth=False)   # open it back up
```

## A signed-up visitor becomes a lead — with the consent distinction intact

Signing up gives the company a name, an email and a phone number. So a signed-in
visitor who states requirements becomes a lead **without retyping their contact
details into the chat**.

That was a real gap: before it, someone could sign up with all three, state a
budget, a timeline and a unit type, ask for the floor plans — and produce **no
lead at all**, because the creation trigger only looked at what was *typed in the
conversation*. The business had their details and their requirements, and sales
never heard about them.

Two things the fix is careful about, because "they gave us their number" and "they
agreed to be marketed to" are not the same permission:

**`contact_source`** records where the route came from — `conversation`, `account`,
or `mixed`. A number volunteered mid-enquiry is a genuinely stronger buying signal
than one attached to a login, and the CRM shows which it has rather than treating
them alike.

**`marketing_opt_in`** travels separately from the chat-notice consent. Responding
to an active enquiry needs no marketing consent — the visitor asked for it. Adding
them to a nurture sequence does. The lead payload carries both, and the CRM's lead
drawer says plainly *"no marketing consent — respond to this enquiry only, do not
add to campaigns"* when the flag is false.

Verified across three scenarios:

| Scenario | Lead | `contact_source` | `marketing_opt_in` |
|---|---|---|---|
| signed up, opted in, contact never typed | ✅ hot | `account` | `1` |
| signed up, opted **out**, contact never typed | ✅ hot | `account` | `0` |
| anonymous, typed contact into the chat | ✅ hot | `conversation` | `0` |

A side effect worth knowing: the assistant no longer asks a signed-in visitor for
a phone number it already has, because `captured_for_session()` now includes the
account's details. Asking someone for something they typed into the signup form
two minutes earlier is exactly the "never re-ask" failure the capture design
exists to prevent.

## What crosses to the CRM, and what does not

The boundary is: **the CRM sees what became a lead.** Nothing else leaves this
service. Asserted in `eval/smoke_http.py`, not just intended.

| Stays here | Crosses to the CRM |
|---|---|
| conversations that produced **no lead** — a visitor who browsed and gave no contact details never enters the sales pipeline | the lead record: contact, captured fields, tier, score and its explanation |
| **message text** — the payload carries `message_count`, never content | attribution: landing URL, referrer, UTM, device, coarse region |
| password hashes, auth sessions, API keys | the consent record: whether given, which notice version, when |
| the anonymous `visitor_id` | a `transcript_url` — a pointer, fetched on demand |
| the hashed IP (kept for rate limiting, stripped from every response) | |
| knowledge-gap questions | |

A transcript is **pulled, never pushed**, and only for a session that produced a
lead. That restriction was missing in the first version of
`GET /api/v1/chats/{session_id}`: any `leads:read` key could read any session,
including a visitor who never gave contact details and never consented to sales
follow-up. Session ids are uuid4 and so unguessable, but they are handed to the
browser, kept in localStorage and appear in logs — "hard to guess" is not a
permission model. The endpoint now requires lead linkage, returns an
indistinguishable 404 either way (so a key holder cannot confirm a given visitor
ever had a conversation), and audit-logs the denial.

## The integration contract (s9)

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/leads` | list; filters + cursor pagination |
| `GET /api/v1/leads/{ref}` | one lead, full field set + session history |
| `PATCH /api/v1/leads/{ref}` | CRM updates business status / owner |
| `GET /api/v1/chats/{session_id}` | full transcript, tool turns included |
| `GET /api/v1/portals/{portal}/fields` | current field schema, for auto-mapping |
| `POST /api/v1/portals/{portal}/fields` | add a field with no code release |
| `GET /api/v1/deliveries` | webhook delivery log |
| `lead.created` / `lead.updated` | signed webhooks (HMAC-SHA256) |

Push and pull share **one payload builder** (`store.payload()`), so a CRM tested
against the pull API cannot be surprised by a differently-shaped webhook — a class
of bug that is miserable to diagnose from the receiving end.

API keys are per consumer, hashed at rest, scoped (`leads:read`, `leads:write`,
`portals:write`), and independently revocable. Mint one:

```bash
curl -X POST localhost:8100/api/admin/api-keys \
  -H 'Content-Type: application/json' \
  -d '{"label":"crm","scopes":["leads:read","leads:write","portals:read"]}'
```

That route needs an admin cookie. The first account created is a `visitor`; promote
it in SQLite, then use its session.

## Embedding the widget

```html
<script src="https://assistant.riphahproperties.com/widget.js"
        data-portal="riphah-property" defer></script>
```

Shadow DOM so the portal's CSS and the widget's cannot interfere; an iframe on the
assistant's own origin so cookies stay first-party and the portal's JavaScript
never touches the transcript. The portal can also open it from its own CTA buttons
via `window.RiphahAssistant.open()`.

The portal key is public, so it is validated against the portal's `allowed_domains`
whitelist. That list is **empty in the seed** and empty means localhost-only — a
portal created without a domain list should not be embeddable anywhere on the
internet.

## Current state

| | |
|---|---|
| ✅ 6 documents, 46 passages, all embedded | 1 restricted document correctly refused |
| ✅ **14/14 retrieval assertions** | including the volatile-withholding check |
| ✅ **25/25 guardrail cases** | pricing under pressure, injection, pacing, Urdu |
| ✅ **52/52 HTTP assertions** | auth, session claiming, scopes, sealing, delivery |
| ✅ Lead → CRM verified both ways | signed webhook **and** pull reconciliation |
| ✅ Retry-after-outage verified | 4 failed attempts, then delivered on recovery |
| ⏳ Live WebRTC audio round-trip | needs a human at a browser with a microphone |

Everything up to the WebRTC handshake is exercised; the handshake itself needs a
real microphone. The whisper/TTS fallback path is what the UI uses and is fully
wired.

## Before this goes live

Ordered by how much they matter, not by effort.

1. **Replace `content/`** with Riphah-supplied documents and delete the
   placeholders. Everything else is scaffolding around this.
2. **Confirm the pricing mode** (s8). It is `refer` — the only mode that cannot
   misquote. Changing it is a commercial decision, not a technical one.
3. **Get the qualification tiers approved by sales leadership** (s7). The weights in
   `portals/seed.py` are a proposal, and the budget band is labelled `provisional`
   in every score explanation so nobody mistakes it for a Riphah figure.
4. **Set `allowed_domains`** and a real `ALLOWED_ORIGINS`. Both are open in dev.
5. **Rotate `WEBHOOK_SECRET`** and match it in the CRM.
6. **Approve the consent notice and privacy text** with Riphah legal (stage 2
   dependency).
7. **Decide the hot-lead alert channel.** `delivery.alert_hot_lead()` records the
   alert and returns what it *would* send; email vs WhatsApp vs SMS is an open item
   (s13), and guessing at a provider would be worse than leaving the seam visible.
8. **Mine `knowledge_gaps`.** Every unanswered question is logged in the visitor's
   own words. That table is the content backlog (stage 12).

## Models

| Role | Model | Note |
|---|---|---|
| Reply | `gpt-4.1` | tool calling, temperature 0.3 |
| Extraction | `gpt-4.1-mini` | separate call, temperature 0 |
| Embeddings | `text-embedding-3-large` @ 1536d | dimension-reduced to halve storage |
| Voice | `gpt-realtime` | `semantic_vad`, native language switching |
| Transcribe / speak | `whisper-1` / `gpt-4o-mini-tts` | the fallback voice path |

All overridable in `.env` — model names move faster than this codebase will.
