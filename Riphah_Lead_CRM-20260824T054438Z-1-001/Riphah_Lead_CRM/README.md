# Riphah Lead CRM

The sales-side half of the pair. It ingests leads from the
[AI Property Assistant](../Riphah_Property_Chatbot/) — and, once credentials
arrive, from Meta lead ads — deduplicates them, tracks the sales process, and
reports the analytics a property sales manager actually acts on.

```
   AI Property Assistant                    Meta lead ads
   (live)                                   (PENDING — credentials)
      │                                            │
      ├── signed webhook  ──┐              ┌── X-Hub-Signature-256
      └── pull reconcile ───┤              │
                            ▼              ▼
                      source adapters  (normalise → NormalisedLead)
                                  │
                                  ▼
                      ingest.upsert()  ── idempotent on (source, external_id)
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
              leads + fields   activity     person_key
              (source-owned)   (CRM-owned)  (cross-source identity)
                                  │
                                  ▼
                      analytics · dashboard · CSV export
```

## Why this is a separate service

It could have read the assistant's SQLite file directly and saved most of this
code. But then the integration contract in the scope document (s9) would never
actually be exercised, and the first real CRM Riphah picks — the strategy report
lists five candidates — would be integrating against an API nobody had tested from
the outside.

So this consumes the assistant exactly the way Salesforce or Zoho would: signed
webhooks for push, an API key against `/api/v1/leads` for pull. If this CRM works,
a commercial one will too. It also means the assistant has no idea this exists,
which is the property that lets Riphah replace this dashboard later without
touching the chatbot.

## Setup

Start the assistant first — this service pulls from it.

```bash
cd Riphah_Lead_CRM
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Three things in `.env` matter:

```bash
# Must be byte-identical to the chatbot's, or every webhook is rejected
WEBHOOK_SECRET=<same value as the chatbot>

# Mint on the chatbot: POST /api/admin/api-keys (needs an admin cookie there)
CHATBOT_API_KEY=rip_...

