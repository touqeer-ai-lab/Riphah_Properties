-- Riphah AI Property Assistant — single SQLite schema.
--
-- Four concerns in one file, in dependency order:
--
--   1. portals + portal_fields   configuration that makes the engine reusable
--   2. kb_documents + kb_chunks  the retrieval corpus (prose + embeddings + FTS)
--   3. users + chat_sessions     identity and conversation history
--   4. leads + delivery          captured leads, scoring, and outbound integration
--
-- Design rules that the code relies on:
--   * Every captured field is a ROW, not a column (lead_field_values). Adding a
--     field to the admission portal must never be a migration.
--   * Every fact carries provenance. KB chunks carry document + section; lead
--     field values carry confidence + the message they came from.
--   * Deletes cascade from the aggregate root, so GDPR-style "delete this
--     visitor" is one statement rather than a cleanup script.

PRAGMA journal_mode = WAL;

-- =============================================================== 1. portals

-- Portal registry. One row per customer-facing surface (property, admission,
-- ...). `persona`, `greeting` and `scoring_rules` are the levers that change
-- behaviour without a deploy.
CREATE TABLE IF NOT EXISTS portals (
    portal_key      TEXT PRIMARY KEY,          -- 'riphah-property'
    display_name    TEXT NOT NULL,
    persona         TEXT NOT NULL,             -- injected into the system prompt
    greeting        TEXT,
    languages       TEXT NOT NULL DEFAULT '["en"]',   -- JSON array of ISO codes
    -- Domains permitted to embed the widget with this key (spec stage 1). JSON
    -- array; empty array means "localhost only".
    allowed_domains TEXT NOT NULL DEFAULT '[]',
    knowledge_scope TEXT NOT NULL DEFAULT '[]',       -- JSON array of project slugs
    consent_notice  TEXT,
    consent_version TEXT NOT NULL DEFAULT 'v1',
    -- Require sign-in before the chat opens. Enforced server-side in /api/chat,
    -- not just hidden in the UI — a frontend-only gate is decoration.
    require_auth    INTEGER NOT NULL DEFAULT 0,
    -- One of: refer | indicative | live. Governs how pricing questions are
    -- answered (spec s8). Defaults to the most conservative option, so a portal
    -- created without an explicit decision cannot quote a price.
    pricing_mode    TEXT NOT NULL DEFAULT 'refer',
    scoring_rules   TEXT,                      -- JSON, see leads/scoring.py
    branding        TEXT,                      -- JSON: colours, logo url
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- The field schema. This table is why the same engine serves a property portal
-- and an admission portal: `agent/extraction.py` builds its function-calling
-- parameter object straight from these rows.
CREATE TABLE IF NOT EXISTS portal_fields (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    portal_key      TEXT NOT NULL REFERENCES portals(portal_key) ON DELETE CASCADE,
    field_key       TEXT NOT NULL,             -- 'budget_max'
    label           TEXT NOT NULL,             -- 'Budget ceiling'
    field_type      TEXT NOT NULL,             -- text | enum | money | int | bool | phone | email
    options         TEXT,                      -- JSON array, enum only
    required        INTEGER NOT NULL DEFAULT 0,
    -- Ask order. The assistant asks at most one question per turn, so this is
    -- the priority list it walks (leads/store.py:next_question).
    sort_order      INTEGER NOT NULL DEFAULT 100,
    -- What the assistant should say to obtain this field. Hand-written per field
    -- because "What's your budget?" and "Roughly what budget are you working
    -- with?" convert differently, and that is a copy decision, not a model one.
    prompt_hint     TEXT,
    -- Extraction guidance handed to the model, e.g. how to read "2.5 crore".
    extract_hint    TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (portal_key, field_key)
);

CREATE INDEX IF NOT EXISTS idx_portal_fields_portal
    ON portal_fields(portal_key, sort_order);

-- ========================================================= 2. knowledge base

CREATE TABLE IF NOT EXISTS kb_documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    portal_key      TEXT NOT NULL REFERENCES portals(portal_key) ON DELETE CASCADE,
    slug            TEXT NOT NULL,
    title           TEXT NOT NULL,
    project         TEXT,                      -- 'riphah-medical-city'
    source          TEXT,                      -- filename or URL
    -- spec s6.1. 'restricted' documents are rejected at ingest, not filtered at
    -- query time — the safest place to enforce it is before the text exists in
    -- the corpus at all.
    classification  TEXT NOT NULL DEFAULT 'public',   -- public | reference | volatile | restricted
    text            TEXT NOT NULL,
    char_count      INTEGER NOT NULL,
    content_hash    TEXT NOT NULL,             -- unchanged content is never re-embedded
    version         INTEGER NOT NULL DEFAULT 1,
    -- Nothing reaches the live assistant until published (spec s6 step 11).
    published       INTEGER NOT NULL DEFAULT 0,
    published_at    TEXT,
    retired_at      TEXT,                      -- set => passages leave retrieval
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (portal_key, slug, version)
);

