"""whoami: what the server knows about the caller, in one call.

An agent had no way to ask "which account is this, can I write, what do you
call me, what day is it for this person". Each answer was scattered across
other tools or simply unavailable, so models guessed -- most visibly at their
own client name and at today's date.
"""
import json
from datetime import datetime, timezone

from joyverse.config import GB, MAX_USER_BYTES, READ_SCOPE, WRITE_SCOPE


def whoami(user: dict = None) -> str:
    """Who is calling, what they may do, and what is stored for them."""
    from joyverse import clients, timezones
    from joyverse.paths import storage_prefix
    from joyverse.storage import INTERNAL_FILES, _list_all, _load_clients

    user_id = user["user_id"]
    scopes = sorted(user.get("scopes") or [])
    out = {
        "user_id": user_id,
        "auth_method": user.get("auth_method"),
        "scopes": scopes,
        "can_read": READ_SCOPE in scopes,
        "can_write": WRITE_SCOPE in scopes,
    }
    for field in ("handle", "email"):
        if user.get(field):
            out[field] = user[field]

    # Which assistant the connection says this is.
    detected = user.get("client")
    try:
        registered = _load_clients(user_id)["clients"]
    except Exception:
        registered = {}
    out["you_are"] = clients.canonical(detected, registered) \
        if detected else None
    if detected:
        out["identified_by"] = user.get("client_source")
    else:
        out["how_to_be_identified"] = (
            "Set the X-Joyverse-Agent header (or ?agent= on the MCP URL) in "
            "this agent's MCP config; until then pass client yourself.")

    tz, label, stated = timezones.for_user(user_id)
    now = datetime.now(timezone.utc)
    out["time"] = {
        "utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timezone": label,
        "local": now.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "today": now.astimezone(tz).strftime("%Y-%m-%d"),
    }
    if not stated:
        out["time"]["note"] = (
            "No timezone is set, so dates are UTC. Set one with "
            "update_profile(field=\"timezone\", value=\"Asia/Kolkata\").")
    elif label == "UTC" and "utc" not in stated.lower() \
            and "gmt" not in stated.lower():
        out["time"]["note"] = (
            f"The profile's timezone {stated!r} could not be read, so dates "
            "are UTC. Use an IANA name (Asia/Kolkata) or an offset "
            "(UTC+5:30).")

    # One listing of the account answers every count.
    try:
        root = f"users/{user_id}/"
        storage = storage_prefix(user_id)
        topics, log_days, files, file_bytes, journal_days = set(), 0, 0, 0, 0
        has = {"profile": False, "bio": False, "memory": False}
        for obj in _list_all(root):
            key, rest = obj["Key"], obj["Key"][len(root):]
            if key.startswith(storage):
                inner = key[len(storage):]
                file_bytes += obj.get("Size", 0)
                if inner in INTERNAL_FILES:
                    continue
                parts = inner.split("/")
                if len(parts) == 3 and parts[1] == "journal" and \
                        inner.endswith(".jsonl"):
                    journal_days += 1
                else:
                    files += 1
            elif rest.startswith("data/"):
                parts = rest.split("/")
                if len(parts) == 3 and not parts[2].startswith("."):
                    topics.add(parts[1])
            elif rest.startswith("logs/") and rest.endswith(".jsonl"):
                log_days += 1
            elif rest in ("profile.md", "bio.md", "memory.json"):
                has[rest.split(".")[0]] = True
        out["stored"] = {
            "has_profile": has["profile"], "has_bio": has["bio"],
            "has_memory": has["memory"],
            "data_topics": len(topics), "log_days": log_days,
            "files": files, "journal_days": journal_days,
            "clients": sorted(registered),
            "storage_used_gb": round(file_bytes / GB, 3),
            "storage_limit_gb": MAX_USER_BYTES // GB,
        }
        if not has["profile"]:
            out["next_step"] = ("This is a new account: there is no profile "
                                "yet. Ask before setting one up.")
    except Exception as e:
        out["stored"] = {"error": f"Could not read storage: {e}"}
    return json.dumps(out, indent=2)
