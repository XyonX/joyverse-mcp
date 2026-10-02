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
# ADDRESSING
# ==========================================

def _load_topic(user_id: str, topic: str):
    """Read a data log, or None when it does not exist."""
    key = get_data_key(user_id, topic)
    try:
        body = r2_client.get_object(Bucket=BUCKET_NAME, Key=key)
        return json.loads(body["Body"].read().decode("utf-8"))
    except Exception as e:
        if "NoSuchKey" in str(e):
            return None
        raise


def _save_topic(user_id: str, topic: str, payload: dict):
    r2_client.put_object(
        Bucket=BUCKET_NAME,
        Key=get_data_key(user_id, topic),
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def _snapshot(user_id: str, topic: str, raw: bytes):
    """Keep the previous version of a log before an overwrite.

    Stored under a dotfile name so neither list_topics nor get_data ever
    surfaces it, and it cannot collide with a real topic.
    """
    try:
        key = f"users/{user_id}/data/{topic}/.previous.json"
        r2_client.put_object(Bucket=BUCKET_NAME, Key=key, Body=raw,
                             ContentType="application/json")
    except Exception:
        # A failed snapshot must not block the write itself.
        pass


def _walk(container, path):
    """Resolve a dotted path to (parent, key), creating dicts as needed.

    Only dicts are traversed: addressing an array by index is unsafe because
    indices shift whenever an item is added or removed.
    """
    if not path:
        return container, None
    parts = str(path).split(".")
    node = container
    for part in parts[:-1]:
        if not isinstance(node, dict):
            raise ValueError(f"Path '{path}' goes through a non-object.")
        node = node.setdefault(part, {})
    return node, parts[-1]


def _find(items, match):
    """Index of the first dict in `items` where all match pairs agree."""
    for i, item in enumerate(items):
        if isinstance(item, dict) and all(item.get(k) == v
                                         for k, v in match.items()):
            return i
    return -1


def _merge(base, patch):
    """RFC 7386 merge patch: nested objects merge, everything else replaces."""
    for k, v in patch.items():
        if v is None:
            base.pop(k, None)
        elif isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


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


# ==========================================
# GRANULAR EDITING
# ==========================================

def edit_data(topic: str, op: str, value=None, path=None, match=None,
              user: dict = None) -> str:
    """Change one thing inside a data log without disturbing the rest.

    Items are addressed by matching a field (`match`), never by array index:
    indices shift as soon as anything is added or removed.
    """
    user_id = user["user_id"]
    try:
        key = get_data_key(user_id, topic)
    except ValueError as e:
        return json.dumps({"error": f"Invalid topic: {e}"})

    raw = None
    try:
        raw = r2_client.get_object(Bucket=BUCKET_NAME, Key=key)["Body"].read()
    except Exception as e:
        if "NoSuchKey" not in str(e):
            return json.dumps({"error": f"R2 error: {e}"})

    doc = json.loads(raw.decode("utf-8")) if raw else {}
    match = match or {}
    parent, last = _walk(doc, path) if path else (doc, None)

    try:
        if op == "set":
            if match:
                # Inside a named item of an array.
                if not isinstance(parent.get(last), list):
                    return json.dumps({"error":
                        f"'{path}' is not a list; `match` needs a list."})
                items = parent[last]
                idx = _find(items, match)
                if idx < 0:
                    return json.dumps({
                        "error": f"No item matching {match} in '{path}'.",
                        "available": [i.get("name") for i in items
                                      if isinstance(i, dict)],
                    })
                item = dict(items[idx])
                item.update(value or {})
                items[idx] = item
            elif path:
                target = parent.setdefault(last, {})
                if isinstance(target, dict):
                    _merge(target, value or {})
                else:
                    parent[last] = value
            else:
                _merge(doc, value or {})

        elif op == "add":
            if not isinstance(parent.get(last), list):
                return json.dumps({"error":
                    f"'{path}' is not a list; use op=set for an object."})
            parent[last].append(value)

        elif op == "remove":
            if not isinstance(parent.get(last), list):
                return json.dumps({"error": f"'{path}' is not a list."})
            if not match:
                return json.dumps({"error":
                    "op=remove needs `match` to identify the item."})
            items = parent[last]
            idx = _find(items, match)
            if idx < 0:
                return json.dumps({
                    "error": f"No item matching {match} in '{path}'.",
                    "available": [i.get("name") for i in items
                                  if isinstance(i, dict)],
                })
            removed = items.pop(idx)
            _save_topic(user_id, topic, doc)
            return json.dumps({"ok": True, "op": "remove",
                               "removed": removed})

        elif op == "append":
            # Two shapes, chosen by whether `match` is given:
            #   without match -> append `value` to the list at `path`
            #   with match    -> append inside the matched item, where
            #                   value is {"list_name": [items]}
            items = parent.get(last)
            if match:
                if not isinstance(items, list):
                    return json.dumps({"error":
                        f"'{path}' is not a list; `match` needs a list."})
                idx = _find(items, match)
                if idx < 0:
                    return json.dumps({
                        "error": f"No item matching {match} in '{path}'.",
                        "available": [i.get("name") for i in items
                                      if isinstance(i, dict)],
                    })
                if not isinstance(value, dict):
                    return json.dumps({
                        "error": "Inside an item, value must be "
                                 '{"list_name": [...]} naming the list to '
                                 "append to."})
                target = items[idx]
                for field, extra in value.items():
                    lst = target.setdefault(field, [])
                    if not isinstance(lst, list):
                        return json.dumps({"error":
                            f"'{field}' is not a list."})
                    lst.extend(extra if isinstance(extra, list) else [extra])
            else:
                # A missing key is created as a list -- "append to notes"
                # should work before the first note exists. A key that is
                # present but NOT a list is a genuine mistake worth reporting.
                if items is None:
                    items = parent[last] = []
                if not isinstance(items, list):
                    return json.dumps({"error":
                        f"'{path}' is not a list; use op=set for objects."})
                items.extend(value if isinstance(value, list) else [value])

        else:
            return json.dumps({"error": f"Unknown op '{op}'. Use set, add, "
                                        "remove or append."})
    except ValueError as e:
        return json.dumps({"error": str(e)})

    _save_topic(user_id, topic, doc)
    return json.dumps({"ok": True, "op": op, "topic": topic})


def replace_data(topic: str, data: str, allow_drop: bool = False,
                 user: dict = None) -> str:
    """Overwrite a whole data log. Anything omitted is deleted.

    Refuses a write that would drop existing top-level keys unless
    allow_drop is set, and snapshots the previous version first.
    """
    user_id = user["user_id"]
    try:
        key = get_data_key(user_id, topic)
    except ValueError as e:
        return f"Error: Invalid topic: {e}"

    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return "Error: Invalid JSON data"

    existing = None
    raw = None
    try:
        raw = r2_client.get_object(Bucket=BUCKET_NAME, Key=key)["Body"].read()
        existing = json.loads(raw.decode("utf-8"))
    except Exception as e:
        if "NoSuchKey" not in str(e):
            return f"Error: R2 error: {e}"

    if (existing and isinstance(existing, dict) and isinstance(parsed, dict)
            and not allow_drop):
        dropped = [k for k in existing if k not in parsed]
        if dropped:
            return json.dumps({
                "error": "Refusing to overwrite: this write would drop "
                         "existing keys.",
                "would_drop": dropped,
                "hint": "Use edit_data for a partial change, or send the "
                        "complete object. Pass allow_drop=true to delete "
                        "keys deliberately.",
            })

    if raw is not None:
        _snapshot(user_id, topic, raw)

    try:
        r2_client.put_object(
            Bucket=BUCKET_NAME, Key=key,
            Body=json.dumps(parsed, indent=2).encode("utf-8"),
            ContentType="application/json")
        return f"Replaced data for {topic}"
    except Exception as e:
        return f"Error writing to R2: {str(e)}"
