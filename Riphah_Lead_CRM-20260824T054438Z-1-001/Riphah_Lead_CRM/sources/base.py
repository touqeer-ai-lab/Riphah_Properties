"""The source adapter contract.

Every lead source implements the same two-method shape:

    normalise(raw) -> NormalisedLead     map a source payload onto the CRM's model
    status()       -> dict               am I connected, and if not, why

`crm/ingest.py` only ever sees `NormalisedLead`, so it has no idea whether a lead
came from the chatbot, a Meta lead form, or a manual entry. That is the property
that makes adding Meta a new file rather than a change to the ingest path, the
analytics, or the dashboard.

`status()` earns its place because "pending" is a real state in this system. The
Meta adapter is written and tested but has no credentials, and a dashboard that
either hid it or showed it as broken would both be wrong. It reports pending, with
the reason, and the UI says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class NormalisedLead:
    """One lead, source-agnostic.

    `fields` holds whatever the source captured that isn't in the core columns —
    property budget, programme of interest, a Meta form's custom question. It is
    the reason a new source needs no migration.
    """
    source_key: str
    external_id: str
    portal: str | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    qualification: str = "cold"
    score: int = 0
    status: str | None = None          # None => don't overwrite the CRM's own value
    assigned_owner: str | None = None
    language: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    field_labels: dict[str, str] = field(default_factory=dict)
    needs_confirmation: list[str] = field(default_factory=list)
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    referrer: str | None = None
    device: str | None = None
    region: str | None = None
    landing_url: str | None = None
    channel: str | None = None
    consent_given: bool = False
    consent_version: str | None = None
    # Where the contact route came from: 'conversation' | 'account' | 'mixed'.
    # A Meta form fill is always 'conversation' in the sense that the person typed
    # it into the form, so the adapter leaves this None and ingest defaults it.
    contact_source: str | None = None
    # Separate from consent_given: responding to an enquiry needs no marketing
    # consent, adding the person to a nurture sequence does.
    marketing_opt_in: bool = False
    has_account: bool = False
    message_count: int = 0
    session_count: int = 0
    transcript_url: str | None = None
    captured_at: str | None = None
    source_updated_at: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def contactable(self) -> bool:
        return bool(self.email or self.phone)


class SourceAdapter(Protocol):
    key: str
    display_name: str

    def normalise(self, raw: dict[str, Any]) -> NormalisedLead: ...

    def status(self) -> dict[str, Any]: ...


VALID_QUALIFICATIONS = ("hot", "warm", "cold", "spam")
VALID_STATUSES = ("new", "contacted", "qualified", "converted", "lost", "spam")


def clean_qualification(value: Any) -> str:
    """Coerce a source's tier onto ours.

    Unknown values become `cold` rather than raising. A source that invents a new
    tier should not be able to stop lead ingestion — the lead still arrives, in the
    tier that triggers no sales action, which is the safe direction to fail.
    """
    lowered = str(value or "").strip().lower()
    return lowered if lowered in VALID_QUALIFICATIONS else "cold"


def clean_status(value: Any) -> str | None:
    lowered = str(value or "").strip().lower()
    return lowered if lowered in VALID_STATUSES else None