CREATE INDEX IF NOT EXISTS idx_kb_documents_live
    ON kb_documents(portal_key, published, retired_at);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id     INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
    ordinal         INTEGER NOT NULL,
    heading         TEXT,
    text            TEXT NOT NULL,
    -- denormalised so the vector store can filter without joining
    portal_key      TEXT NOT NULL,
    project         TEXT,
    classification  TEXT NOT NULL DEFAULT 'public',
    embedding       BLOB,                      -- float32, EMBED_DIMENSIONS long
    embed_model     TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (document_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_portal ON kb_chunks(portal_key);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_pending ON kb_chunks(id) WHERE embedding IS NULL;

-- Keyword half of hybrid retrieval. Vectors miss exact tokens ("3-bed",
-- "Block C", "Pharm-D"); FTS catches them. kb/retrieve.py fuses both with RRF.
CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
    text,
    heading,
    content='kb_chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS kb_chunks_fts_insert AFTER INSERT ON kb_chunks BEGIN
    INSERT INTO kb_chunks_fts(rowid, text, heading) VALUES (new.id, new.text, new.heading);
END;
CREATE TRIGGER IF NOT EXISTS kb_chunks_fts_delete AFTER DELETE ON kb_chunks BEGIN
    INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, text, heading)
        VALUES ('delete', old.id, old.text, old.heading);
END;
CREATE TRIGGER IF NOT EXISTS kb_chunks_fts_update AFTER UPDATE ON kb_chunks BEGIN
    INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, text, heading)
        VALUES ('delete', old.id, old.text, old.heading);
    INSERT INTO kb_chunks_fts(rowid, text, heading) VALUES (new.id, new.text, new.heading);
END;

-- ================================================= 3. identity and sessions

-- Registered accounts. Optional: a visitor can chat without one. Signing in
-- upgrades an anonymous session by claiming it (auth/users.py:claim_sessions),
-- which is what makes "my previous chats" work across devices.
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name            TEXT,
    phone           TEXT,
    password_hash   TEXT NOT NULL,             -- pbkdf2$iterations$salt$hash
    role            TEXT NOT NULL DEFAULT 'visitor',   -- visitor | agent | admin
    -- Marketing consent captured at signup, separate from the per-session
    -- consent record. Two different legal bases; don't conflate them.
    marketing_opt_in INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT,
    disabled_at     TEXT
);

-- Bearer sessions. Only the SHA-256 of the token is stored, so a database dump
-- does not hand over live sessions.
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash      TEXT PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_agent      TEXT,
    ip              TEXT,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    revoked_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);

