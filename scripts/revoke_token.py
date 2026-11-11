#!/usr/bin/env python3
"""Retire a bearer token before it expires.

See what is revoked:
    python scripts/revoke_token.py --list

Retire one token (paste the token itself; it must carry a jti, which tokens
from generate_token.py now do):
    python scripts/revoke_token.py --token eyJhbGciOi...

Retire EVERY token issued so far for a handle -- use this after a leak, or
for tokens minted before jti existed. Tokens generated afterwards work:
    python scripts/revoke_token.py --handle joydip

Undo that:
    python scripts/revoke_token.py --handle joydip --restore

A running server picks the change up within a minute.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import jwt  # noqa: E402

from joyverse import revocation  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Revoke bearer tokens.")
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--list", action="store_true")
    what.add_argument("--token", help="the token to retire")
    what.add_argument("--jti", help="the id of the token to retire")
    what.add_argument("--handle", help="retire every token for this handle")
    parser.add_argument("--restore", action="store_true",
                        help="with --handle: lift the cut-off again")
    args = parser.parse_args()

    if args.list:
        print(json.dumps(revocation._read()[0], indent=2))
        return 0

    if args.handle:
        try:
            if args.restore:
                revocation.restore_handle(args.handle)
                print(f"Tokens for '{args.handle}' are no longer cut off.")
            else:
                revocation.revoke_handle(args.handle)
                print(f"Every token issued for '{args.handle}' before now is "
                      "revoked. Generate a new one with generate_token.py.")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0

    jti, expires = args.jti, None
    if args.token:
        # Read without verifying: revoking needs only the id, and a token
        # signed with an old secret should still be revocable.
        try:
            claims = jwt.decode(args.token.strip(),
                                options={"verify_signature": False})
        except jwt.InvalidTokenError as e:
            print(f"Error: not a readable token: {e}", file=sys.stderr)
            return 1
        jti, expires = claims.get("jti"), claims.get("exp")
        if not jti:
            handle = claims.get("handle") or claims.get("username")
            print("Error: this token has no jti, so it cannot be revoked on "
                  f"its own. Use --handle {handle or '<handle>'} to retire "
                  "every token issued for that handle.", file=sys.stderr)
            return 1
    revocation.revoke_token(jti, expires)
    print(f"Token {jti} is revoked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
