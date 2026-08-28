# WhatsApp Integration — Meta Cloud API

WhatsApp is wired into this assistant as an **inbound channel**, not a second
bot. A WhatsApp message becomes a turn in an ordinary chat session and runs the
same pipeline as the website widget: retrieve → answer → extract lead fields →
score → deliver to the CRM. So a WhatsApp lead shows up in the CRM with a
transcript, captured fields (budget, location, property type…), a tier and a
`channel = whatsapp` tag — with no separate CRM code.

```
WhatsApp user
   │  sends a message to the business number
   ▼
Meta WhatsApp Cloud API
   │  POST (signed) to our webhook
   ▼
POST /api/webhooks/whatsapp          verify X-Hub-Signature-256, ack 200, process in background
   ▼
channels/whatsapp.py
   ├─ dedupe by Meta message id        (whatsapp_messages)
   ├─ map phone → session              (whatsapp_contacts;  visitor_id = whatsapp:<number>)
   ├─ stage phone (+ profile name)     the number IS the contact route
   ├─ transcribe voice notes           (Whisper) — text/voice answered, other media → polite fallback
   ▼
agent/chat.py  answer()               retrieval, guardrails, extraction, scoring  (unchanged)
   ├─ lead created/updated  ─────────► CRM  (signed webhook, existing path)
   ▼
Graph API  /{phone_number_id}/messages   reply sent back to the customer
```

The whole thing is **credential-gated**: with the Meta values unset the webhook
rejects every POST and `GET /api/admin/whatsapp/status` reports `pending` and
names what is missing — exactly like the Meta lead-ads source. Fill the five
values and it goes live with no code change.

---

## Part 1 — Meta Developer setup (sandbox, free)

Plain-language, in dashboard order. You need a Facebook account; a business is
**not** required for the sandbox.