-- One row per chat conversation (spec stage 2). Survives refresh because the
-- browser keeps only the id; the turns live here.
CREATE TABLE IF NOT EXISTS chat_sessions (
    id              TEXT PRIMARY KEY,          -- uuid4, minted server-side
    portal_key      TEXT NOT NULL REFERENCES portals(portal_key) ON DELETE CASCADE,
    -- Anonymous browser identity, so a signed-out visitor still gets their own
    -- history and can be linked to an account later.
    visitor_id      TEXT,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    title           TEXT,
    -- Lifecycle status (spec s9): active | idle | inactive
    status          TEXT NOT NULL DEFAULT 'active',
    channel         TEXT NOT NULL DEFAULT 'text',   -- text | voice | mixed
    language        TEXT,
    turn_count      INTEGER NOT NULL DEFAULT 0,
    -- source metadata, captured once at session creation
    landing_url     TEXT,
    referrer        TEXT,
    utm_source      TEXT,
    utm_medium      TEXT,
    utm_campaign    TEXT,
    device          TEXT,
    region          TEXT,                      -- coarse, from IP
    ip_hash         TEXT,                      -- hashed: needed for rate limiting, not for identity
    -- consent record (spec stage 2)
    consent_given   INTEGER NOT NULL DEFAULT 0,
    consent_version TEXT,
    consent_at      TEXT,
    started_at      TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    closed_at       TEXT,                      -- set when finalised; transcript sealed
    sealed          INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_visitor ON chat_sessions(visitor_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_user ON chat_sessions(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_status ON chat_sessions(status, last_activity_at);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,             -- user | assistant | tool
    content         TEXT,
    -- Tool turns are stored so a transcript shows which retrieval produced an
    -- answer. Without this a disputed price is uninvestigable.
    tool_name       TEXT,
    tool_input      TEXT,                      -- JSON
    tool_found      INTEGER,
    -- Passages that grounded an assistant turn: JSON [{document, heading, similarity}]
    citations       TEXT,
    channel         TEXT NOT NULL DEFAULT 'text',
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

-- Fields extracted before a lead exists.
--
-- The lead creation trigger is "one contact route plus one qualification field"
-- (spec stage 7), so a visitor who states a budget on turn 3 and gives a phone
-- number on turn 6 has three turns where extracted data has no lead to live on.
-- Discarding it there would break the promise that matters most in s2.1 — never
-- re-ask something already provided — because `captured` would come back empty
-- and the assistant would ask for the budget again.
--
-- So extraction always writes here, and `leads/store.py:promote_session_fields`
-- moves the rows onto the lead the moment one is created. Rows are kept after
-- promotion as the session-level record of what was said when.
CREATE TABLE IF NOT EXISTS session_field_values (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    field_key       TEXT NOT NULL,
    value           TEXT,
    value_raw       TEXT,
    confidence      REAL NOT NULL DEFAULT 1.0,
    source_message_id INTEGER,
    promoted        INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    UNIQUE (session_id, field_key)
);

CREATE INDEX IF NOT EXISTS idx_session_field_values_session
    ON session_field_values(session_id);

-- Questions the knowledge base could not answer (spec stage 12). This is the
-- content backlog: it says what to write next, in the visitor's own words.
CREATE TABLE IF NOT EXISTS knowledge_gaps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    portal_key      TEXT NOT NULL,
    session_id      TEXT,
    question        TEXT NOT NULL,
    top_similarity  REAL,
    language        TEXT,
    resolved_at     TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_gaps_open
    ON knowledge_gaps(portal_key, resolved_at);

-- ================================================================= 4. leads

CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_ref        TEXT NOT NULL UNIQUE,      -- 'LD-2026-04817', the external id
    portal_key      TEXT NOT NULL REFERENCES portals(portal_key) ON DELETE CASCADE,
    -- The session that created the lead. Later sessions attach via lead_sessions,
    -- so a returning visitor enriches one lead instead of spawning duplicates.
    session_id      TEXT REFERENCES chat_sessions(id) ON DELETE SET NULL,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    name            TEXT,
    email           TEXT,
    phone           TEXT,
    -- Normalised copies used for deduplication only. Lowercased email, E.164
    -- phone. Kept separate so the displayed value stays as the visitor typed it.
    email_norm      TEXT,
    phone_norm      TEXT,
    -- Where the contact route came from: 'conversation' (typed into the chat),
    -- 'account' (taken from a signed-in user), or 'mixed'. A number volunteered
    -- mid-enquiry is a warmer signal than one attached to an account, and the
    -- consent basis differs, so the two are never conflated.
    contact_source  TEXT NOT NULL DEFAULT 'conversation',
    -- Marketing consent, copied from the account when one exists. Responding to an
    -- active enquiry needs no marketing consent; adding someone to a nurture
    -- sequence does. See core/db.py:_ADDED_COLUMNS.
    marketing_opt_in INTEGER NOT NULL DEFAULT 0,
    qualification   TEXT NOT NULL DEFAULT 'cold',   -- hot | warm | cold | spam
    score           INTEGER NOT NULL DEFAULT 0,
    score_detail    TEXT,                      -- JSON: which rules fired, for audit
    -- Sales-owned status (spec s9), distinct from the session lifecycle.
    status          TEXT NOT NULL DEFAULT 'new',    -- new | contacted | qualified | converted | lost | spam
    assigned_owner  TEXT,
    language        TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_contacted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_leads_portal ON leads(portal_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email_norm) WHERE email_norm IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone_norm) WHERE phone_norm IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_qualification ON leads(qualification, created_at DESC);

-- Many-to-many: one lead, many conversations over time.
CREATE TABLE IF NOT EXISTS lead_sessions (
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    session_id      TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    linked_at       TEXT NOT NULL,
    PRIMARY KEY (lead_id, session_id)
);

-- One row per captured field. This is the table that makes the field schema
-- configurable — a new portal field needs no DDL.
CREATE TABLE IF NOT EXISTS lead_field_values (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    field_key       TEXT NOT NULL,
    -- Both forms are kept: `value_raw` is what the visitor said, `value` is the
    -- normalised form ("2.5 crore" -> "25000000"). Sales needs the first to
    -- judge the lead; the CRM needs the second to filter on it.
    value           TEXT,
    value_raw       TEXT,
    -- 0..1 from the extractor. Low-confidence values are surfaced for human
    -- confirmation rather than silently trusted (spec stage 6).
    confidence      REAL NOT NULL DEFAULT 1.0,
    source_message_id INTEGER,                 -- audit trail back to the exact turn
    -- 'assistant' extracted it, or a human corrected it in the dashboard.
    source          TEXT NOT NULL DEFAULT 'assistant',
    updated_at      TEXT NOT NULL,
    UNIQUE (lead_id, field_key)
);

CREATE TABLE IF NOT EXISTS notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    session_id      TEXT REFERENCES chat_sessions(id) ON DELETE CASCADE,
    author          TEXT NOT NULL,
    body            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_lead ON notes(lead_id, id DESC);

-- API keys for the lead API. Per portal and per consumer so the CRM's key can
-- be revoked without disturbing anyone else's (spec s9.2).
CREATE TABLE IF NOT EXISTS api_keys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash        TEXT NOT NULL UNIQUE,      -- sha256; the key itself is shown once
    key_prefix      TEXT NOT NULL,             -- first 8 chars, so keys are identifiable in a UI
    label           TEXT NOT NULL,
    portal_key      TEXT,                      -- NULL => all portals
    scopes          TEXT NOT NULL DEFAULT '["leads:read"]',   -- JSON array
    created_at      TEXT NOT NULL,
    last_used_at    TEXT,
    revoked_at      TEXT
);

-- Outbound webhook attempts (spec stage 10). A CRM outage must never lose a
-- lead, so every attempt is a row and the retry state is durable.
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    event           TEXT NOT NULL,             -- lead.created | lead.updated
    target_url      TEXT NOT NULL,
    payload         TEXT NOT NULL,             -- JSON, exactly as signed
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending | delivered | failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_status_code INTEGER,
    last_error      TEXT,
    next_attempt_at TEXT,
    created_at      TEXT NOT NULL,
    delivered_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_webhook_due
    ON webhook_deliveries(status, next_attempt_at);

-- Who changed what. Configuration edits, knowledge publishing, lead edits,
-- exports (spec s10).
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    entity          TEXT,
    entity_id       TEXT,
    detail          TEXT,                      -- JSON
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);

-- ============================================================ 6. whatsapp
-- Inbound WhatsApp (Meta Cloud API) rides on the same sessions, messages and
-- leads as the web chat. These two tables are only the channel's bookkeeping:
-- which phone maps to which conversation, and which Meta message ids have
-- already been handled — Meta redelivers a webhook it did not get a 200 for,
-- so without the second table one message could be answered twice.
CREATE TABLE IF NOT EXISTS whatsapp_contacts (
    wa_id           TEXT PRIMARY KEY,          -- E.164 digits, no '+', as Meta sends it
    profile_name    TEXT,                      -- WhatsApp display name, not verified
    session_id      TEXT REFERENCES chat_sessions(id) ON DELETE SET NULL,
    portal_key      TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    message_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS whatsapp_messages (
    wa_message_id   TEXT PRIMARY KEY,          -- Meta's 'wamid.…'
    wa_id           TEXT NOT NULL,
    direction       TEXT NOT NULL,             -- in | out
    session_id      TEXT,
    message_type    TEXT,                      -- text | audio | image | document | …
    status          TEXT,                      -- out: sent | delivered | read | failed
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_contact
    ON whatsapp_messages(wa_id, created_at DESC);
