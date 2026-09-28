#!/usr/bin/env python3
"""
Generate a JWT access token for the joyverse-mcp server.

Run it:
    python scripts/generate_token.py

Then give the printed token to your client as:
    Authorization: Bearer <token>
"""

# ============================================================
# EDIT THESE FIELDS
# ============================================================

USERNAME = "joydip"              # -> becomes the R2 key "users/<USERNAME>/..."
EXPIRES_IN_HOURS = 24 * 7         # 168 = 7 days. Use 1 for short-lived tokens.
ALGORITHM = "HS256"               # must match the server in joyverse/auth.py

# Leave the secret blank to read JWT_SECRET from your .env file.
# Set it only if you deliberately want to override the .env value.
JWT_SECRET_OVERRIDE = ""


# ============================================================
# DO NOT EDIT BELOW THIS LINE
# ============================================================

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make sure the repo root is importable no matter where you run from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# joyverse/auth.py does NOT call load_dotenv() itself -- it only sees your real
# secret because run.py imports joyverse.config (which does) first. We must do
# the same here, or we would silently sign tokens with the fallback secret and
# the server would reject every one of them.
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

# Match the server's fallback exactly.
DEFAULT_SECRET = "dev-secret-change-in-production"

if JWT_SECRET_OVERRIDE:
    secret = JWT_SECRET_OVERRIDE
    source = "JWT_SECRET_OVERRIDE at the top of this script"
else:
    secret = os.getenv("JWT_SECRET", DEFAULT_SECRET)
    source = f"JWT_SECRET in {ENV_PATH.name}" if os.getenv("JWT_SECRET") else "the built-in fallback"

import jwt


def main() -> int:
    if not USERNAME.strip():
        print("Error: USERNAME is empty. Edit the top of this script.", file=sys.stderr)
        return 1

    # Mirror the server's traversal guard so bad usernames fail here, not in prod.
    if "/" in USERNAME or ".." in USERNAME or USERNAME.strip() == "":
        print(
            "Error: USERNAME contains '/' or '..'.\n"
            "The server rejects these to prevent R2 key path traversal.",
            file=sys.stderr,
        )
        return 1

    now = int(time.time())
    expires_at = now + (EXPIRES_IN_HOURS * 3600)

    payload = {
        "username": USERNAME,
        "iat": now,
        "exp": expires_at,
    }

    token = jwt.encode(payload, secret, algorithm=ALGORITHM)

    print("=" * 70)
    print("TOKEN GENERATED")
    print("=" * 70)
    print(f"Username  : {USERNAME}")
    print(f"R2 key    : users/{USERNAME}/profile.md")
    print(f"Expires   : {datetime.fromtimestamp(expires_at, timezone.utc)} UTC")
    print(f"          (in {EXPIRES_IN_HOURS} hours)")
    print(f"Secret    : {source}")
    print()
    print("--- TOKEN ---")
    print(token)
    print("--- END TOKEN ---")
    print()
    print("Send it like this:")
    print(f'  curl -X POST http://127.0.0.1:8001/mcp \\')
    print(f'    -H "Authorization: Bearer {token}" \\')
    print('    -H "Content-Type: application/json" \\')
    print('    -d \'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\'')

    if secret == DEFAULT_SECRET:
        print()
        print("=" * 70)
        print("WARNING: this token was signed with the BUILT-IN FALLBACK SECRET.")
        print("The server will only accept it if JWT_SECRET is also unset in .env.")
        print("Add a real JWT_SECRET to .env and regenerate.")
        print("=" * 70)

    if EXPIRES_IN_HOURS > 24 * 30:
        print()
        print("Note: lifetime is over 30 days. Prefer short-lived tokens.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
