"""CRM configuration.

The CRM is a separate service with its own database, on purpose. It could have
read the chatbot's SQLite file directly and saved a lot of code — but then the
integration contract in the scope document (s9) would never actually be
exercised, and the first real CRM Riphah picks would be integrating against an
API nobody had tested from the outside.

So this service consumes the chatbot exactly the way Salesforce or Zoho would:
signed webhooks for push, an API key against `/api/v1/leads` for pull. If this
CRM works, a commercial one will too.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "crm.sqlite3"
EXPORT_DIR = DATA_DIR / "exports"

# --- chatbot integration ------------------------------------------------------
# Must match WEBHOOK_SECRET in the chatbot's .env. Without it, inbound webhooks
# are rejected rather than trusted — an unauthenticated write into the CRM is
# worse than a missed lead, because a missed lead can be back-filled by the pull
# reconciler below and a poisoned one cannot be found again.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
WEBHOOK_MAX_AGE_SECONDS = int(os.getenv("WEBHOOK_MAX_AGE_SECONDS", "300"))

CHATBOT_BASE_URL = os.getenv("CHATBOT_BASE_URL", "http://127.0.0.1:8100")
CHATBOT_API_KEY = os.getenv("CHATBOT_API_KEY", "")

# The pull reconciler exists because push alone is not enough. A webhook can be
# lost while this service is restarting, and the CRM has no way to know what it
# never received. Polling `/api/v1/leads?since=` closes that gap.
PULL_INTERVAL_SECONDS = int(os.getenv("PULL_INTERVAL_SECONDS", "300"))
PULL_OVERLAP_MINUTES = int(os.getenv("PULL_OVERLAP_MINUTES", "15"))
PULL_ENABLED = os.getenv("PULL_ENABLED", "1") not in ("0", "false", "no")

# --- Meta lead ads (PENDING) -------------------------------------------------
# The second lead source. Not implemented: the client has not supplied the page
# id, the app credentials, or a decision about which form fields map to which CRM
# fields. See sources/meta.py — the adapter, the field mapping and the ingest path
# are all written and tested against a fixture; only the live credentials are
# missing.
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
META_PAGE_ACCESS_TOKEN = os.getenv("META_PAGE_ACCESS_TOKEN", "")
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "")
META_PAGE_ID = os.getenv("META_PAGE_ID", "")

# --- outbound comms: WhatsApp + SIP calling (PENDING until credentials) --------
# Same philosophy as the Meta source above: the send/originate paths, the UI
# buttons and the activity logging are all built and wired. What is missing is
# credentials. Fill these and the buttons flip from fallback links to real API
# calls with no code change.
#
# WhatsApp Business Cloud API (Meta): the token and phone-number id come from
# the same Meta app the lead-ads credentials will. developers.facebook.com →
# WhatsApp → API Setup.
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v21.0")

# SIP click-to-call. Most PBXes (Asterisk ARI, 3CX, FreePBX, Twilio, a SIP
# trunk provider's REST bridge) expose an HTTP endpoint that originates a call
# between a consultant's extension and an outside number. The CRM POSTs
# {"to", "caller_id", "lead_ref", "agent"} there with a bearer token.
SIP_ORIGINATE_URL = os.getenv("SIP_ORIGINATE_URL", "")
SIP_ORIGINATE_TOKEN = os.getenv("SIP_ORIGINATE_TOKEN", "")
SIP_CALLER_ID = os.getenv("SIP_CALLER_ID", "")

# --- auth ---------------------------------------------------------------------
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "7"))
PBKDF2_ITERATIONS = int(os.getenv("PBKDF2_ITERATIONS", "600000"))
AUTH_COOKIE = "riphah_crm"

# First-run bootstrap. Creating an admin on first boot beats shipping a default
# password, but it is still a credential in an env file — the README says to
# change it after the first login.
BOOTSTRAP_ADMIN_EMAIL = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "admin@riphah.local")
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")

# --- SLA ----------------------------------------------------------------------
# "What is the expected response time for a hot lead, and who is accountable for
# it?" is an open item in the scope document (s13). These are the placeholders the
# breach report measures against; they are surfaced in the UI as provisional so
# nobody mistakes them for an agreed SLA.
SLA_HOURS = {
    "hot": int(os.getenv("SLA_HOT_HOURS", "24")),
    "warm": int(os.getenv("SLA_WARM_HOURS", "72")),
    "cold": 0,   # no sales action
}

# --- analytics ----------------------------------------------------------------
# A lead with no activity for this long is "dormant" in the pipeline view. Not the
# same as the chatbot's session lifecycle: that measures a conversation going
# quiet in minutes, this measures a sales process going quiet in days.
DORMANT_AFTER_DAYS = int(os.getenv("DORMANT_AFTER_DAYS", "14"))
DEFAULT_ANALYTICS_DAYS = int(os.getenv("DEFAULT_ANALYTICS_DAYS", "30"))

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8200"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
