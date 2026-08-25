-- Riphah Lead CRM.
--
-- This is a *consumer* schema, not a copy of the chatbot's. Two rules shape it:
--
--   1. **Source-agnostic core.** `leads` holds what every source can provide;
--      whatever is specific to a source goes in `lead_fields` as rows, and the
--      untouched original payload goes in `raw_payload`. That is what lets the
--      Meta adapter land without a migration.
--   2. **Never lose the original.** `raw_payload` keeps exactly what arrived. If
--      the mapping turns out to be wrong six weeks in, the leads can be
--      re-derived instead of re-collected.
--
-- The CRM owns the sales process: status, owner, activity, notes. It does not own
-- the captured record — that is evidence of what a visitor said, and it is
-- replaced on each upsert from the source of truth.

PRAGMA journal_mode = WAL;

-- Where leads come from. A row per integration, so the dashboard can report
-- source mix and show an honest connection state for each.
CREATE TABLE IF NOT EXISTS sources (
    key             TEXT PRIMARY KEY,          -- 'chatbot' | 'meta' | 'manual'
    display_name    TEXT NOT NULL,
    -- live | pending | disabled. 'pending' is a first-class state, not an error:
    -- Meta is specified and coded but has no credentials yet, and a dashboard that
    -- hid it would misrepresent the pipeline.
    status          TEXT NOT NULL DEFAULT 'pending',
    detail          TEXT,                      -- why it is in that state
    last_sync_at    TEXT,
    last_cursor     TEXT,                      -- high-water mark for incremental pull
    leads_received  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- The source's own identifier. (source_key, external_id) is the idempotency
    -- key: a webhook redelivered three times must produce one lead.
    source_key      TEXT NOT NULL REFERENCES sources(key),
    external_id     TEXT NOT NULL,
    portal          TEXT,                      -- 'riphah-property'
    name            TEXT,
    email           TEXT,
    phone           TEXT,
    email_norm      TEXT,
    phone_norm      TEXT,
    -- Cross-source identity. Two rows can be the same human (chatbot + Meta form),
    -- so they share a person_key and the dashboard can collapse them.
    person_key      TEXT,
    qualification   TEXT NOT NULL DEFAULT 'cold',
    score           INTEGER NOT NULL DEFAULT 0,
    -- CRM-owned sales process.
    status          TEXT NOT NULL DEFAULT 'new',
    assigned_owner  TEXT,
    language        TEXT,
    -- Attribution, flattened from the source payload for cheap grouping.
    utm_source      TEXT,
    utm_medium      TEXT,
    utm_campaign    TEXT,
    referrer        TEXT,
    device          TEXT,
    region          TEXT,
    landing_url     TEXT,
    channel         TEXT,                      -- text | voice | mixed | form
    consent_given   INTEGER NOT NULL DEFAULT 0,
    consent_version TEXT,
    -- 'conversation' | 'account' | 'mixed'. See core/db.py:_ADDED_COLUMNS.
    contact_source  TEXT,
    -- A separate legal basis from consent_given above, and never conflated with it:
    -- responding to an active enquiry needs no marketing consent, adding the person
    -- to a nurture sequence does.
    marketing_opt_in INTEGER NOT NULL DEFAULT 0,
    has_account     INTEGER NOT NULL DEFAULT 0,
    message_count   INTEGER NOT NULL DEFAULT 0,
    session_count   INTEGER NOT NULL DEFAULT 0,
    transcript_url  TEXT,
    -- Timestamps. `captured_at` is when the *source* captured it, which is what
    -- analytics must use — `created_at` here would measure when this service
    -- happened to be running.
    captured_at     TEXT,
    source_updated_at TEXT,
    first_response_at TEXT,                    -- first outbound sales contact
    closed_at       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    raw_payload     TEXT,                      -- verbatim, for re-derivation
    UNIQUE (source_key, external_id)
);

CREATE INDEX IF NOT EXISTS idx_leads_captured ON leads(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_qual ON leads(qualification, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_owner ON leads(assigned_owner);
CREATE INDEX IF NOT EXISTS idx_leads_person ON leads(person_key);
CREATE INDEX IF NOT EXISTS idx_leads_source ON leads(source_key, captured_at DESC);

-- Source-specific captured fields, one row each. The reason a Meta form with
-- fields the property portal never had needs no DDL.
CREATE TABLE IF NOT EXISTS lead_fields (
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    field_key       TEXT NOT NULL,
    value           TEXT,
    label           TEXT,                      -- from the portal field schema
    -- Flagged by the source as inferred rather than stated. Surfaced in the UI so
    -- a consultant confirms it instead of trusting it.
    needs_confirmation INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (lead_id, field_key)
);

-- Sales activity. Every status change, assignment, note and contact attempt.
-- This table is what makes response-time analytics possible at all — without it
-- "how fast do we call hot leads" is unanswerable.
CREATE TABLE IF NOT EXISTS activity (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    actor           TEXT NOT NULL,
    kind            TEXT NOT NULL,   -- ingested | status | assigned | note | contacted | export
    detail          TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_lead ON activity(lead_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_activity_kind ON activity(kind, created_at DESC);

CREATE TABLE IF NOT EXISTS notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    author          TEXT NOT NULL,
    body            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_lead ON notes(lead_id, id DESC);

-- Transcripts, fetched lazily from the chatbot the first time someone opens a
-- lead. Cached because a consultant preparing for a call reads it repeatedly, and
-- because the chatbot may retire an old session before the CRM is done with it.
CREATE TABLE IF NOT EXISTS transcripts (
    lead_id         INTEGER PRIMARY KEY REFERENCES leads(id) ON DELETE CASCADE,
    session_id      TEXT,
    body            TEXT NOT NULL,             -- JSON message array
    fetched_at      TEXT NOT NULL
);

-- Staff accounts. Separate from the chatbot's `users`: a sales consultant is not
-- a website visitor, and the two systems should not share a credential store.
CREATE TABLE IF NOT EXISTS staff (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name            TEXT,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'agent',   -- agent | manager | admin
    created_at      TEXT NOT NULL,
    last_login_at   TEXT,
    disabled_at     TEXT
);

CREATE TABLE IF NOT EXISTS staff_sessions (
    token_hash      TEXT PRIMARY KEY,
    staff_id        INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    revoked_at      TEXT
);

-- Every inbound webhook, verified or not. A rejected delivery is kept
-- deliberately: "the CRM never got the lead" and "the CRM rejected the lead
-- because the signature was wrong" are different problems with different fixes,
-- and only this table can tell them apart.
CREATE TABLE IF NOT EXISTS inbound_webhooks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key      TEXT NOT NULL,
    event           TEXT,
    signature_valid INTEGER NOT NULL DEFAULT 0,
    reject_reason   TEXT,
    lead_id         INTEGER,
    body            TEXT NOT NULL,
    received_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inbound_received
    ON inbound_webhooks(received_at DESC);
