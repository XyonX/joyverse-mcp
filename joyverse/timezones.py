"""The user's timezone, so "today" means their day rather than UTC's.

Logs are stored in UTC and that does not change: storage has to be the same
for everyone and must not move when a person travels. What changes is how a
date is READ. For someone at UTC+5:30, "what did I do on the 10th" used to
return 05:30 on the 10th to 05:30 on the 11th -- the first hours of their
day were filed under the day before.

The timezone is the `timezone:` line of the profile -- already a fact the
profile holds and every agent knows how to set with update_profile -- rather
than a separate setting with its own tool. It accepts an IANA name
(Asia/Kolkata), a UTC offset (UTC+5:30, +0530) or a common abbreviation, and
tolerates the surrounding words a model tends to add ("Asia/Kolkata (IST)").
With no usable timezone everything behaves exactly as before, in UTC.
"""
import re
import time
from datetime import timedelta, timezone, tzinfo
from typing import Optional, Tuple

from joyverse.atomic import is_not_found
from joyverse.config import BUCKET_NAME, get_profile_key

CACHE_SECONDS = 60
_CACHE: dict = {}

_OFFSET = re.compile(
    r"^(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)

# Abbreviations are ambiguous in principle; these are the readings people
# almost always mean.
_ABBREVIATIONS = {
    "ist": "Asia/Kolkata", "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles", "pt": "America/Los_Angeles",
    "mst": "America/Denver", "mdt": "America/Denver",
    "cst": "America/Chicago", "cdt": "America/Chicago",
    "est": "America/New_York", "edt": "America/New_York",
    "et": "America/New_York", "bst": "Europe/London",
    "cet": "Europe/Paris", "cest": "Europe/Paris", "eet": "Europe/Athens",
    "jst": "Asia/Tokyo", "kst": "Asia/Seoul", "sgt": "Asia/Singapore",
    "hkt": "Asia/Hong_Kong", "aest": "Australia/Sydney",
    "aedt": "Australia/Sydney", "nzst": "Pacific/Auckland",
    "pkt": "Asia/Karachi", "gst": "Asia/Dubai", "wib": "Asia/Jakarta",
}


def _one(token: str) -> Optional[Tuple[tzinfo, str]]:
    token = token.strip()
    if not token:
        return None
    if token.lower() in ("utc", "gmt", "z"):
        return timezone.utc, "UTC"

    found = _OFFSET.match(token)
    if found:
        sign, hours, minutes = found.group(1), int(found.group(2)), \
            int(found.group(3) or 0)
        if hours > 14 or minutes > 59:
            return None
        delta = timedelta(hours=hours, minutes=minutes)
        if sign == "-":
            delta = -delta
        if not delta:
            return timezone.utc, "UTC"
        return timezone(delta), f"UTC{sign}{hours:02d}:{minutes:02d}"

    name = _ABBREVIATIONS.get(token.lower(), token.replace(" ", "_"))
    if "/" not in name:
        # A bare word is not an IANA zone worth trusting ("Local", "Home").
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), name
    except Exception:
        return None


def parse(value) -> Optional[Tuple[tzinfo, str]]:
    """(tzinfo, label) for a timezone as a person would write it, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    whole = _one(value)
    if whole:
        return whole
    # "Asia/Kolkata (IST, UTC+5:30)" -- take the first part that parses.
    for token in re.split(r"[\s(),;|]+", value):
        got = _one(token)
        if got:
            return got
    return None


def _profile_value(user_id: str) -> str:
    from joyverse.config import r2_client

    try:
        body = r2_client.get_object(Bucket=BUCKET_NAME,
                                    Key=get_profile_key(user_id))
        text = body["Body"].read().decode("utf-8", "replace")
    except Exception as e:
        if is_not_found(e):
            return ""
        raise
    for line in text.splitlines():
        stripped = line.strip().lstrip("-* ").strip()
        if stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        if key.strip().strip("*_").lower() in ("timezone", "time zone", "tz"):
            return value.strip().strip("*_").strip()
    return ""


def for_user(user_id: str) -> Tuple[tzinfo, str, str]:
    """(tzinfo, label, what the profile says) -- UTC when nothing usable.

    Never raises: a timezone is a nicety, and failing to read it must not
    fail a log read.
    """
    cached = _CACHE.get(user_id)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        stated = _profile_value(user_id)
    except Exception:
        return timezone.utc, "UTC", ""
    got = parse(stated)
    result = (got[0], got[1], stated) if got else (timezone.utc, "UTC", stated)
    _CACHE[user_id] = (time.time(), result)
    return result


def forget(user_id: str = None):
    """Drop the cached timezone, e.g. after the profile changed."""
    if user_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(user_id, None)
