"""Stable identity for users, independent of how they authenticated.

Two authentication paths produce two different identity inputs:

    OAuth : (issuer, sub)  e.g. ("https://tenant.auth0.com/", "auth0|abc123")
    Bearer: a handle       e.g. "joydip880"

Both resolve to the SAME thing -- a server-minted `user_id` like
"u_8f3a91c7e4b2" -- and that id is the only value that ever becomes an R2
path. Handles and `sub` values are lookups, never paths.

The point: the same human signing in from Claude, Cursor and ChatGPT gets
one user_id, because identity is derived from (issuer, sub) and never from
the client. A client cannot fork somebody's profile.

Storage layout in R2:

    users/_registry/identity.json      the lookup tables
    users/u_8f3a91c7e4b2/account.json  a user's own record
    users/u_8f3a91c7e4b2/profile.md    their data

The registry lives in one object rather than one object per lookup key. That
keeps every lookup consistent with every other: there is no window where a
handle resolves but the email index does not, and no need to update several
objects "at once" when only a single-key write is actually atomic.
"""
import json
import re
import secrets
import time
from datetime import datetime, timezone

from joyverse.atomic import get_versioned, put_versioned, retry_on_conflict
from joyverse.config import r2_client, BUCKET_NAME

# user_id is always "u_" + 12 lowercase hex chars.
#
# Lowercase-only removes a whole class of bug: without it, users/Joy/ and
# users/joy/ are two directories holding one person's data. Hex keeps it
# short, URL-safe, and free of characters that need escaping.
USER_ID_PREFIX = "u_"
USER_ID_BYTES = 6                       # 48 bits of entropy
USER_ID_PATTERN = re.compile(r"^u_[0-9a-f]{12}$")

# Handles are names people type, not secrets. Kept restrictive because a
# handle is a lookup key that must never be mistakable for a real user_id or
# for the registry's own namespace.
HANDLE_PATTERN = re.compile(r"^[a-z0-9_.-]{3,32}$")
HANDLE_MIN, HANDLE_MAX = 3, 32

# Reserved inside the users/ namespace. A handle may never be one of these.
RESERVED_HANDLES = {"_registry"}

REGISTRY_KEY = "users/_registry/identity.json"


# ==========================================
# ID GENERATION
# ==========================================

def new_user_id() -> str:
    """Mint a fresh user_id. Never derived from user input."""
    return USER_ID_PREFIX + secrets.token_hex(USER_ID_BYTES)


def is_user_id(value) -> bool:
    """True when `value` has the exact shape of a generated user_id."""
    return isinstance(value, str) and bool(USER_ID_PATTERN.match(value))


# ==========================================
# HANDLE VALIDATION
# ==========================================

def normalise_handle(handle) -> str:
    """Validate and canonicalise a handle.

    Case-insensitive by design: JoyDip and joydip must be the same person.
    Two spellings resolving to two profiles is precisely the failure this
    module exists to prevent.

    Rejects the registry namespace, and anything shaped like a generated id,
    so a typed handle can neither shadow the registry nor collide with (or
    guess) another account's directory.
    """
    if not isinstance(handle, str):
        raise ValueError("Handle must be a string.")

    normalised = handle.strip().lower()

    if not normalised:
        raise ValueError("Handle cannot be empty.")

    if len(normalised) < HANDLE_MIN or len(normalised) > HANDLE_MAX:
        raise ValueError(
            f"Handle must be {HANDLE_MIN}-{HANDLE_MAX} characters, "
            f"got {len(normalised)}.")

    if not HANDLE_PATTERN.match(normalised):
        raise ValueError(
            "Handle may contain only letters, digits, underscore, dot "
            "and hyphen.")

    if normalised in RESERVED_HANDLES:
        raise ValueError(f"Handle '{normalised}' is reserved.")

    if is_user_id(normalised):
        raise ValueError("Handle cannot look like a user id.")

    return normalised


# ==========================================
# REGISTRY IO
# ==========================================

