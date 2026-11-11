"""Retiring a self-issued bearer token before it expires.

A bearer token is valid until its `exp`, and the only way to kill a leaked
one used to be rotating JWT_SECRET, which kills every token for everyone.
This keeps a small list in storage that the auth check consults:

    tokens   -- individual tokens, by their `jti` id
    handles  -- "every token for this handle issued before this moment"

The second form is what you want after a leak when you do not know which
token got out; it also covers tokens minted before `jti` existed.

The list is read at most once every CACHE_SECONDS per process, so a
revocation takes up to that long to bite. If storage cannot be read the last
list seen keeps applying -- and with none seen yet tokens are accepted,
because failing closed would turn a storage blip into a full outage of the
one login path that is meant to work when everything else is broken.

OAuth tokens are not covered: revoke those at the identity provider.
"""
import json
import time

from joyverse.atomic import get_versioned, put_versioned, retry_on_conflict
from joyverse.config import BUCKET_NAME

REVOKED_KEY = "users/_registry/revoked.json"
CACHE_SECONDS = 60
_CACHE = {"at": 0.0, "data": None}


def _empty() -> dict:
    return {"tokens": {}, "handles": {}}


def _read() -> tuple:
    from joyverse.config import r2_client

    raw, version = get_versioned(r2_client, BUCKET_NAME, REVOKED_KEY)
    data = json.loads(raw.decode("utf-8")) if raw else _empty()
    for table in ("tokens", "handles"):
        if not isinstance(data.get(table), dict):
            data[table] = {}
    return data, version


def current() -> dict:
    now = time.monotonic()
    if _CACHE["data"] is not None and now - _CACHE["at"] < CACHE_SECONDS:
        return _CACHE["data"]
    try:
        _CACHE["data"] = _read()[0]
    except Exception:
        if _CACHE["data"] is None:
            return _empty()
    # Stamped on failure too, so an outage is not retried on every request.
    _CACHE["at"] = now
    return _CACHE["data"]


def forget():
    _CACHE["at"], _CACHE["data"] = 0.0, None


def is_revoked(payload: dict, handle: str) -> bool:
    """True when this token's id, or its handle's cut-off, retires it."""
    data = current()
    jti = payload.get("jti")
    if isinstance(jti, str) and jti in data["tokens"]:
        return True
    cutoff = data["handles"].get(handle)
    if cutoff is not None:
        issued = payload.get("iat")
        # A token that does not say when it was issued cannot show it is
        # newer than the cut-off.
        if not isinstance(issued, (int, float)) or issued < cutoff:
            return True
    return False


def _prune(data: dict):
    now = int(time.time())
    data["tokens"] = {jti: exp for jti, exp in data["tokens"].items()
                      if not isinstance(exp, (int, float)) or exp > now}


@retry_on_conflict
def revoke_token(jti: str, expires_at: int = None) -> dict:
    """Retire one token. `expires_at` lets the entry be dropped afterwards."""
    from joyverse.config import r2_client

    if not isinstance(jti, str) or not jti.strip():
        raise ValueError("jti is required.")
    data, version = _read()
    _prune(data)
    data["tokens"][jti.strip()] = expires_at
    put_versioned(r2_client, BUCKET_NAME, REVOKED_KEY,
                  json.dumps(data, indent=2).encode("utf-8"), version,
                  ContentType="application/json")
    forget()
    return data


@retry_on_conflict
def revoke_handle(handle: str, before: int = None) -> dict:
    """Retire every token for `handle` issued before `before` (default now)."""
    from joyverse.config import r2_client
    from joyverse.identity import normalise_handle

    handle = normalise_handle(handle)
    data, version = _read()
    _prune(data)
    data["handles"][handle] = int(before if before is not None
                                  else time.time())
    put_versioned(r2_client, BUCKET_NAME, REVOKED_KEY,
                  json.dumps(data, indent=2).encode("utf-8"), version,
                  ContentType="application/json")
    forget()
    return data


@retry_on_conflict
def restore_handle(handle: str) -> dict:
    """Lift a handle's cut-off, making its older tokens valid again."""
    from joyverse.config import r2_client
    from joyverse.identity import normalise_handle

    handle = normalise_handle(handle)
    data, version = _read()
    data["handles"].pop(handle, None)
    put_versioned(r2_client, BUCKET_NAME, REVOKED_KEY,
                  json.dumps(data, indent=2).encode("utf-8"), version,
                  ContentType="application/json")
    forget()
    return data
