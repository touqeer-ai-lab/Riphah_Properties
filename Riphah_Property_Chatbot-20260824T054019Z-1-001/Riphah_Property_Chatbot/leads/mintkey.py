"""Mint and manage API keys from the command line.

    python -m leads.mintkey --label voice-hub --scopes chat:proxy
    python -m leads.mintkey --label riphah-crm --scopes leads:read leads:write
    python -m leads.mintkey --list
    python -m leads.mintkey --revoke rip_JOhrS9LX

The key is printed **once**. Only its SHA-256 is stored, so there is no way to
recover it later — that is the point, and it is why this prints a reminder rather
than assuming the operator will notice.
"""
from __future__ import annotations

import argparse
import sys

from core import db
from leads import api

# Scopes the rest of the system checks for. Listed here so a typo becomes an
# error at mint time rather than a 403 nobody can explain a week later.
KNOWN_SCOPES = (
    "leads:read",      # GET /api/v1/leads, lead detail, transcripts of lead sessions
    "leads:write",     # PATCH a lead's business status or owner
    "portals:read",    # read a portal's field schema
    "portals:write",   # add or amend a field without a code release
    "chat:proxy",      # relay a conversation on a visitor's behalf (the voice hub)
    "*",               # everything; use for local development only
)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m leads.mintkey")
    parser.add_argument("--label", help="who this key is for, e.g. 'voice-hub'")
    parser.add_argument("--scopes", nargs="+", default=["leads:read"],
                        help=f"any of: {', '.join(KNOWN_SCOPES)}")
    parser.add_argument("--portal", default=None,
                        help="bind the key to one portal (default: all portals)")
    parser.add_argument("--list", action="store_true", help="list existing keys")
    parser.add_argument("--revoke", metavar="PREFIX",
                        help="revoke the key with this prefix")
    args = parser.parse_args()

    db.migrate()

    if args.list:
        rows = db.query("SELECT key_prefix, label, scopes, portal_key, created_at, "
                        "last_used_at, revoked_at FROM api_keys ORDER BY id")
        if not rows:
            print("No API keys. Mint one with --label and --scopes.")
            return 0
        width = max(len(r["key_prefix"]) for r in rows) + 2
        print(f"{'PREFIX':<{width}}{'LABEL':<18}{'SCOPES':<40}{'LAST USED':<18}STATE")
        for row in rows:
            scopes = ",".join(db.loads(row["scopes"], []))
            used = (row["last_used_at"] or "never")[:16].replace("T", " ")
            state = "revoked" if row["revoked_at"] else "active"
            print(f"{row['key_prefix']:<{width}}{row['label'][:16]:<18}"
                  f"{scopes[:38]:<40}{used:<18}{state}")
        return 0

    if args.revoke:
        with db.tx() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE key_prefix = ? "
                "AND revoked_at IS NULL", (db.now(), args.revoke))
        if not cur.rowcount:
            raise SystemExit(f"No active key with prefix {args.revoke}.")
        db.audit("cli", "api_key.revoked", entity="api_key", entity_id=args.revoke)
        print(f"Revoked {args.revoke}.")
        return 0

    if not args.label:
        parser.error("--label is required when minting (or pass --list / --revoke)")

    unknown = [s for s in args.scopes if s not in KNOWN_SCOPES]
    if unknown:
        raise SystemExit(
            f"Unknown scope(s): {unknown}. Nothing checks for those, so a key "
            f"carrying them would be silently useless. Known: {list(KNOWN_SCOPES)}")

    created = api.create_key(label=args.label, portal_key=args.portal,
                             scopes=args.scopes, actor="cli")
    print(f"\n  {created['api_key']}\n")
    print(f"  label   : {args.label}")
    print(f"  scopes  : {', '.join(args.scopes)}")
    print(f"  portal  : {args.portal or 'all portals'}")
    print("\n  Copy it now — only its hash is stored, so it cannot be shown again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
