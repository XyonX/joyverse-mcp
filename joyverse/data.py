import difflib
import json
from typing import Dict, List

from joyverse.config import r2_client, BUCKET_NAME, get_data_key


# ==========================================
# TOPIC CATALOGUE
# ==========================================

def _data_prefix(user_id: str) -> str:
    return f"users/{user_id}/data/"


def _topic_from_key(key: str, prefix: str) -> str:
    """Extract the topic folder name from a full object key.

    Parsed from the key rather than reverse-engineered from filename_map, so a
    topic stored under any filename still lists correctly.
    """
    rest = key[len(prefix):]
    return rest.split("/", 1)[0] if "/" in rest else ""


def _describe(raw: str) -> str:
    """Best available human description of a data log.

    Prefers an explicit `description` field, then falls back to `summary`.
    The prompt already requires a `summary`, so every well-formed log has one
    and no parallel metadata store has to be kept in sync.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    for field in ("description", "summary"):
        value = parsed.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def list_topics(user: dict) -> str:
    """List every data topic this user has, with a description of each.

    Without this an agent must guess topic names, and a wrong guess returns a
    bare "No data found" with nothing to recover from -- which is how a client
    ends up inventing a reason for the failure.
    """
    user_id = user["user_id"]
    prefix = _data_prefix(user_id)

    try:
        listing = r2_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=prefix)
    except Exception as e:
        return json.dumps({"error": f"Could not list topics: {e}"})

    entries: Dict[str, dict] = {}
    for obj in listing.get("Contents", []):
        topic = _topic_from_key(obj.get("Key", ""), prefix)
        if not topic:
            continue
        entries.setdefault(topic, {
            "topic": topic,
            "description": "",
            "size_bytes": obj.get("Size", 0),
            "last_modified": str(obj.get("LastModified", ""))[:10],
            "_key": obj.get("Key", ""),
        })

    for entry in entries.values():
        try:
            body = r2_client.get_object(Bucket=BUCKET_NAME, Key=entry.pop("_key"))
            entry["description"] = _describe(
                body["Body"].read().decode("utf-8"))
        except Exception:
            # A topic we cannot read is still worth listing; an agent should
            # see that it exists even when the description is unavailable.
            entry.pop("_key", None)

    topics: List[dict] = [entries[t] for t in sorted(entries)]
    return json.dumps({
        "topics": topics,
        "count": len(topics),
        "hint": "Fetch one with get_data(topic=...)",
    }, indent=2)


def _topic_names(user_id: str) -> List[str]:
    """Topic names only, used to build a helpful error."""
    try:
        listing = r2_client.list_objects_v2(Bucket=BUCKET_NAME,
                                            Prefix=_data_prefix(user_id))
    except Exception:
        return []
    prefix = _data_prefix(user_id)
    names = {_topic_from_key(o.get("Key", ""), prefix)
             for o in listing.get("Contents", [])}
    return sorted(t for t in names if t)


def _normalise(name: str) -> str:
    """Fold the separators an agent might improvise into the same string.

    Topics are stored with underscores, but a caller may well type spaces or
    nothing at all ("mobile games", "mobilegames"), and those should still
    match.
    """
    return name.replace("_", "").replace("-", "").replace(" ", "").lower()


def _suggest(topic: str, available: List[str]) -> List[str]:
    """Best-effort near matches for a mistyped topic.

    Two tiers, because edit distance alone misses too much: "mobile" and
    "mobile_games" are close, but "games" and "steam_games" only relate
    through a shared substring, and a probe like "gaming" relates to neither.

    A wrong guess must still leave the caller able to recover, which is why
    `available_topics` is always returned regardless of what matches.
    """
    if not topic or not available:
        return []

    matches = difflib.get_close_matches(topic, available, n=3, cutoff=0.5)

    needle = _normalise(topic)
    if needle:
        for name in available:
            hay = _normalise(name)
            # substring either way: "mobile" finds "mobile_games",
            # "games" finds "steam_games".
            if (needle in hay or hay in needle) and name not in matches:
                matches.append(name)

    return sorted(matches)[:3]


def _miss(user_id: str, topic: str) -> str:
    """A "no such topic" error that tells the caller how to recover.

    A dead-end error is what pushes an agent into guessing again, or into
    fabricating an explanation for the failure.
    """
    available = _topic_names(user_id)
    payload = {
        "error": f"No data found for topic: {topic}",
        "available_topics": available,
        "hint": "Call list_topics() for descriptions of each topic.",
    }
    suggestions = _suggest(topic, available)
    if suggestions:
        payload["did_you_mean"] = suggestions
    return json.dumps(payload, indent=2)


# ==========================================
# TOOLS
# ==========================================

def get_data(topic: str, user: dict) -> str:
    user_id = user["user_id"]
    try:
        key = get_data_key(user_id, topic)
    except ValueError as e:
        return json.dumps({"error": f"Invalid topic: {e}"})
    try:
        response = r2_client.get_object(Bucket=BUCKET_NAME, Key=key)
        return response["Body"].read().decode("utf-8")
    except Exception as e:
        if "NoSuchKey" in str(e):
            return _miss(user_id, topic)
        return json.dumps({"error": f"R2 error: {str(e)}"})


def update_data(topic: str, data: str, user: dict) -> str:
    user_id = user["user_id"]
    try:
        key = get_data_key(user_id, topic)
    except ValueError as e:
        return f"Error: Invalid topic: {e}"
    try:
        parsed = json.loads(data)
        r2_client.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=json.dumps(parsed, indent=2).encode("utf-8"),
            ContentType="application/json"
        )
        return f"Updated data for {topic}"
    except json.JSONDecodeError:
        return "Error: Invalid JSON data"
    except Exception as e:
        return f"Error writing to R2: {str(e)}"