def _now() -> str:
    # UTC, because the string is labelled "Z".
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_versioned() -> tuple:
    """Read the registry and the version a later write must still match.

    A missing key is distinguished from a real R2 failure: a transport error
    must not be silently read as "no users exist yet".
    """
    raw, version = get_versioned(r2_client, BUCKET_NAME, REGISTRY_KEY)
    registry = json.loads(raw.decode("utf-8")) if raw else {}
    for table in ("users", "handles", "identities", "emails"):
        registry.setdefault(table, {})
    return registry, version


def _load() -> dict:
    """Read the registry, or return an empty one."""
    return _load_versioned()[0]


def _save(registry: dict, version=None):
    """Write the registry back.

    Every account lives in this one object, so an unguarded write here is the
    most damaging lost update in the system: two people signing in for the
    first time at once would each write a registry that lacks the other, and
    whoever lost would be given a second, empty account on their next
    request. Passing the `version` from _load_versioned makes the write fail
    instead, and the caller's retry redoes it against the fresh registry.
    """
    put_versioned(
        r2_client, BUCKET_NAME, REGISTRY_KEY,
        json.dumps(registry, indent=2).encode("utf-8"), version,
        ContentType="application/json",
    )


# ==========================================
# LOOKUP CACHE
# ==========================================
# Authentication resolves an identity on every request, and the answer never
# changes once it exists: a handle or (issuer, sub) maps to one user_id for
# good. Remembering resolved mappings means a known caller costs no R2 read.
# Only hits are cached -- an unknown identity always goes to the registry, so
# a new account is visible immediately. The TTL bounds how long a mapping
# removed by hand in R2 keeps resolving.
_LOOKUP_CACHE: dict = {}
LOOKUP_TTL_SECONDS = 60


def _cached(cache_key: str):
    hit = _LOOKUP_CACHE.get(cache_key)
    if hit and time.monotonic() - hit[0] < LOOKUP_TTL_SECONDS:
        return hit[1]
    return None


def _remember(cache_key: str, user_id: str) -> str:
    _LOOKUP_CACHE[cache_key] = (time.monotonic(), user_id)
    return user_id


def _identity_key(issuer: str, subject: str) -> str:
    """Registry lookup key for an OAuth identity.

    Composite on purpose: `sub` is only unique *within* an issuer, so keying
    on it alone would let two providers collide on one account.
    """
    return f"{issuer}|{subject}"


def account_key(user_id: str) -> str:
    return f"users/{user_id}/account.json"


def get_user(user_id: str):
    """Public record for a user_id, or None if unknown."""
    return _load()["users"].get(user_id)


def list_user_ids():
    """Every known user_id. Useful for admin and cleanup tooling."""
    return list(_load()["users"].keys())


# ==========================================
# RESOLUTION
# ==========================================

def _create_user(registry: dict, **fields) -> str:
    """Add a brand new user to the registry and return its id.

    Called only after the caller has decided no existing account applies.
    """
    user_id = new_user_id()
    # Retry on the astronomically unlikely id collision rather than silently
    # overwriting somebody's record.
    while user_id in registry["users"]:
        user_id = new_user_id()

    record = {"user_id": user_id, "created": _now(), "auth": "bearer"}
    record.update({k: v for k, v in fields.items() if v is not None})

    registry["users"][user_id] = record
    if fields.get("handle"):
        registry["handles"][fields["handle"]] = user_id
    return user_id


@retry_on_conflict
def resolve_handle(handle, allow_create: bool = True) -> str:
    """Map a handle to its user_id, registering it the first time.

    The discriminator is "is this handle registered", NOT "does a directory
    exist". Checking for a directory would mean only the very first handle
    ever minted could be reused -- which is wrong for self-service bearer
    tokens, where every new handle is a new person by definition.

    `allow_create=False` makes it read-only, for callers that must not be able
    to conjure an account.
    """
    normalised = normalise_handle(handle)
    cache_key = f"h:{normalised}"

    cached = _cached(cache_key)
    if cached:
        return cached

    registry, version = _load_versioned()
    existing = registry["handles"].get(normalised)
    if existing:
        return _remember(cache_key, existing)

    if not allow_create:
        raise KeyError(f"No account registered for handle '{normalised}'.")

    user_id = _create_user(registry, handle=normalised)
    _save(registry, version)
    return _remember(cache_key, user_id)


