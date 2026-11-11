#!/usr/bin/env python3
"""Make a bearer-token account and an OAuth login the same account.

A bearer token resolves a *handle* to a user_id; an OAuth login resolves an
(issuer, sub) pair to one. Nothing ties the two together on its own, so data
written with a token is invisible after signing in from Claude or ChatGPT.
This script attaches the OAuth identity to the handle's account.

See what exists:
    python scripts/link_account.py --list

Link an OAuth identity to a handle:
    python scripts/link_account.py --handle joydip --sub "auth0|abc123"

If that identity has already signed in, it owns an account of its own and the
link is refused. Pass --repoint to move the login onto the handle's account;
the other account's data is left untouched under its own user_id.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from joyverse import config as jv_config  # noqa: E402
from joyverse import identity  # noqa: E402


def show_registry() -> int:
    registry = identity._load()
    print("HANDLES (bearer tokens)")
    for handle, user_id in sorted(registry["handles"].items()):
        print(f"  {handle:<32} -> {user_id}")
    if not registry["handles"]:
        print("  (none)")

    print("\nOAUTH IDENTITIES  (issuer|sub)")
    for key, user_id in sorted(registry["identities"].items()):
        print(f"  {key:<60} -> {user_id}")
    if not registry["identities"]:
        print("  (none -- sign in once through an MCP client to create one)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--list", action="store_true",
                        help="show every handle and OAuth identity")
    parser.add_argument("--handle", help="the bearer-token handle to keep")
    parser.add_argument("--sub", help="the OAuth subject, e.g. auth0|abc123")
    parser.add_argument("--issuer",
                        help="OAuth issuer (default: derived from AUTH0_DOMAIN)")
    parser.add_argument("--repoint", action="store_true",
                        help="move an identity that already has its own account")
    args = parser.parse_args()

    if args.list:
        return show_registry()

    if not args.handle or not args.sub:
        parser.error("pass --list, or both --handle and --sub")

    user_id = identity.lookup_handle(args.handle)
    if not user_id:
        print(f"Error: no account is registered for handle '{args.handle}'. "
              "Run with --list to see the handles that exist.",
              file=sys.stderr)
        return 1

    try:
        issuer = args.issuer or jv_config.issuer_url()
    except RuntimeError as e:
        print(f"Error: {e} Or pass --issuer.", file=sys.stderr)
        return 1

    try:
        identity.link_identity(user_id, issuer, args.sub,
                               repoint=args.repoint)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Pass --repoint to move this login onto the handle's account. "
              "The other account's data stays under its own user_id and "
              "stops being reachable through this login.", file=sys.stderr)
        return 1

    print(f"Linked {issuer}|{args.sub}")
    print(f"    -> {user_id}  (handle '{args.handle}')")
    print("A running server picks this up within "
          f"{identity.LOOKUP_TTL_SECONDS} seconds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