1. **developers.facebook.com** → log in → **My Apps** → **Create App**.
2. Use case: **"Other"** → type: **"Business"** → name it (e.g. "Riphah
   Assistant Dev") → create.
3. On the app dashboard, find **WhatsApp** → **Set up**. This creates a free
   **test business account** and a **test WhatsApp number** (Meta owns it — you
   cannot receive on your own number in the sandbox, but you can send/receive
   with it).
4. **WhatsApp → API Setup**. Here you see:
   - **Temporary access token** (top) — valid **24 hours**. → `WHATSAPP_ACCESS_TOKEN`
   - **Phone number ID** (the *test number's* id, not the phone digits) → `WHATSAPP_PHONE_NUMBER_ID`
   - **WhatsApp Business Account ID** (WABA) → `WHATSAPP_BUSINESS_ACCOUNT_ID`
5. **Add a recipient**: under "To", **Manage phone number list** → add YOUR own
   WhatsApp number → enter the code WhatsApp sends you. The sandbox only
   delivers to numbers on this list (up to 5).
6. **App ID / App Secret**: top-left **App settings → Basic**. Reveal the
   **App secret** → `WHATSAPP_APP_SECRET`. (App ID is not needed by the code.)
7. **Webhook** (do this after the server is publicly reachable — see below):
   **WhatsApp → Configuration → Edit**:
   - **Callback URL**: `https://<your-public-host>/api/webhooks/whatsapp`
   - **Verify token**: paste the **same string** you put in `WHATSAPP_VERIFY_TOKEN`
   - Click **Verify and save** — Meta calls your GET endpoint with the token and
     expects the challenge echoed back. Our endpoint does that automatically.
   - **Manage** → subscribe to the **messages** field. (That is the only field
     needed; it carries both messages and delivery statuses.)

### Making the webhook publicly reachable

Meta must reach your server over **HTTPS on a public URL**. Locally, tunnel it:

```
ngrok http 8100
# → https://xxxx.ngrok-free.dev  — use this as the Callback URL base
```

In production this is your real domain behind HTTPS (see Part 9).

---

## Part 2 — Configuration

Everything lives in `.env` (never in code — see Part 10). Copy from
`.env.example`:

```bash
WHATSAPP_VERIFY_TOKEN=riphah-wa-verify-7c2e9a41   # your choice; matches Meta's form
WHATSAPP_APP_SECRET=<App settings → Basic>
WHATSAPP_ACCESS_TOKEN=<API Setup, 24h in sandbox>
WHATSAPP_PHONE_NUMBER_ID=<API Setup>
WHATSAPP_BUSINESS_ACCOUNT_ID=<API Setup>
WHATSAPP_API_VERSION=v21.0
WHATSAPP_PORTAL=riphah-property
```

Restart the server, then check:

```bash
curl -s localhost:8100/api/admin/whatsapp/status   # needs an admin cookie
# → {"status":"live", ...}  or  {"status":"pending","missing_config":[...]}
```

### Endpoints (already built)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/webhooks/whatsapp` | Meta's verify handshake — echoes the challenge |
| POST | `/api/webhooks/whatsapp` | Inbound messages + delivery statuses (signed) |
| GET | `/api/admin/whatsapp/status` | Channel state + recent conversations (admin) |

---

## Part 3 — Data model

WhatsApp reuses the existing `chat_sessions`, `messages`, `leads`,
`lead_field_values`, `session_field_values` tables. Two channel-only tables were
added (`schema.sql` section 6):

- **`whatsapp_contacts`** — one row per phone: `wa_id` (the number),
  `profile_name`, the current `session_id`, counts. This is what makes a
  returning customer continue their conversation instead of starting over.
- **`whatsapp_messages`** — one row per Meta message id, inbound and outbound,
  with `direction` and delivery `status` (sent → delivered → read → failed).
  The primary key on Meta's id is the **dedupe**: a redelivered webhook is a
  no-op.

Identity link: a WhatsApp session's `visitor_id` is `whatsapp:<number>`, and the
phone is staged as the lead's contact route, so lead **deduplication by phone**
keeps one customer as one lead across sessions and even across channels (a person
who used the web chat and later WhatsApp shares a `person_key` in the CRM).

---

## Part 5 — AI / lead extraction

No WhatsApp-specific AI code. `agent/chat.py:answer()` already runs a **second
model call** (`agent/extraction.py`) every turn that returns structured fields
against the portal's schema — `property_type`, `location`, `budget`, `purpose`,
`size`, `timeline`, name, phone, email — classifies each as
stated/implied/asked-about, drops the weak ones, and folds the rest onto the
lead. Your example ("5 marla house in Islamabad, budget 2 crore") produces those
four fields and updates the lead automatically.

### Using your local Qwen instead of OpenAI

The extractor and the reply both go through the OpenAI **client**, which speaks a
format many servers emulate. Point it at an OpenAI-compatible endpoint:

```bash
# vLLM or Ollama serving Qwen with an OpenAI-compatible API:
OPENAI_BASE_URL=http://127.0.0.1:8000/v1
OPENAI_API_KEY=any-nonempty-string        # most local servers ignore it
CHAT_MODEL=Qwen2.5-14B-Instruct
EXTRACT_MODEL=Qwen2.5-14B-Instruct
```

Caveats: **embeddings** (knowledge-base retrieval) and **voice/Whisper** still
need a provider that offers them — either keep an OpenAI key for those two, or
swap `kb/embed.py` for a local embedding model. Extraction relies on the model
returning clean JSON for tool calls; Qwen2.5-14B-Instruct and up handle it,
smaller models are less reliable.

---

## Part 6 — Conversation, dedupe, media, statuses

- **Continuity**: `whatsapp_contacts.session_id`. Same number tomorrow → same
  session. Once a session is sealed (lead finalised and sent), the next message
  opens a fresh session under the same `visitor_id`; the lead stays one lead
  because dedupe is by phone.
- **Duplicate webhooks**: Meta retries anything not answered with 200 in time.
  We ack immediately and process in the background; `whatsapp_messages` keyed by
  Meta id makes a retry a no-op. Verified in the test suite.
- **Delivery / read status**: status webhooks update the outbound row
  (`sent → delivered → read`, or `failed` with Meta's error).
- **Voice notes**: downloaded and transcribed (Whisper), then answered as text —
  so a spoken budget still becomes a lead field.
- **Images / documents / stickers / video**: the pipeline cannot read them, so
  the customer gets a short bilingual "please type or send a voice note" reply
  and the model is not called. A caption on an image is answered. (Full document
  understanding is a future add — the media is downloadable via
  `download_media()` when you want to store or OCR it.)

---

## Part 7 — CRM dashboard

WhatsApp leads appear in the existing CRM automatically — same Leads list,
filters, and the **Chats** panel — tagged `whatsapp`. Nothing new to build. The
chatbot admin also exposes `GET /api/admin/whatsapp/status` with the recent
WhatsApp conversation list (number, name, session, lead ref) if you want a
channel-specific view.

---

## Part 8 — Sandbox limits (free) vs production

| | Sandbox (free) | Production |
|---|---|---|
| Cost | Free | Free to receive; per-message fee to send templates |
| Business verification | **Not** required | Required (Meta Business Verification) |
| WhatsApp number | Meta's **test** number | Your **own** business number (a new SIM, not on the WhatsApp app) |
| Recipients | Up to **5**, pre-registered | Anyone who messages you |
| Access token | **Temporary, 24 h** | Permanent **system-user** token |
| Who can start | Only the 5 test numbers, and they must message first | Any customer |
| Free-form replies | Inside the 24 h customer window (fine for a chatbot) | Same; templates needed to *initiate* outside it |

For this assistant the sandbox is enough to prove the whole flow end to end.
The customer always messages first, so the 24-hour window is always open and no
paid templates are needed.

---

## Part 9 — Production migration

1. **Business verification**: Meta Business Manager → verify Riphah (registration
   docs, domain, official email). 1–2 weeks, mostly Meta's queue.
2. **Real number**: add Riphah's business number to the WhatsApp Business
   Account (a fresh SIM that is **not** on the normal WhatsApp app), verify it.
3. **Permanent token**: Business Manager → **System Users** → create a system
   user → assign the WhatsApp app → **Generate token** with `whatsapp_business_messaging`
   + `whatsapp_business_management`, no expiry → `WHATSAPP_ACCESS_TOKEN`.
4. **New Phone Number ID**: the production number has its own id → update
   `WHATSAPP_PHONE_NUMBER_ID`.
5. **Webhook**: point the Callback URL at the production HTTPS domain
   (`https://assistant.riphahproperties.com/api/webhooks/whatsapp`), same verify
   token, subscribe **messages**.
6. Everything else — code, CRM, extraction — is unchanged. Only `.env` values move.

---

## Part 10 — Security

- All secrets live in **`.env`**, which is git-ignored — never in source.
- **Every inbound POST is verified** by HMAC-SHA256 over the raw body against
  `WHATSAPP_APP_SECRET`. Unset secret → every POST rejected (no unverified writes).
- The **verify token** only gates the one-time GET handshake; the app-secret
  signature is the real control on message traffic.
- **Access token**: rotate the 24-h sandbox token as it expires; in production a
  system-user token, revocable from Business Manager. Never logged.
- **Database / webhook secret**: as today — `.env`, HMAC-signed CRM delivery.
- For production, put `.env` behind the OS user only, or a secrets manager
  (AWS Secrets Manager / Doppler); the code reads plain environment variables so
  any of them drops in.

---

## Part 11 — Testing

**Automated** (free, no Meta, no model spend — stubs the Graph API and the
model, runs against a copy of the DB):

```bash
python scratchpad/test_whatsapp.py   # 11 checks: handshake, signature, dedupe,
                                      # continuity, seal, status, media, fallback
```

**Live sandbox**, once the webhook is verified in the dashboard:

1. From a registered test number, send **"Hello"** to the test number.
2. `GET /api/admin/whatsapp/status` → `inbound_messages` increments; a
   `whatsapp_contacts` row appears.
3. CRM → the number appears as a session; a lead is created once a
   qualification field is stated.
4. Reply arrives on WhatsApp (the assistant's answer).
5. Send **"5 marla house in Islamabad, budget 2 crore"** → CRM lead now shows
   property type / location / budget captured, tier updated.
6. Send another message → **same** lead updates, no duplicate customer.
7. Send a voice note → it is transcribed and answered.
8. Send an image → polite "please type or send a voice note" reply.
9. Check `whatsapp_messages` → outbound rows move to `delivered`/`read`.
```
