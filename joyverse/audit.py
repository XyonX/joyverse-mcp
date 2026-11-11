"""A record of every change made to an account, and by whom.

The stores say what the data is now. Nothing said how it got that way: which
agent replaced a data log, when a file was deleted, whether a write the user
never asked for happened at all. With several agents sharing one account
that question comes up, and the only evidence was whatever the agent chose
to say about itself.

So the server writes the record itself, after every tool call that can
change something: the time, the tool, the agent the connection identified,
and what it was aimed at. It is kept per day at users/<id>/audit/, beside the
log it resembles.

What is NOT recorded is the content written -- a summary, a file body, a
profile value. The audit trail is about who did what to which thing; copying
the data into it would make it a second, unprotected copy of everything.
"""
import json
from datetime import datetime, timezone

from joyverse.config import DATE_PATTERN

# Arguments that say WHICH thing was touched. Their values are recorded;
# every other argument is recorded only as its size.
_ADDRESS_ARGS = ("topic", "path", "field", "section", "document", "version",
                 "kind", "ts", "client", "name", "op", "upload_id", "prefix")
MAX_VALUE_CHARS = 200


def audit_key(user_id: str, date: str) -> str:
    from joyverse.config import _safe_user_id

    if not DATE_PATTERN.match(date or ""):
        raise ValueError(f"Invalid date: {date!r}. Use YYYY-MM-DD.")
    return f"users/{_safe_user_id(user_id)}/audit/{date}.jsonl"


def _succeeded(output, is_error: bool) -> bool:
    """Whether the tool did what was asked, read from its own reply."""
    if is_error:
        return False
    text = str(output or "").lstrip()
    if text.startswith("Error"):
        return False
    if text.startswith("{"):
        try:
            return "error" not in json.loads(text)
        except json.JSONDecodeError:
            return True
    return True


def record(user: dict, tool: str, arguments: dict, output=None,
           is_error: bool = False):
    """Append one audit entry. Never raises: auditing must not fail a call."""
    try:
        from joyverse.logs import _append_entry

        user_id = (user or {}).get("user_id")
        if not user_id:
            return
        now = datetime.now(timezone.utc)
        entry = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "tool": tool,
                 "ok": _succeeded(output, is_error)}
        if user.get("client"):
            entry["agent"] = user["client"]
        if user.get("auth_method"):
            entry["auth"] = user["auth_method"]

        target, sizes = {}, {}
        for name, value in (arguments or {}).items():
            if name == "user" or value is None:
                continue
            if name in _ADDRESS_ARGS and isinstance(value, (str, int, bool)):
                target[name] = str(value)[:MAX_VALUE_CHARS]
            else:
                sizes[name] = len(value) if isinstance(
                    value, (str, list, dict)) else 1
        if target:
            entry["target"] = target
        if sizes:
            entry["sizes"] = sizes
        _append_entry(audit_key(user_id, now.strftime("%Y-%m-%d")), entry)
    except Exception:
        pass


def get_audit_log(date: str = None, since: str = None, until: str = None,
                  limit: int = 50, tool: str = None, user: dict = None) -> str:
    """Read the record of changes made to this account.

    Same windowing as get_log: nothing gives the last 24 hours, `date` one
    day, `since`/`until` a range. `tool` narrows to one tool name.
    """
    from joyverse.logs import _days_under, _query, _read_entries, _user_zone

    user_id = user["user_id"]
    try:
        prefix = audit_key(user_id, "2000-01-01").rsplit("/", 1)[0] + "/"
    except ValueError as e:
        return json.dumps({"error": str(e)})

    def read_day(day):
        entries = _read_entries(f"{prefix}{day}.jsonl", day)
        if tool:
            entries = [e for e in entries if e.get("tool") == str(tool)]
        return entries

    return _query(read_day, lambda: _days_under(prefix), date, since, until,
                  limit, "newest", None, _user_zone(user_id))
