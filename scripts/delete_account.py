#!/usr/bin/env python3
"""Permanently delete one account: every object it owns, and its logins.

DRY RUN BY DEFAULT. Nothing is removed unless you pass --apply AND repeat
the account's user id with --confirm. There is no undo: export first.

See what would be removed:
    python scripts/delete_account.py --handle joydip

Do it:
    python scripts/delete_account.py --handle joydip --apply --confirm u_1a2b3c4d5e6f

This is a script rather than an MCP tool on purpose: an assistant should not
be able to erase an account, whatever it is told.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from joyverse import config as jv_config  # noqa: E402
from joyverse import identity, maintenance  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete an account. Dry run unless --apply.")
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--handle", help="bearer handle of the account")
    who.add_argument("--user-id", help="the account's user id (u_...)")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete")
    parser.add_argument("--confirm", default="",
                        help="the user id again, required with --apply")
    args = parser.parse_args()

    user_id = args.user_id or identity.lookup_handle(args.handle)
    if not user_id or not identity.is_user_id(user_id):
        print("Error: no such account.", file=sys.stderr)
        return 1
    if args.apply and args.confirm != user_id:
        print(f"Error: to delete, pass --confirm {user_id}", file=sys.stderr)
        return 1

    report = maintenance.delete_account(
        jv_config.get_r2_client(), jv_config.BUCKET_NAME, user_id,
        apply=args.apply)
    print(json.dumps(report, indent=2))
    if not args.apply:
        print(f"\nDRY RUN -- nothing was deleted. To delete, add "
              f"--apply --confirm {user_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
