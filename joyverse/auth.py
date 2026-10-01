from fastapi import Request, HTTPException
from typing import Dict
import jwt
import os
from dotenv import load_dotenv

from joyverse import config as jv_config
from joyverse import identity

load_dotenv()

# Read the secret lazily instead of at import time. The old module raised
# RuntimeError here, which meant the whole package could not even be imported
# without JWT_SECRET -- so OAuth could never be used on its own, and tests had
# to set an env var before anything worked.
JWT_SECRET = os.getenv("JWT_SECRET")

# The full set of scopes a self-issued bearer token carries. These tokens are
# minted by us for our own operator, so they get both read and write rather
# than being artificially restricted.
_BEARER_SCOPES = [jv_config.READ_SCOPE, jv_config.WRITE_SCOPE]


def _bearer_secret() -> str:
    secret = os.getenv("JWT_SECRET") or JWT_SECRET
    if not secret:
        raise HTTPException(
            status_code=401,
            detail="Bearer auth is not configured. Set JWT_SECRET, or "
                   "authenticate with OAuth.")
    return secret


# ==========================================
# STRATEGY 1 -- OAuth (Auth0 or any OIDC provider)
# ==========================================

def oauth_auth(request: Request) -> Dict:
    """Validate an OAuth 2.1 access token and resolve it to a user_id.

    The framework (mcppro) does the cryptographic work: signature against the
    provider's JWKS, plus iss/aud/exp. This function does the one thing only
    the application can do -- decide what (issuer, sub) *means* here, which is
    the identity registry.

    Note there is no `client_id` anywhere in this lookup. That omission is
    deliberate and load-bearing: identity comes from the person, so Claude,
    Cursor and ChatGPT all resolve to the same profile.
    """
    from mcppro.auth import oauth_bearer_auth

    # Built per request rather than cached at import: configuration comes from
    # the environment, and building it is cheap (no network happens until a
    # token is actually verified).
    dependency = oauth_bearer_auth(
        issuer=jv_config.issuer_url(),
        audience=jv_config.AUTH0_AUDIENCE,
        algorithms=("RS256",),
    )

    ctx = dependency(request)
    claims = ctx.get("claims", {})

    try:
        user_id = identity.resolve_identity(
            issuer=ctx["issuer"],
            subject=ctx["subject"],
            email=claims.get("email"),
            # Absent claim must mean False, never True: an unstated
            # verification status cannot be assumed to be verified.
            email_verified=bool(claims.get("email_verified") is True),
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    return {
        "user_id": user_id,
        "scopes": ctx.get("scopes", []),
        "auth_method": "oauth",
        "issuer": ctx["issuer"],
        "subject": ctx["subject"],
        # Kept so a human-facing tool can say who is calling without the app
        # having to re-read the token. Never used as a storage key.
        "email": claims.get("email"),
    }


# ==========================================
# STRATEGY 2 -- self-issued bearer token
# ==========================================

def jwt_auth(request: Request) -> Dict:
    """Validate a self-issued HS256 bearer token.

    The token carries a handle; the handle is resolved to a user_id here so
    that downstream code only ever deals in user_ids.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authentication token")

    token = auth_header[len("Bearer "):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing authentication token")

    try:
        payload = jwt.decode(token, _bearer_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Accept the historical claim name so tokens minted before the rename keep
    # working, but prefer the new one.
    handle = payload.get("handle") or payload.get("username")
    if not handle:
        raise HTTPException(status_code=401,
                            detail="Invalid token: missing handle")

    try:
        user_id = identity.resolve_handle(handle)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Invalid handle: {e}")

    # Self-issued tokens carry both scopes. An explicit claim is honoured if
    # present so a token can be narrowed later, but the default is full access
    # -- these tokens are minted for our own operator.
    scopes = _BEARER_SCOPES
    if payload.get("scope"):
        from mcppro.scopes import parse_scopes

        scopes = parse_scopes(payload["scope"])

    return {
        "user_id": user_id,
        "handle": identity.normalise_handle(handle),
        "scopes": scopes,
        "auth_method": "bearer",
    }