#!/usr/bin/env python3
"""Create the throwaway Auth0 user used by scripts/live_oauth_check.py.

Uses the Auth0 Management API so it works even when the database connection
has no "Add Users" button in the dashboard (which is the case while the
connection is disabled).

Prerequisite, once, in the dashboard:
    Applications -> your app -> API Access ->
    tick the CLIENT ACCESS box for "Auth0 Management API"

That is a different column from user-delegated access. The Joyverse MCP API
should stay at 0 client-access permissions: granting them would let the app
act as a user with no login at all, which breaks one-person-one-profile.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

DOMAIN = os.getenv("AUTH0_DOMAIN", "").strip()
CLIENT_ID = os.getenv("AUTH0_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("AUTH0_CLIENT_SECRET", "").strip()
EMAIL = os.getenv("AUTH0_TEST_EMAIL", "").strip()
PASSWORD = os.getenv("AUTH0_TEST_PASSWORD", "").strip()

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")

CONNECTION = "Username-Password-Authentication"


def api(path, method="GET", body=None, token=None):
    url = f"https://{DOMAIN}/api/v2{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw[:200]}


def mgmt_token():
    """Client-credentials token for the Management API.

    The token endpoint lives at the tenant root (/oauth/token); only the
    resource calls live under /api/v2.
    """
    req = urllib.request.Request(
        f"https://{DOMAIN}/oauth/token",
        data=urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "audience": f"https://{DOMAIN}/api/v2/",
        }).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            return body.get("access_token"), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return None, json.loads(raw)
        except ValueError:
            return None, {"error": raw[:200]}


def main():
    print(f"{DIM}tenant  {DOMAIN}\nemail   {EMAIL}{RESET}\n")

    missing = [n for n, v in [("AUTH0_CLIENT_ID", CLIENT_ID),
                              ("AUTH0_CLIENT_SECRET", CLIENT_SECRET),
                              ("AUTH0_TEST_EMAIL", EMAIL),
                              ("AUTH0_TEST_PASSWORD", PASSWORD)] if not v]
    if missing:
        print(f"{RED}Missing in .env: {', '.join(missing)}{RESET}")
        return 1

    token, err = mgmt_token()
    if not token:
        print(f"{RED}Could not get a Management API token.{RESET}")
        detail = (err or {}).get("error_description") or (err or {}).get("error")
        print(f"  {DIM}{detail}{RESET}")
        print(f"\n{YELLOW}Most likely cause:{RESET} the app has no CLIENT ACCESS")
        print(f"grant for the Auth0 Management API.")
        print(f"\n  Applications -> {CLIENT_ID[:12]}... -> API Access ->")
        print(f"  tick the 'Client Access' box on the Auth0 Management API row.")
        print(f"\n{DIM}That is the CLIENT ACCESS column -- not the "
              f"user-delegated one.{RESET}")
        print(f"{DIM}Leave Joyverse MCP at 0 client-access permissions.{RESET}")
        return 1

    print(f"  {GREEN}ok{RESET}  Management API token acquired")

    # Is the connection enabled?
    status, conns = api(f"/connections?strategy=auth0&name={CONNECTION}", token=token)
    if status != 200 or not conns:
        print(f"{RED}Could not read connections (status {status}){RESET}")
        return 1

    match = next((c for c in conns if c.get("name") == CONNECTION), None)
    if not match:
        print(f"{RED}Connection {CONNECTION} not found.{RESET}")
        return 1

    if not match.get("enabled"):
        print(f"  {YELLOW}note{RESET} the connection is currently DISABLED.")
        print(f"       {DIM}User creation still works, but the password grant "
              f"will not.{RESET}")
        print(f"       {DIM}Enable it under Authentication -> Database.{RESET}")

    # Does the user already exist?
    quoted = urllib.parse.quote(EMAIL, safe="")
    status, users = api(f"/users?q=email:{quoted}&search_engine=v3", token=token)
    if status == 200 and users:
        print(f"  {GREEN}ok{RESET}  user already exists "
              f"({users[0].get('user_id')})")
        print(f"\n{DIM}Nothing to do. Run: python scripts/live_oauth_check.py{RESET}")
        return 0

    status, user = api("/users", method="POST", body={
        "email": EMAIL,
        "password": PASSWORD,
        "connection": CONNECTION,
        "email_verified": True,
    }, token=token)

    if status not in (200, 201):
        print(f"{RED}User creation failed (status {status}){RESET}")
        print(f"  {DIM}{(user or {}).get('message') or (user or {}).get('error')}{RESET}")
        if "connection" in json.dumps(user or {}).lower():
            print(f"\n{YELLOW}If the connection name is rejected, pass the "
                  f"connection id instead:{RESET}")
            print(f"  {DIM}{match.get('id')}{RESET}")
        return 1

    print(f"  {GREEN}ok{RESET}  created {EMAIL}")
    print(f"       {DIM}user_id {user.get('user_id')}{RESET}")
    print(f"\nNext: python scripts/live_oauth_check.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())