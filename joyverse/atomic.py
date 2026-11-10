"""Optimistic concurrency for read-modify-write on R2.

Object storage has no "append" or "update in place": every change is a read,
a modification in memory, and a write of the whole object. Without a guard,
two callers doing that at once both read the same version and the second
write silently discards the first -- a lost log entry, a lost edit, or (in the
identity registry) a lost account mapping.

The guard is a conditional write. A read remembers the object's ETag, and the
write only succeeds if the object still has that ETag (`If-Match`), or still
does not exist (`If-None-Match: *`). A write that loses the race raises
`Conflict`, and `retry_on_conflict` reruns the whole read-modify-write against
the fresh version.
"""
import functools
import random
import time

# Version of an object that did not exist when it was read. Distinct from
# None, which means "exists, but the backend gave no ETag" -- in that case the
# write falls back to unconditional, which is what it always was.
MISSING = object()

MAX_ATTEMPTS = 6


class Conflict(Exception):
    """The object changed between the read and the write."""

    def __init__(self, key: str):
        self.key = key
        super().__init__(
            f"Concurrent update on {key}; the write was not applied. "
            "Try again.")


def _error_code(exc) -> tuple:
    """(error code, HTTP status) from a botocore ClientError, else ("", None)."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return "", None
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code, status


def is_not_found(exc) -> bool:
    """True when an R2 error means "no such object" rather than a failure."""
    code, status = _error_code(exc)
    if code in ("NoSuchKey", "NotFound", "404") or status == 404:
        return True
    text = str(exc)
    return "NoSuchKey" in text or "does not exist" in text


def is_conflict(exc) -> bool:
    """True when a conditional write lost the race."""
    code, status = _error_code(exc)
    if code in ("PreconditionFailed", "ConditionalRequestConflict"):
        return True
    if status in (409, 412):
        return True
    return "PreconditionFailed" in str(exc)


def get_versioned(client, bucket: str, key: str) -> tuple:
    """Read an object and the version to guard a later write with.

    Returns (bytes, etag) for an existing object and (None, MISSING) when
    there is none. Any other failure propagates: a transport error must never
    be read as "the object is empty".
    """
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except Exception as e:
        if is_not_found(e):
            return None, MISSING
        raise
    return response["Body"].read(), response.get("ETag")


def put_versioned(client, bucket: str, key: str, body: bytes, version=None,
                  **kwargs):
    """Write an object only if it is still the version that was read.

    `version` is what get_versioned returned. Raises Conflict when someone
    else wrote first.
    """
    from joyverse.config import CONDITIONAL_WRITES

    condition = {}
    if CONDITIONAL_WRITES:
        if version is MISSING:
            condition["IfNoneMatch"] = "*"
        elif version:
            condition["IfMatch"] = version

    try:
        client.put_object(Bucket=bucket, Key=key, Body=body, **kwargs,
                          **condition)
    except Exception as e:
        if condition and is_conflict(e):
            raise Conflict(key) from e
        raise


def retry_on_conflict(fn):
    """Rerun a read-modify-write function when its write loses a race.

    The wrapped function must do its own read on every call, so each attempt
    works from the current version. Jittered backoff keeps two racing callers
    from colliding again on the retry.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        for attempt in range(MAX_ATTEMPTS):
            try:
                return fn(*args, **kwargs)
            except Conflict:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                time.sleep(random.uniform(0, 0.02 * (2 ** attempt)))
    return wrapper
