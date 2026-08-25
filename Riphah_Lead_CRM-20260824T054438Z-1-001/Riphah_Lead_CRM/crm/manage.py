"""Staff administration from the command line.

    python -m crm.manage list
    python -m crm.manage create-staff sales1@riphah.local --name "Ayesha" --role agent
    python -m crm.manage create-staff boss@riphah.local --role manager
    python -m crm.manage passwd sales1@riphah.local
    python -m crm.manage disable sales1@riphah.local
    python -m crm.manage enable sales1@riphah.local
    python -m crm.manage role sales1@riphah.local manager

There is no self-signup in the CRM and there should not be: it reads leads with
phone numbers in them, so accounts are created *for* staff rather than *by*
anyone who finds the URL.

Passwords are prompted for rather than passed as arguments, because an argument
lands in shell history and in the process list where any other user on the box can
read it. Pass `--password` only in a scripted setup, and expect the warning.
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import string
import sys

from core import db, security
from crm import auth


def _prompt_password(email: str) -> str:
    first = getpass.getpass(f"Password for {email}: ")
    second = getpass.getpass("Confirm: ")
    if first != second:
        raise SystemExit("Passwords did not match.")
    return first


def _suggest() -> str:
    """A readable random password, for when someone wants one generated."""
    alphabet = string.ascii_letters + string.digits
    return "-".join(
        "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)
    )


def cmd_list(args: argparse.Namespace) -> int:
    rows = auth.listing()
    if not rows:
        print("No staff accounts. Create one: python -m crm.manage create-staff <email>")
        return 0
    width = max(len(r["email"]) for r in rows) + 2
    print(f"{'EMAIL':<{width}}{'ROLE':<10}{'NAME':<22}{'LAST LOGIN':<18}STATE")
    for row in rows:
        state = "disabled" if row["disabled_at"] else "active"
        # Trimmed to the minute: the full ISO timestamp is 25 chars and overruns
        # the column, which is the kind of thing that makes a CLI look unfinished.
        seen = (row["last_login_at"] or "never")[:16].replace("T", " ")
        print(f"{row['email']:<{width}}{row['role']:<10}"
              f"{(row['name'] or '—'):<22}{seen:<18}{state}")
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    if args.password:
        print("! --password was passed on the command line; it is now in your shell "
              "history. Rotate it after first login.", file=sys.stderr)
        password = args.password
    elif args.generate:
        password = _suggest()
    else:
        password = _prompt_password(args.email)

    try:
        staff = auth.create_staff(email=args.email, password=password,
                                 name=args.name, role=args.role, actor="cli")
    except auth.AuthError as exc:
        raise SystemExit(f"Could not create the account: {exc}") from exc

    print(f"Created {staff['email']} as {staff['role']}.")
    if args.generate:
        # Printed once. Nothing stores the plaintext, so this is the only chance.
        print(f"  password: {password}")
        print("  Give it to them over a channel you trust, and have them change it.")
    print(f"\nThey sign in at http://127.0.0.1:{__import__('config').PORT}")
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    row = db.one("SELECT id, email FROM staff WHERE email = ?", (args.email,))
    if not row:
        raise SystemExit(f"No account for {args.email}.")
    password = _suggest() if args.generate else _prompt_password(args.email)
    try:
        password_hash = security.hash_password(password)
    except security.WeakPassword as exc:
        raise SystemExit(str(exc)) from exc

    with db.tx() as conn:
        conn.execute("UPDATE staff SET password_hash = ? WHERE id = ?",
                     (password_hash, row["id"]))
        # Every existing session is revoked. A password reset is usually a
        # response to suspecting one is compromised.
        conn.execute("UPDATE staff_sessions SET revoked_at = ? WHERE staff_id = ?",
                     (db.now(), row["id"]))
    db.log_activity(None, "cli", "staff_password_reset", {"email": row["email"]})
    print(f"Password changed for {row['email']}. All their sessions were signed out.")
    if args.generate:
        print(f"  password: {password}")
    return 0


# Prefixes used by the seeder and the test suites. Anything matching these is
# fixture data and safe to remove; a real chatbot lead is `LD-<year>-<seq>`.
TEST_PREFIXES = ("DEMO-", "SMOKE-", "PROBE-", "manual-")


def cmd_clear_leads(args: argparse.Namespace) -> int:
    """Remove lead data. Test fixtures by default, everything with --all.

    Notes cascade from `leads`, and so do `lead_fields`, `activity` and
    `transcripts`, so one DELETE is enough — but `sources.leads_received` is a
    running counter and has to be reset by hand or the dashboard keeps reporting
    leads that no longer exist.
    """
    if args.all:
        target, params = "1 = 1", []
        label = "ALL leads"
    else:
        target = " OR ".join("external_id LIKE ?" for _ in TEST_PREFIXES)
        params = [f"{p}%" for p in TEST_PREFIXES]
        label = "test fixtures (" + ", ".join(TEST_PREFIXES) + ")"

    count = db.scalar(f"SELECT COUNT(*) FROM leads WHERE {target}", params) or 0
    if not count:
        print(f"Nothing to remove — no {label}.")
        return 0

    if not args.yes:
        print(f"About to delete {count} leads: {label}.")
        if input("Type 'yes' to confirm: ").strip().lower() != "yes":
            print("Cancelled.")
            return 1

    with db.tx() as conn:
        conn.execute(f"DELETE FROM leads WHERE {target}", params)
        if args.all:
            # Inbound webhook records reference lead ids that no longer exist.
            # Kept, but unlinked, so the delivery history stays auditable.
            conn.execute("UPDATE inbound_webhooks SET lead_id = NULL")
        # Recount rather than decrement: the counter and the table can only be
        # trusted to agree if it is derived.
        for row in conn.execute("SELECT key FROM sources").fetchall():
            actual = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE source_key = ?", (row["key"],)
            ).fetchone()[0]
            conn.execute("UPDATE sources SET leads_received = ? WHERE key = ?",
                         (actual, row["key"]))

    db.log_activity(None, "cli", "leads_cleared",
                    {"scope": "all" if args.all else "test", "removed": count})
    remaining = db.scalar("SELECT COUNT(*) FROM leads")
    print(f"Removed {count}. {remaining} leads remain.")
    return 0


def cmd_role(args: argparse.Namespace) -> int:
    if args.new_role not in auth.ROLES:
        raise SystemExit(f"Role must be one of {auth.ROLES}.")
    with db.tx() as conn:
        cur = conn.execute("UPDATE staff SET role = ? WHERE email = ?",
                           (args.new_role, args.email))
    if not cur.rowcount:
        raise SystemExit(f"No account for {args.email}.")
    db.log_activity(None, "cli", "staff_role_changed",
                    {"email": args.email, "role": args.new_role})
    print(f"{args.email} is now {args.new_role}.")
    return 0


def _set_disabled(email: str, disabled: bool) -> int:
    with db.tx() as conn:
        cur = conn.execute("UPDATE staff SET disabled_at = ? WHERE email = ?",
                           (db.now() if disabled else None, email))
        if disabled:
            # Disabling has to end their live sessions too, or the account stays
            # usable until the cookie expires — which is the whole point of
            # disabling it.
            conn.execute(
                "UPDATE staff_sessions SET revoked_at = ? WHERE staff_id = "
                "(SELECT id FROM staff WHERE email = ?)", (db.now(), email))
    if not cur.rowcount:
        raise SystemExit(f"No account for {email}.")
    db.log_activity(None, "cli",
                    "staff_disabled" if disabled else "staff_enabled",
                    {"email": email})
    print(f"{email} {'disabled and signed out' if disabled else 'enabled'}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m crm.manage",
        description="Staff administration for the Riphah Lead CRM")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list staff accounts")

    create = sub.add_parser("create-staff", help="create a staff account")
    create.add_argument("email")
    create.add_argument("--name")
    create.add_argument("--role", default="agent", choices=auth.ROLES)
    create.add_argument("--password", help="avoid: lands in shell history")
    create.add_argument("--generate", action="store_true",
                        help="generate a password and print it once")

    passwd = sub.add_parser("passwd", help="reset a password")
    passwd.add_argument("email")
    passwd.add_argument("--generate", action="store_true")

    role = sub.add_parser("role", help="change a role")
    role.add_argument("email")
    role.add_argument("new_role", choices=auth.ROLES)

    clear = sub.add_parser("clear-leads",
                           help="remove demo/test leads (or all, with --all)")
    clear.add_argument("--all", action="store_true",
                       help="remove EVERY lead, not just test fixtures")
    clear.add_argument("--yes", action="store_true", help="skip the confirmation")

    for name, help_text in (("disable", "disable an account and sign it out"),
                            ("enable", "re-enable an account")):
        node = sub.add_parser(name, help=help_text)
        node.add_argument("email")

    args = parser.parse_args()
    db.migrate()

    return {
        "list": cmd_list,
        "create-staff": cmd_create,
        "passwd": cmd_passwd,
        "role": cmd_role,
        "clear-leads": cmd_clear_leads,
        "disable": lambda a: _set_disabled(a.email, True),
        "enable": lambda a: _set_disabled(a.email, False),
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
