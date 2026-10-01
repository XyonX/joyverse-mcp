from fastapi import Request, HTTPException
from typing import Callable, Dict, List, Optional, Sequence

from mcppro.scopes import missing_scopes, parse_scopes

BEARER_PREFIX = "Bearer "

def no_auth(request: Request) -> Dict:
    """
    Strategy 1: No authentication.
    Always allows access. Returns empty user context.
    """
    return {}

def api_key_auth(valid_keys: list) -> Callable:
    """
    Strategy 2: API Key authentication.
    Checks X-API-Key header or Authorization: Bearer header.
    """
    def auth_dependency(request: Request) -> Dict:
        # Check X-API-Key header first
        api_key = request.headers.get("X-API-Key")
        
        # Fall back to Authorization: Bearer header
        if not api_key:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                api_key = auth_header[7:]
        
        # Validate
        if not api_key or api_key not in valid_keys:
            raise HTTPException(status_code=401, detail="Invalid API Key")
            
        # Return user context (usable for tool-level permissions later)
        return {"api_key": api_key, "role": "user"}
    
    return auth_dependency


def extract_bearer_token(request: Request) -> str:
    """Pull the token out of an `Authorization: Bearer <token>` header.

    Shares one implementation with api_key_auth so the accepted syntax is
    identical across strategies: the scheme is matched case-sensitively and
    only when followed by a space, so "Bearerk" and a bare "Bearer" are
    rejected rather than treated as a token of "".
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith(BEARER_PREFIX):
        raise HTTPException(status_code=401, detail="Missing authentication token")
    token = header[len(BEARER_PREFIX):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing authentication token")
    return token


def default_jwk_client(issuer: str, timeout: float = 30.0):
    """Build a cached JWKS client from an issuer's OIDC discovery document.

    The URL is derived from the issuer rather than configured separately, so
    issuer and keys cannot drift out of sync -- a JWKS fetched from one
    provider cannot verify tokens claiming another.
    """
    import jwt

    discovery_url = issuer.rstrip("/") + "/.well-known/openid-configuration"

    class _DiscoveryJWKClient(jwt.PyJWKClient):
        """Resolves the real JWKS URI from discovery on first use."""

        _resolved_uri = None

        def __init__(self):
            super().__init__(discovery_url, cache_jwk_set=True, lifespan=300,
                             timeout=timeout)

        @property
        def uri(self) -> str:
            if self._resolved_uri is None:
                import json
                from urllib.request import urlopen

                with urlopen(discovery_url, timeout=timeout) as resp:
                    self._resolved_uri = json.loads(
                        resp.read().decode("utf-8")
                    )["jwks_uri"]
            return self._resolved_uri

    return _DiscoveryJWKClient()


def _describe_jwt_error(exc: Exception) -> str:
    """Map a PyJWT failure to a safe, specific 401 detail."""
    import jwt

    if isinstance(exc, jwt.ExpiredSignatureError):
        return "Token expired"
    if isinstance(exc, jwt.InvalidAudienceError):
        # Token was minted for another service -- exactly the token-confusion
        # case audience validation exists to catch.
        return "Invalid token: wrong audience"
    if isinstance(exc, jwt.InvalidIssuerError):
        return "Invalid token: wrong issuer"
    if isinstance(exc, jwt.MissingRequiredClaimError):
        return f"Invalid token: missing {exc.claim} claim"
    if isinstance(exc, jwt.InvalidTokenError):
        return "Invalid token"
    return "Invalid access token"


def oauth_bearer_auth(
    *,
    issuer: str,
    audience: str,
    algorithms: Sequence[str] = ("RS256",),
    jwk_client=None,
    required_scopes: Sequence[str] = (),
    leeway: int = 30,
) -> Callable:
    """Strategy 3: OAuth 2.1 bearer validation (RFC 9728 resource server).

    Verifies the signature against the issuer's published JWKS, then checks
    `iss`, `aud` and `exp`. Audience validation is not optional: without it a
    token minted for a *different* service would be accepted here, which the
    MCP spec calls out as a security-boundary failure.

    Returns a provider-agnostic context:

        {"subject", "issuer", "scopes", "claims", "auth_method"}

    There is deliberately no `user_id` here. The framework cannot know how the
    application stores data, so it returns the issuer's opaque `sub` and lets
    the application decide what identity means. That mapping is the one place
    where two different clients must converge on the same account.

    `jwk_client` is injectable so tests never touch the network.
    """
    import jwt

    if not issuer:
        raise ValueError("oauth_bearer_auth requires an issuer")
    if not audience:
        raise ValueError("oauth_bearer_auth requires an audience")
    if "none" in algorithms:
        # An unsigned token would defeat the point of the strategy entirely.
        raise ValueError("algorithm 'none' is not permitted")

    client = jwk_client if jwk_client is not None else default_jwk_client(issuer)

    def auth_dependency(request: Request) -> Dict:
        token = extract_bearer_token(request)

        try:
            signing_key = client.get_signing_key_from_jwt(token)
        except Exception:
            # Deliberately vague: a caller must not learn whether the key was
            # unknown, the signature bad, or the JWKS unreachable.
            raise HTTPException(status_code=401, detail="Invalid access token")

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(algorithms),
                audience=audience,
                issuer=issuer,
                leeway=leeway,
                options={"require": ["exp", "iss", "sub"]},
            )
        except Exception as e:
            raise HTTPException(status_code=401, detail=_describe_jwt_error(e))

        subject = claims.get("sub")
        if not subject or not isinstance(subject, str):
            raise HTTPException(status_code=401,
                                detail="Invalid token: missing subject")

        context = {
            "subject": subject,
            "issuer": claims.get("iss"),
            "scopes": parse_scopes(claims.get("scope")),
            "claims": claims,
            "auth_method": "oauth",
        }

        # Fail closed: a token with no scopes cannot satisfy a requirement.
        # Checked here so an under-scoped caller never reaches a tool.
        if required_scopes:
            missing = missing_scopes(context["scopes"], required_scopes)
            if missing:
                raise HTTPException(
                    status_code=403,
                    detail="Insufficient scope. Missing: " + ", ".join(missing),
                )

        return context

    return auth_dependency


def any_auth(*strategies: Callable) -> Callable:
    """Try each auth strategy in order; the first to succeed wins.

    For running OAuth and legacy bearer tokens side by side. Order matters:
    put OAuth first so a genuine OAuth token is never mistaken for a
    self-issued one.

    If every strategy fails, the *first* strategy's error is raised. Surfacing
    the last one would mean a user with a perfectly valid OAuth token that has
    simply expired is told their API key is invalid.
    """
    if not strategies:
        raise ValueError("any_auth requires at least one strategy")

    def auth_dependency(request: Request) -> Dict:
        first_error: Optional[HTTPException] = None

        for index, strategy in enumerate(strategies):
            try:
                context = strategy(request)
            except HTTPException as e:
                # 403 means "authenticated but not permitted". Falling through
                # to the next strategy could only weaken that, so surface it.
                if e.status_code == 403:
                    raise
                if first_error is None:
                    first_error = e
                continue
            # Tag which strategy authenticated the caller so the app layer can
            # branch on it without re-inspecting the token.
            context.setdefault("auth_method", f"strategy_{index}")
            return context

        raise first_error or HTTPException(status_code=401, detail="Unauthorized")

    return auth_dependency