@retry_on_conflict
def resolve_identity(issuer: str, subject: str, email=None,
                     email_verified: bool = False) -> str:
    """Map an OAuth (issuer, sub) to a user_id, provisioning on first sight.

    This is what makes the same human resolve to one account from every
    client: the lookup key is built from the issuer and subject only.
    `client_id` is not part of it and cannot be, or connecting a second client
    would create a second profile.
    """
    if not issuer or not subject:
        raise ValueError("Both issuer and subject are required.")

    key = _identity_key(issuer, subject)
    cache_key = f"i:{key}"

    cached = _cached(cache_key)
    if cached:
        return cached

    registry, version = _load_versioned()

    # 1. This exact identity has been seen before.
    existing = registry["identities"].get(key)
    if existing:
        return _remember(cache_key, existing)

    # 2. A different identity, but a verified email we already know. This is
    #    the person who signed up with Google and later added GitHub -- they
    #    get the profile they already have, not a second empty one.
    #
    #    Gated on email_verified: an unverified address is attacker-supplied
    #    and would otherwise let anyone claim an existing account.
    if email and email_verified:
        linked = registry["emails"].get(email.strip().lower())
        if linked:
            registry["identities"][key] = linked
            record = registry["users"].setdefault(linked, {
                "user_id": linked, "created": _now(), "auth": "oauth"})
            record.setdefault("links", []).append(
                {"issuer": issuer, "sub": subject})
            _save(registry, version)
            return _remember(cache_key, linked)

    # 3. Genuinely new person.
    user_id = _create_user(registry, auth="oauth", email=None)
    registry["identities"][key] = user_id
    registry["users"][user_id]["links"] = [
        {"issuer": issuer, "sub": subject}]

    # The email index is only populated for verified addresses -- an unverified
    # one must never be able to pull an account into existence later.
    if email and email_verified:
        registry["emails"][email.strip().lower()] = user_id
        registry["users"][user_id]["email"] = email

    _save(registry, version)
    return _remember(cache_key, user_id)


def lookup_handle(handle):
    """Handle -> user_id, or None. Never creates."""
    try:
        normalised = normalise_handle(handle)
    except ValueError:
        return None
    return _load()["handles"].get(normalised)


@retry_on_conflict
def link_identity(user_id: str, issuer: str, subject: str,
                  repoint: bool = False):
    """Attach an additional OAuth identity to an existing account.

    This is how a bearer-token account and an OAuth login become one person:
    the two paths otherwise mint separate user_ids, so data written with a
    token is invisible after signing in through a client.

    An identity that already resolves to a different account is refused
    unless `repoint` is set. Repointing moves the login only -- the other
    account's data stays where it is, under its own user_id, and is no longer
    reachable through this identity.
    """
    if not is_user_id(user_id):
        raise ValueError(f"Not a valid user id: {user_id!r}")

    registry, version = _load_versioned()
    if user_id not in registry["users"]:
        raise KeyError(f"Unknown user id: {user_id}")

    key = _identity_key(issuer, subject)
    link = {"issuer": issuer, "sub": subject}
    claimed = registry["identities"].get(key)
    if claimed and claimed != user_id:
        if not repoint:
            raise ValueError(
                "That identity is already linked to a different account "
                f"({claimed}).")
        previous = registry["users"].get(claimed, {})
        previous["links"] = [l for l in previous.get("links", [])
                             if l != link]

    registry["identities"][key] = user_id
    links = registry["users"][user_id].setdefault("links", [])
    if link not in links:
        links.append(link)
    _save(registry, version)
    # The cached answer for this identity may be the account it just left.
    _LOOKUP_CACHE.pop(f"i:{key}", None)
    return user_id