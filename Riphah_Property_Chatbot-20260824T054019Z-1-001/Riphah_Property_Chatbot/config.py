"""Central configuration: paths, models, thresholds, lifecycle timings.

Everything tunable lives here or in `.env`. Two rules the rest of the codebase
depends on:

* **Nothing portal-specific belongs in this file.** Portal personas, field
  schemas and qualification rules are *data* (see `portals/`), because the whole
  point of the build is that adding the admission portal is a row, not a release.
* **No secret has a default.** A missing key must fail loudly at the call site
  rather than silently degrade into an unauthenticated request.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

# --- paths ---
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "chatbot.sqlite3"
CONTENT_DIR = ROOT / "content"          # source documents for the knowledge base
UPLOAD_DIR = DATA_DIR / "uploads"       # admin-uploaded KB documents

# --- models -------------------------------------------------------------------
# Overridable because model names move faster than this codebase will. The
# defaults are chosen for tool-calling reliability, not for benchmark scores.
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4.1")
# Extraction is a separate, cheaper model on purpose (see agent/extraction.py):
# it runs on every turn and never writes prose.
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "gpt-4.1-mini")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-large")
EMBED_DIMENSIONS = int(os.getenv("EMBED_DIMENSIONS", "1536"))
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-realtime")
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "coral")
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "whisper-1")
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")

# Sampling. Low but not zero: the assistant should read as a person, and 0.0
# makes it repeat the same phrasing every turn, which is obvious after three.
CHAT_TEMPERATURE = float(os.getenv("CHAT_TEMPERATURE", "0.3"))
# Extraction is a data path. Determinism matters more than fluency.
EXTRACT_TEMPERATURE = float(os.getenv("EXTRACT_TEMPERATURE", "0.0"))

# --- internal integration briefing (DEMO — remove before go-live) ------------
# A spoken/typed briefing for the project owner about the WhatsApp, SIP/SIM
# (Jazz/Zong) and messaging channels: what they cost, how they plug into the CRM.
# The assistant asks who is speaking and only delivers it to a name on this list.
# Set BRIEFING_ENABLED=0 (or delete these lines) when the project goes live — the
# tool, the prompt block and the audit entries all switch off together.
BRIEFING_ENABLED = os.getenv("BRIEFING_ENABLED", "0") not in ("0", "false", "no", "")
BRIEFING_AUTHORISED_NAMES = [
    name.strip() for name in os.getenv(
        "BRIEFING_AUTHORISED_NAMES", "Ali Waqas, علی وقاص").split(",")
    if name.strip()
]

# --- chunking -----------------------------------------------------------------
CHUNK_TARGET_CHARS = 1400
CHUNK_OVERLAP_CHARS = 200
# Body text repeated verbatim across this many documents is a template block
# (footers, boilerplate disclaimers), not content. See kb/chunk.py.
TEMPLATE_BLOCK_THRESHOLD = 4

# --- retrieval ----------------------------------------------------------------
DEFAULT_TOP_K = 6
# Below this cosine similarity the passage is treated as absent. The assistant
# then says it doesn't know rather than answering from a weak match — which for
# a property portal is the difference between "I don't have that" and inventing
# a payment plan.
MIN_SIMILARITY = 0.22
RRF_K = 60          # reciprocal-rank-fusion constant, standard value

# --- session lifecycle (spec s9) ---------------------------------------------
# Silence thresholds, in minutes. The spec proposes 5 and 30; both are config so
# sales can retune after watching real traffic.
IDLE_AFTER_MINUTES = int(os.getenv("IDLE_AFTER_MINUTES", "5"))
INACTIVE_AFTER_MINUTES = int(os.getenv("INACTIVE_AFTER_MINUTES", "30"))
LIFECYCLE_SWEEP_SECONDS = int(os.getenv("LIFECYCLE_SWEEP_SECONDS", "60"))

# --- abuse and cost control (spec s3) ----------------------------------------
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "2000"))
RATE_LIMIT_MESSAGES = int(os.getenv("RATE_LIMIT_MESSAGES", "30"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "300"))
MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "24"))

# --- auth ---------------------------------------------------------------------
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))
PBKDF2_ITERATIONS = int(os.getenv("PBKDF2_ITERATIONS", "600000"))
AUTH_COOKIE = "riphah_auth"
VISITOR_COOKIE = "riphah_visitor"

# --- lead delivery (spec s10) ------------------------------------------------
# Signing secret for outbound webhooks. Absent => delivery is disabled rather
# than sent unsigned; an unsigned lead webhook is an open door into the CRM.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
WEBHOOK_URL = os.getenv("LEAD_WEBHOOK_URL", "")
WEBHOOK_TIMEOUT_SECONDS = float(os.getenv("WEBHOOK_TIMEOUT_SECONDS", "10"))
# Exponential backoff, in seconds, one entry per attempt after the first.
WEBHOOK_RETRY_DELAYS = (30, 120, 600, 3600, 21600)
WEBHOOK_MAX_ATTEMPTS = len(WEBHOOK_RETRY_DELAYS) + 1

# --- lead API ----------------------------------------------------------------
API_PAGE_SIZE = 50
API_PAGE_SIZE_MAX = 200

# --- server ------------------------------------------------------------------
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8100"))
# Comma-separated origins allowed to embed the widget. "*" only for local dev —
# the widget carries a public portal key, so an open CORS policy plus that key
# is enough for anyone to run a chatbot on Riphah's OpenAI budget.
ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()
]

DEFAULT_PORTAL = os.getenv("DEFAULT_PORTAL", "riphah-property")

# --- web search fallback ------------------------------------------------------
# Used only when the approved knowledge base has nothing. It is domain-limited on
# purpose: the whole point of this build is that the assistant speaks from
# Riphah-approved material, and an open web search would put arbitrary pages —
# third-party listing sites, forum posts, stale price rumours — into the context
# of an assistant a buyer reads as the company. A page on Riphah's own domain is
# still Riphah's own statement, so the allowlist keeps that property intact.
#
# Set WEB_SEARCH_DOMAINS to an empty string to allow the whole web. Don't, unless
# someone has accepted that a scraped price on a listing site can reach a buyer.
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", ""
)
WEB_SEARCH_DOMAINS = [
    d.strip() for d in os.getenv(
        "WEB_SEARCH_DOMAINS",
        "riphahmedicalcity.com,riphah.edu.pk,riphahproperties.com",
    ).split(",") if d.strip()
]
# The hosted search runs its own model call, so it gets its own budget.
WEB_SEARCH_MODEL = os.getenv("WEB_SEARCH_MODEL", "gpt-4.1")
WEB_SEARCH_MAX_TOKENS = int(os.getenv("WEB_SEARCH_MAX_TOKENS", "700"))


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)


def openai_key() -> str:
    """The key, or a loud failure. Never returns an empty string."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if len(key) < 25:
        raise RuntimeError(
            "OPENAI_API_KEY is missing or looks like a placeholder. "
            "Copy .env.example to .env and set a real key."
        )
    return key


def has_openai_key() -> bool:
    return len(os.getenv("OPENAI_API_KEY", "").strip()) > 25