# First run only — creates an admin when the staff table is empty
BOOTSTRAP_ADMIN_PASSWORD=<something you'll remember>
```

```bash
python -m crm.server                 # http://127.0.0.1:8200
python eval/seed_demo.py             # ~70 demo leads, so the dashboard has data
```

The startup log states each source's status plainly, including *why* a source is
pending.

## Accounts — there is no signup form

Deliberately. This CRM reads leads with phone numbers in them, so a public signup
would hand the sales pipeline to anyone who found the URL. Accounts are created
**for** staff, three ways:

**1. The first admin** is created on first boot from `BOOTSTRAP_ADMIN_PASSWORD` in
`.env`, when the `staff` table is empty. Change that password after the first login
and clear the variable. Shipping a default password would be worse; requiring a
manual SQL insert before anyone can log in would be needlessly annoying.

**2. The Team tab** (manager and above can see it, admin can change it). Add a
consultant, pick a role, generate a password with 🎲. The password is shown **once**
— nothing stores the plaintext.

**3. The CLI**, for scripted setup or when nobody can get in:

```bash
python -m crm.manage list
python -m crm.manage create-staff sales1@riphah.local --name "Ayesha" --role agent --generate
python -m crm.manage passwd sales1@riphah.local          # prompts; revokes their sessions
python -m crm.manage role sales1@riphah.local manager
python -m crm.manage disable sales1@riphah.local         # and signs them out immediately
python -m crm.manage enable sales1@riphah.local
```

Passwords are prompted for rather than passed as arguments, because an argument
lands in shell history and in the process list. `--password` exists for scripting
and warns when used.

Two self-protections: an admin cannot demote or disable **themselves**, through the
UI or the API. Either one, done by the only admin, locks the whole team out with no
way back except editing SQLite by hand. Disabling an account also revokes its live
sessions — otherwise it keeps working until the cookie expires, which defeats the
point.

### Verify

```bash
python eval/test_meta_fixture.py     # 35 assertions — the Meta adapter, no credentials needed
python eval/test_parity.py           # normalisation + HMAC parity with the chatbot
python eval/smoke_crm.py             # 64 end-to-end HTTP assertions (server must be up)
python eval/seed_demo.py --clear     # remove demo leads only
```

## Meta lead ads: what "pending" means here

The brief listed two lead sources and said Meta is pending. Rather than leave a
hole, the adapter is **written and tested against a captured payload fixture**, and
wired into ingest, analytics and the dashboard. What is missing is credentials.

**Done and verified** (`eval/test_meta_fixture.py`, 35/35):

- `normalise()` maps Meta's `field_data` array onto the same `NormalisedLead` the
  chatbot produces, so a Meta lead flows through the identical ingest, dedupe,
  analytics and UI path.
- `FIELD_MAP` translates Meta's form field names — including the question-text keys
  its form builder generates — to the property portal's field keys.
- `VALUE_MAP` translates free-text answers ("1-3 months") to the portal's enums
  (`within_3_months`).
- Unmapped questions are **preserved**, not dropped, and flagged for confirmation —
  so a wrong mapping can be corrected retroactively instead of the data being
  re-collected.
- `/api/webhooks/meta` verifies `X-Hub-Signature-256` and handles the `GET`
  subscription handshake.
- Scoring, deliberately more conservative than the chatbot's (see below).

**Blocked, and on whom:**

| Needed | From | Why |
|---|---|---|
| `META_APP_SECRET` | Riphah marketing / agency | verify webhook signatures |
| `META_PAGE_ACCESS_TOKEN` | same | read `/{leadgen_id}` for the field data |
| `META_VERIFY_TOKEN` | our choice, theirs to enter | subscription handshake |
| `META_PAGE_ID` | same | scope which page's leads we accept |
| `FIELD_MAP` sign-off | Riphah sales | the map is our best guess at their live forms |

Meta's webhook carries only a `leadgen_id`; the answers must then be fetched from
the Graph API with the page token. So the adapter can normalise a payload but cannot
obtain one — which is why this is credential-blocked rather than half-built. Set the
four variables and `status()` flips to live with no code change.

While pending, `POST /api/webhooks/meta` returns **503 naming the missing
variables** rather than a generic error, and the dashboard shows the source as
`pending` with what it is waiting on. A pipeline gap is information, not something
to hide.

### Why Meta leads are scored more conservatively

The chatbot's scorer leans on signals a form submission does not have: engagement
depth, evidence the visitor read anything, and a conversation in which a stated
budget can be judged. Paid-social form-fill rates are also high relative to intent.

So a Meta lead's ceiling is Warm unless it has **both** a phone number and a
near-term timeline. Marking form fills Hot on arrival would fill the priority queue
and train the sales team to ignore the tier — at which point the tier is worth
nothing on either source.

## Analytics

The brief asked for active-versus-inactive users and the different lead types
arriving from the chatbot API. Both are here, plus the two figures a property sales
manager acts on: how fast hot leads get called, and where good leads come from.

"Active user" is ambiguous, so the dashboard reports **two axes** rather than
picking one:

- **Visitor engagement** — did they hold a real conversation (4+ messages), a brief
  look, or none at all; and did they come back. A property of the visitor.
- **Sales pipeline** — is the lead in play, dormant (no activity for 14 days), or
  closed. A property of the process.

Conflating them is how you end up with an "active users" chart nobody can act on.

Everything else: tier mix, funnel (captured → contactable → contacted → qualified →
converted), daily volume by tier, budget bands in crore/lakh, project and timeline
and purpose distributions, channel mix (text vs voice vs form), campaign attribution
with hot-rate per campaign, consultant load and median response time, and an
SLA-breach list.

Two deliberate choices:

**Median, not mean, response time.** One lead that sat over a holiday weekend drags
a mean into meaninglessness.

**Source rows carry quality, not just volume.** A channel producing four times the
leads at a third of the hot rate is not four times as valuable, and paid social is
exactly where that happens.

The SLA targets (24h hot, 72h warm) are **placeholders** — hot-lead response time
and accountability are open items in the scope document (s13) — and every response
that uses them says so, so nobody mistakes a placeholder for an agreed SLA.

## Architecture

```
config.py             integration endpoints, SLA placeholders, thresholds
schema.sql            source-agnostic core + per-source fields as rows
core/
  db.py               connections, activity log, duration helpers
  security.py         signature verification, staff passwords, PII normalisation
sources/
  base.py             the adapter contract — NormalisedLead
  chatbot.py          live: normalise, pull, transcript fetch, status write-back
  meta.py             PENDING: adapter + field map + scoring, fixture-tested
crm/
  ingest.py           idempotent upsert; CRM-owned vs source-owned columns
  analytics.py        overview, engagement, funnel, attribution, SLA, CSV
  auth.py             staff accounts, three roles, lead scoping
  server.py           FastAPI: dashboard, leads, webhooks, pull reconciler
