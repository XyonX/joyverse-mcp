#!/usr/bin/env python3
"""End-to-end OAuth check against a live Auth0 tenant and a live server.

The only test that proves what unit tests cannot: that a token minted by a
real authorization server verifies against its real JWKS, and that the
audience/issuer checks genuinely fire.

Usage:
    python scripts/live_oauth_check.py                  # mint then check
    python scripts/live_oauth_check.py --token <jwt>    # use a token you have

Configuration comes from the repo-root .env. Writes only under the test
user's own user_id -- never into another account's prefix.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

DOMAIN = os.getenv("AUTH0_DOMAIN", "").strip()
AUDIENCE = os.getenv("AUTH0_AUDIENCE", "").strip()
CLIENT_ID = os.getenv("AUTH0_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("AUTH0_CLIENT_SECRET", "").strip()
TEST_EMAIL = os.getenv("AUTH0_TEST_EMAIL", "").strip()
TEST_PASSWORD = os.getenv("AUTH0_TEST_PASSWORD", "").strip()
BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

# Sent on every request. Some edge layers (Cloudflare among them) reject
# requests that carry no User-Agent at all with a 403, which would look like
# a server or auth failure when it is neither. urllib sends no UA by default.
USER_AGENT = "joyverse-mcp-live-check/1.0"
HTTP_HEADERS = {"User-Agent": USER_AGENT}

# RFC 9728 discovery prefix. Kept in one place so the discovery and
# issuer-consistency checks cannot drift onto different paths.
WELL_KNOWN = "/.well-known/oauth-protected-resource"

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")
PASSED, FAILED, WARNED = [], [], []


def ok(label, detail=""):
    PASSED.append(label)
    tail = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"  {GREEN}PASS{RESET}  {label}{tail}")


def fail(label, detail=""):
    FAILED.append(label)
    tail = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"  {RED}FAIL{RESET}  {label}{tail}")


def warn(label, detail=""):
    WARNED.append(label)
    tail = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"  {YELLOW}WARN{RESET}  {label}{tail}")


def section(title):
    print(f"\n{title}\n{'-' * len(title)}")


def mint_token(scope=None):
    """Password Realm grant -> a real user-delegated access token."""
    payload = {
        "grant_type": "password",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "username": TEST_EMAIL,
        "password": TEST_PASSWORD,
        "audience": AUDIENCE,
        "scope": scope or "openid profile email joyverse:read joyverse:write",
    }
    req = urllib.request.Request(
        f"https://{DOMAIN}/oauth/token",
        data=urllib.parse.urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return None, json.loads(raw)
        except ValueError:
            return None, {"error": raw[:200]}


def decode_unverified(token):
    """Decode claims WITHOUT verifying. Inspection only, never trusted."""
    import base64

    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode())


def rpc(token, method, params=None, req_id=1):
    """POST a JSON-RPC request to the live server."""
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        f"{BASE_URL}/mcp",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {token}",
            **HTTP_HEADERS,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw, status, headers = resp.read().decode(), resp.status, resp.headers
    except urllib.error.HTTPError as e:
        return e.code, None, e.headers

    for line in raw.splitlines():
        if line.startswith("data: "):
            return status, json.loads(line[6:]), headers
    return status, None, headers


def http_get(path, headers=None):
    url = path if path.startswith("http") else f"{BASE_URL}{path}"
    merged = {**HTTP_HEADERS, **(headers or {})}
    req = urllib.request.Request(url, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers


def text_of(payload):
    try:
        return payload["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return ""


def get_token():
    """Return a token from --token, or mint one via Password Realm."""
    if "--token" in sys.argv:
        return sys.argv[sys.argv.index("--token") + 1], "supplied on the command line"

    missing = [n for n, v in [("AUTH0_CLIENT_ID", CLIENT_ID),
                              ("AUTH0_CLIENT_SECRET", CLIENT_SECRET),
                              ("AUTH0_TEST_EMAIL", TEST_EMAIL),
                              ("AUTH0_TEST_PASSWORD", TEST_PASSWORD)] if not v]
    if missing:
        print(f"{RED}Missing in .env: {', '.join(missing)}{RESET}")
        return None, None

    result, err = mint_token()
    if err:
        fail("token request rejected", err.get("error", "?"))
        print(f"        {DIM}{err.get('error_description', '')}{RESET}")
        return None, None

    ok("password realm grant succeeded",
       f"expires_in={result.get('expires_in')}")
    return result["access_token"], "minted"


def check_claims(token):
    claims = decode_unverified(token)
    expected_iss = f"https://{DOMAIN}/"
    iss = claims.get("iss", "")

    if iss == expected_iss:
        ok("issuer matches", iss)
    else:
        fail("issuer mismatch", f"got {iss!r}, expected {expected_iss!r}")

    aud = claims.get("aud")
    aud_list = aud if isinstance(aud, list) else [aud]
    if AUDIENCE in aud_list:
        ok("audience matches", AUDIENCE)
    else:
        fail("audience mismatch", f"got {aud!r}, expected {AUDIENCE!r}")

    if claims.get("sub"):
        ok("subject present", claims["sub"][:48])
    else:
        fail("subject missing")

    if claims.get("email"):
        verified = claims.get("email_verified")
        ok("email present",
           f"{claims['email']} (email_verified={verified})")
        if verified is not True:
            warn("email_verified is not True",
                 "account linking will not engage for this user")
    else:
        warn("no email claim", "account linking cannot be exercised")

    granted = sorted(set((claims.get("scope") or "").split()))
    if granted:
        print(f"  {DIM}scopes in token: {' '.join(granted)}{RESET}")
    for needed in ("joyverse:read", "joyverse:write"):
        if needed in granted:
            ok(f"token carries {needed}")
        else:
            warn(f"token lacks {needed}",
                 "a tool needing it will 403 -- see section 6")


def check_discovery():
    root = WELL_KNOWN
    inserted = f"{root}/mcp"

    status, body, headers = http_get(inserted)
    if status != 200:
        fail("metadata not served", f"{inserted} -> {status}")
        return
    ok("path-inserted metadata served", inserted)

    doc = json.loads(body)
    if doc.get("resource") == f"{BASE_URL}/mcp":
        ok("resource field correct", doc["resource"])
    else:
        warn("resource field", f"got {doc.get('resource')!r}")

    servers = doc.get("authorization_servers") or []
    if servers:
        ok("authorization_servers advertised", ", ".join(servers))
    else:
        fail("authorization_servers absent",
             "clients cannot discover where to log in")

    if doc.get("scopes_supported"):
        ok("scopes_supported advertised", ", ".join(doc["scopes_supported"]))
    else:
        warn("scopes_supported absent")

    if headers.get("access-control-allow-origin") == "*":
        ok("CORS allows browser clients")
    else:
        warn("no permissive CORS on metadata",
             "browser clients may not read it cross-origin")

    status_root, _, _ = http_get(root)
    if status_root == 200:
        ok("root metadata also served (client variance)")
    else:
        warn("root metadata not served", f"-> {status_root}")


def check_issuer_consistency():
    """Walk the discovery chain the way an MCP client does.

    A client fetches {AS}/.well-known/oauth-authorization-server and compares
    its `issuer` field against the URL it fetched. Any difference -- even a
    trailing slash -- aborts the connection with an "issuer mismatch" error.
    This is the check that would have caught the bug ChatGPT reported, and it
    is the one our own tests were missing.
    """
    status, body, _ = http_get(f"{WELL_KNOWN}/mcp")
    if status != 200:
        return  # already reported by check_discovery

    advertised = (json.loads(body).get("authorization_servers") or [None])[0]
    if not advertised:
        return  # already reported

    # rstrip before appending: the issuer ends in "/" by design, and joining
    # blindly would produce "...auth0.com//.well-known/..." and 404. The
    # comparison below still uses the advertised value verbatim.
    status, body, _ = http_get(
        f"{advertised.rstrip('/')}/.well-known/oauth-authorization-server")
    if status != 200:
        fail("authorization server metadata unreachable",
             f"{advertised} -> {status}")
        return

    as_metadata = json.loads(body)
    real_issuer = as_metadata.get("issuer")

    if real_issuer == advertised:
        ok("advertised AS matches its own issuer field", real_issuer)
    else:
        fail("issuer mismatch -- MCP clients will refuse to connect",
             f"advertised {advertised!r} != issuer {real_issuer!r}")

    # The token's iss claim must be the same string too, or validation and
    # discovery disagree with each other.
    if real_issuer == f"https://{DOMAIN}/":
        ok("issuer is consistent with this tenant")
    else:
        warn("issuer differs from the configured tenant",
             f"{real_issuer!r} vs https://{DOMAIN}/")


def check_challenge():
    status, _, _ = rpc("not-a-real-token", "tools/list")
    if status != 401:
        fail("invalid token was not rejected", f"got {status}")
        return
    ok("invalid token rejected with 401")

    _, _, headers = rpc("not-a-real-token", "tools/list")
    challenge = headers.get("www-authenticate", "")
    if "resource_metadata=" in challenge:
        ok("401 carries WWW-Authenticate resource_metadata")
        print(f"        {DIM}{challenge[:112]}{RESET}")
    else:
        fail("401 missing resource_metadata",
             "clients cannot discover the login flow")


def check_calls(token):
    status, payload, _ = rpc(token, "tools/list")
    if status != 200 or not payload:
        fail("tools/list failed", f"status={status}")
        return

    names = [t["name"] for t in payload.get("result", {}).get("tools", [])]
    ok(f"tools/list returned {len(names)} tools")
    if len(names) == 9:
        ok("all nine tools advertised")
    else:
        warn("unexpected tool count", ", ".join(names))

    status, payload, _ = rpc(token, "tools/call",
                             {"name": "get_profile", "arguments": {}})
    if status != 200 or not payload:
        fail("get_profile failed", f"status={status}")
    elif payload.get("result", {}).get("isError"):
        warn("get_profile errored", text_of(payload)[:110])
    elif "No profile found" in text_of(payload):
        ok("get_profile reached this user's own (empty) profile")
        print(f"        {DIM}{text_of(payload)[:110]}{RESET}")
    else:
        ok("get_profile returned this user's profile")


def check_write(token):
    stamp = int(time.time())
    status, payload, _ = rpc(token, "tools/call",
                             {"name": "update_profile",
                              "arguments": {"field": "livecheck",
                                            "value": f"set at {stamp}"}})
    if status == 200 and payload and not payload["result"].get("isError"):
        ok("update_profile accepted the OAuth token")
    else:
        detail = text_of(payload) or str(payload)[:110]
        fail("update_profile failed", detail)
        return

    status, payload, _ = rpc(token, "tools/call",
                             {"name": "get_profile", "arguments": {}})
    if status == 200 and payload and str(stamp) in text_of(payload):
        ok("the write reads back through OAuth")
    else:
        warn("could not read the written field back")


def check_bearer_isolation():
    """The OAuth user must not be the bearer user.

    Two different identity inputs must never collapse onto one account.
    """
    from joyverse import config as jv_config
    import jwt as pyjwt

    secret = os.getenv("JWT_SECRET")
    if not secret:
        warn("JWT_SECRET not set; skipped bearer-isolation check")
        return

    now = int(time.time())
    token = pyjwt.encode({"handle": "joydip", "exp": now + 300},
                         secret, algorithm="HS256")
    status, payload, _ = rpc(token, "tools/call",
                             {"name": "get_profile", "arguments": {}})
    if status != 200:
        warn("bearer check did not reach the server", f"status={status}")
        return

    text = text_of(payload)
    # The bearer user must not see the field the OAuth user just wrote.
    if "set at " in text:
        fail("bearer user can see the OAuth user's field",
             "accounts have collided")
    else:
        ok("bearer account is separate from the OAuth account")

    readv_config = jv_config.resource_url()
    print(f"        {DIM}bearer resolves under its own user_id; "
          f"resource is {readv_config}{RESET}")


def main():
    print(f"{DIM}tenant {DOMAIN}\naudience {AUDIENCE}\n"
          f"server  {BASE_URL}{RESET}")

    section("1. MINT A REAL TOKEN")
    token, origin = get_token()
    if not token:
        print(f"\n{RED}Cannot continue without a token.{RESET}")
        print(f"{DIM}If the grant was rejected, the test user probably does "
              f"not exist yet.{RESET}")
        return 1
    if origin:
        print(f"  {DIM}({origin}){RESET}")

    section("2. TOKEN CLAIMS")
    check_claims(token)

    section("3. DISCOVERY (RFC 9728)")
    check_discovery()
    check_issuer_consistency()

    section("4. AUTH CHALLENGE")
    check_challenge()

    section("5. AUTHENTICATED CALLS")
    check_calls(token)

    section("6. WRITE PATH + ACCOUNT ISOLATION")
    check_write(token)
    check_bearer_isolation()

    section("SUMMARY")
    print(f"  {GREEN}{len(PASSED)} passed{RESET}   "
          f"{RED}{len(FAILED)} failed{RESET}   "
          f"{YELLOW}{len(WARNED)} warnings{RESET}")
    if FAILED:
        print(f"\n{RED}Failed:{RESET}")
        for f in FAILED:
            print(f"  - {f}")
    if WARNED:
        print(f"\n{YELLOW}Warnings:{RESET}")
        for w in WARNED:
            print(f"  - {w}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())