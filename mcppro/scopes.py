"""Scope parsing and checking for OAuth-protected MCP tools.

Deliberately free of FastAPI and JWT imports so the logic can be tested and
reused on its own. Every function here is pure.
"""
from typing import Dict, Iterable, List, Sequence

from fastapi import HTTPException

# RFC 6749 s3.3: scope is a space-delimited, case-sensitive string.
SCOPE_DELIMITER = " "


def parse_scopes(value) -> List[str]:
    """Normalise a scope claim into a list of individual scopes.

    Accepts the raw space-delimited string from a token, or an already-parsed
    list. Blank entries are dropped, and duplicates removed while preserving
    order, so a token claiming "read read write" yields ["read", "write"].
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts: Iterable[str] = value.split(SCOPE_DELIMITER)
    else:
        parts = value

    seen = set()
    out: List[str] = []
    for part in parts:
        if not isinstance(part, str):
            continue
        scope = part.strip()
        # A claim like "a  b" (double space) or a stray tab must not become a
        # scope named "" -- that would satisfy a required "" and hide a bug.
        if not scope or scope in seen:
            continue
        seen.add(scope)
        out.append(scope)
    return out


def has_scopes(granted: Sequence[str], required: Sequence[str]) -> bool:
    """True when every required scope is present in the granted set."""
    return not missing_scopes(granted, required)


def missing_scopes(granted: Sequence[str], required: Sequence[str]) -> List[str]:
    """Required scopes that were not granted, in the order they were required."""
    held = set(granted or ())
    return [scope for scope in (required or ()) if scope not in held]


def user_scopes(user_context: Dict) -> List[str]:
    """Read the scopes off an auth context produced by this framework."""
    if not user_context:
        return []
    return parse_scopes(user_context.get("scopes"))


def require_scopes(user_context: Dict, required: Sequence[str]) -> None:
    """Raise 403 unless the context carries every required scope.

    Fails closed: a caller with no scopes at all is rejected rather than
    treated as unrestricted, so a misconfigured context cannot silently grant
    full access.
    """
    if not required:
        return

    missing = missing_scopes(user_scopes(user_context), required)
    if missing:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Insufficient scope. Required: {', '.join(required)}. "
                f"Missing: {', '.join(missing)}"
            ),
            headers={
                # RFC 6750 s3: tell the client which scopes would help.
                "WWW-Authenticate": (
                    'Bearer error="insufficient_scope", '
                    f'scope="{SCOPE_DELIMITER.join(required)}"'
                ),
            },
        )