frontend/index.html   the dashboard
eval/                 Meta fixture, parity test, HTTP smoke test, demo seeder
```

### Push and pull, not push alone

A webhook can be lost while this service is restarting or mid-deploy, and the CRM
has **no way to know about a delivery it never received**. So a background
reconciler polls `/api/v1/leads?since=`.

The overlap matters: it asks for leads since the last sync *minus fifteen minutes*,
not since the last sync. Two services with slightly different clocks and a lead
created during the handover would otherwise fall between two windows and never
arrive. Re-fetching a few already-seen leads costs nothing because upsert is
idempotent on `(source_key, external_id)`.

Both paths are verified in `eval/smoke_crm.py`: a delivery sent three times produces
one lead.

### The CRM keeps its own columns

This is the classic two-way-sync bug, and it is worth being explicit about. An
inbound `lead.updated` refreshes the *captured record* — name, phone, budget, tier —
because the source owns that. It does **not** touch `status`, `assigned_owner`, or
`first_response_at`, because the CRM owns those.

Get it backwards and a consultant marks a lead 'contacted', the visitor sends one
more message, and the lead reappears in the new queue. There is a test for exactly
this.

Status changes are mirrored *back* to the assistant on a best-effort basis, so both
dashboards agree — but the CRM is the system of record, so a failed write-back is
logged rather than fatal.

### Two consent flags, never conflated

A lead carries **two** separate permissions, and the drawer shows both because a
consultant needs to know which they have:

- **`consent_given`** — the visitor accepted the chat notice, with the notice
  version and timestamp. Covers capturing and passing on the enquiry.
- **`marketing_opt_in`** — copied from their account if they have one. Covers
  adding them to nurture sequences and campaigns.

Responding to an active enquiry needs only the first: the person asked to be
contacted about a specific unit. Marketing to them needs the second. When
`marketing_opt_in` is false, the drawer says so in plain words — *"no marketing
consent — respond to this enquiry only, do not add to campaigns"* — rather than
leaving a consultant to infer it from a blank field.

`contact_source` sits alongside them: `conversation` if the visitor typed their
number into the chat, `account` if it came from their login, `mixed` if both. A
number volunteered mid-enquiry is a stronger buying signal than one attached to a
login, so the two are shown differently rather than treated as the same thing.

### Cross-source identity without merging

`person_key` is derived from the normalised phone (preferred) or email, so the same
human arriving from the chatbot and from a Meta form is visibly one person. The rows
are **not merged**: each remains the record of what that source captured, and the
lead detail view links between them. Collapsing them would lose which said what.

Phone is preferred over email because it is the field a property sales team works
from, and because one person routinely has several email addresses but rarely
several mobile numbers.

### ⚠️ Normalisation parity

`core/security.py` duplicates `normalise_email` and `normalise_phone` from the
chatbot. This is the one place where duplication between the two services is a real
hazard: deduplication depends on both sides agreeing that `0300 1234567` and
`+92 300 1234567` are the same person. If they disagree, every lead arriving by both
webhook and pull becomes two rows and the sales team calls the same buyer twice.

`eval/test_parity.py` imports both implementations and asserts they agree on a
shared case list, plus that the HMAC formula matches. **If you change one side,
change both and run that test.**

### Nothing is ever lost

`raw_payload` stores exactly what arrived. If a mapping turns out wrong six weeks
in, the leads can be re-derived rather than re-collected.

`inbound_webhooks` records every delivery, verified or not, with the rejection
reason. "The CRM never got the lead" and "the CRM rejected the lead because the
secret was mismatched" are different problems with different fixes, and only that
table distinguishes them. The reasons are specific: a signature mismatch says the
secret differs between services; a stale timestamp says replay or clock skew.

### Roles

| Role | Sees | Can |
|---|---|---|
| `agent` | own leads + the unassigned pool | work leads, add notes, self-assign |
| `manager` | everything | assign work, read analytics, export |
| `admin` | everything | manager + staff administration |

The agent restriction is not decoration. A CSV of every lead with phone numbers is
the most portable asset in this system, and "which consultant took the database to a
competitor" is a real question in property sales. Export is manager-and-above, and
every export is logged to the activity trail with who and how many rows.

### Dashboard colour

Chart palettes were validated rather than chosen by eye — lightness band, chroma
floor, colour-vision-deficiency separation, normal-vision floor, and contrast
against both the light and dark surface:

| Set | Worst CVD ΔE | Worst normal ΔE |
|---|---|---|
| tiers, light | 19.8 | 24.1 |
| tiers, dark | 16.9 | 20.8 |
| sources | 9.2 | 24.0 |

Two slots sit below 3:1 contrast against the light surface, so the relief rule
applies and is honoured: **every mark carries a visible direct label, and every chart
has a table view.** Colour never carries a value alone. Spam is the one achromatic
slot — it is excluded from lead counts, and reading as inert grey is the correct
signal.

Dark mode is a selected set stepped for the dark surface, not an automatic flip, and
the charts read their colours from CSS custom properties so the theme toggle
repaints them without any JavaScript knowing a hex value.

## Current state

| | |
|---|---|
| ✅ **64/64 HTTP assertions** | signatures, idempotency, roles, analytics, export |
| ✅ **35/35 Meta adapter assertions** | mapping, value translation, scoring, envelope |
| ✅ Parity with the chatbot verified | 35 normalisation cases + HMAC round-trip |
| ✅ Live lead received both ways | signed webhook and pull reconciliation |
| ✅ Forged / unsigned / replayed webhooks rejected | and recorded with reasons |
| ⏳ Meta source | adapter done; awaiting credentials from Riphah marketing |

## Before this goes live

1. **Rotate `WEBHOOK_SECRET`** and match it in the assistant. The dev value is
   shared and in git history as an example.
2. **Change the bootstrap admin password**, then clear
   `BOOTSTRAP_ADMIN_PASSWORD` from `.env`.
3. **Get the Meta credentials and sign off `FIELD_MAP`** against the real live
   forms. The map is the single most likely thing to be wrong on the first real
   Meta lead.
4. **Agree the SLA targets and who is accountable** (s13). They are placeholders and
   the UI says so.
5. **Create real staff accounts** and assign owners; the demo seeder invents
   `sales1@riphah.local`.
6. **Clear the demo data** — `python eval/seed_demo.py --clear`.
7. **Decide retention** for transcripts and lead records (s13), and whether data
   subject access and deletion need to be platform features. The schema cascades
   from the lead, so a deletion is one statement, but nothing schedules it yet.
