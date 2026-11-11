import copy
import json
import threading
from datetime import datetime, timezone
from joyverse.atomic import (
    MISSING, get_versioned, is_not_found, put_versioned, retry_on_conflict,
)
from joyverse.config import (
    r2_client, BUCKET_NAME, get_memory_key, ensure_doc_size,
)

DEFAULT_MEMORY = {
    "personality": [],
    "observed_patterns": [],
    "preferences": {"explanation_style": "", "code_style": "", "feedback_style": ""},
    "current_context": {"main_focus": "", "immediate_next": "", "mood": ""},
    "last_updated": ""
}

# The bytes last read on this thread, between a versioned load and its save.
_PREVIOUS = threading.local()

MAX_ITEMS = 20          # personality traits, and observed patterns
MAX_ITEM_CHARS = 500

PREFERENCE_KEYS = tuple(DEFAULT_MEMORY["preferences"])
CONTEXT_KEYS = tuple(DEFAULT_MEMORY["current_context"])


def _normalise(memory) -> dict:
    """Fill in any part of the schema a stored memory is missing.

    A memory written by an older version, or edited by hand, may lack a list
    or a group. Without this a tool reaching for the missing part failed with
    a bare KeyError instead of simply starting it.
    """
    if not isinstance(memory, dict):
        memory = {}
    for field, default in DEFAULT_MEMORY.items():
        if isinstance(default, list):
            if not isinstance(memory.get(field), list):
                memory[field] = []
        elif isinstance(default, dict):
            if not isinstance(memory.get(field), dict):
                memory[field] = {}
            for sub, sub_default in default.items():
                memory[field].setdefault(sub, sub_default)
        else:
            memory.setdefault(field, default)
    return memory


def _load_memory(user_id: str) -> dict:
    key = get_memory_key(user_id)
    try:
        response = r2_client.get_object(Bucket=BUCKET_NAME, Key=key)
        return _normalise(
            json.loads(response["Body"].read().decode("utf-8")))
    except Exception as e:
        if is_not_found(e):
            fresh = copy.deepcopy(DEFAULT_MEMORY)
            _save_memory(user_id, fresh)
            return fresh
        raise e

def _load_memory_versioned(user_id: str) -> tuple:
    """Memory plus the version a write must still match. Never writes."""
    raw, version = get_versioned(r2_client, BUCKET_NAME,
                                 get_memory_key(user_id))
    if raw is None:
        return copy.deepcopy(DEFAULT_MEMORY), MISSING
    # Remembered so the save that follows can keep it as the earlier version
    # without reading the object a second time.
    _PREVIOUS.raw = (user_id, raw)
    return _normalise(json.loads(raw.decode("utf-8"))), version


def _save_memory(user_id: str, data: dict, version=None):
    key = get_memory_key(user_id)
    data["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    body = json.dumps(data, indent=2).encode("utf-8")
    ensure_doc_size(body, "memory model")
    put_versioned(r2_client, BUCKET_NAME, key, body, version,
                  ContentType="application/json")
    held = getattr(_PREVIOUS, "raw", None)
    _PREVIOUS.raw = None
    if held and held[0] == user_id and held[1] != body:
        from joyverse import history
        history.keep(user_id, "memory", held[1])

def get_memory(user: dict) -> str:
    user_id = user["user_id"]
    return json.dumps(_load_memory(user_id))

def _add_to_list(user_id: str, field: str, label: str, item) -> str:
    """Append to one of the capped lists, skipping what is already there."""
    text = str(item).strip()[:MAX_ITEM_CHARS] if item is not None else ""
    if not text:
        return f"Error: {label} cannot be empty."

    memory, version = _load_memory_versioned(user_id)
    items = memory[field]
    # The same observation gets made again in every conversation. Stored
    # twice it only pushes a different, real entry out of the capped list.
    if any(str(existing).strip().lower() == text.lower()
           for existing in items):
        return f"Already recorded, nothing changed: {text}"

    dropped = None
    if len(items) >= MAX_ITEMS:
        dropped = items.pop(0)
    items.append(text)
    _save_memory(user_id, memory, version)

    message = f"Added {label}: {text}"
    if dropped is not None:
        # Said out loud: the list is full, so adding one silently lost one.
        message += (f" (the list holds {MAX_ITEMS}; the oldest entry was "
                    f"dropped to make room: {dropped})")
    return message


@retry_on_conflict
def add_memory_trait(trait: str, user: dict) -> str:
    return _add_to_list(user["user_id"], "personality", "trait", trait)


@retry_on_conflict
def add_observed_pattern(pattern: str, user: dict) -> str:
    """Record a recurring behaviour noticed across conversations."""
    return _add_to_list(user["user_id"], "observed_patterns", "pattern",
                        pattern)


@retry_on_conflict
def update_preference(name: str, value: str, user: dict) -> str:
    """Set how the user likes explanations, code or feedback delivered."""
    name = str(name).strip()
    if name not in PREFERENCE_KEYS:
        return ("Error: unknown preference. Use one of: "
                + ", ".join(PREFERENCE_KEYS))
    memory, version = _load_memory_versioned(user["user_id"])
    memory["preferences"][name] = str(value).strip()[:MAX_ITEM_CHARS]
    _save_memory(user["user_id"], memory, version)
    return f"Updated preference {name} to: {memory['preferences'][name]}"


@retry_on_conflict
def update_context(field: str, value: str, user: dict) -> str:
    """Set part of the user's current context: focus, next step, or mood."""
    field = str(field).strip()
    if field not in CONTEXT_KEYS:
        return ("Error: unknown context field. Use one of: "
                + ", ".join(CONTEXT_KEYS))
    memory, version = _load_memory_versioned(user["user_id"])
    memory["current_context"][field] = str(value).strip()[:MAX_ITEM_CHARS]
    _save_memory(user["user_id"], memory, version)
    return f"Updated {field} to: {memory['current_context'][field]}"

@retry_on_conflict
def update_focus(focus: str, user: dict) -> str:
    user_id = user["user_id"]
    memory, version = _load_memory_versioned(user_id)
    memory["current_context"]["main_focus"] = focus
    _save_memory(user_id, memory, version)
    return f"Updated focus to: {focus}"


_LISTS = {
    "trait": "personality", "traits": "personality",
    "personality": "personality",
    "pattern": "observed_patterns", "patterns": "observed_patterns",
    "observed_pattern": "observed_patterns",
    "observed_patterns": "observed_patterns",
}


@retry_on_conflict
def delete_memory_item(kind: str, text: str, user: dict = None) -> str:
    """Remove one personality trait or observed pattern from memory.

    `kind` is "trait" or "pattern". `text` is the entry, or any part of it
    that identifies exactly one entry. To clear a preference or a context
    field, set it to an empty value with update_preference / update_context.
    """
    field = _LISTS.get(str(kind or "").strip().lower())
    if not field:
        return json.dumps({"error": f"Unknown kind: {kind!r}",
                           "kinds": ["trait", "pattern"]})
    needle = str(text or "").strip().lower()
    if not needle:
        return json.dumps({"error": "text is required."})

    memory, version = _load_memory_versioned(user["user_id"])
    items = memory[field]
    hits = [i for i, item in enumerate(items)
            if str(item).strip().lower() == needle]
    if not hits:
        hits = [i for i, item in enumerate(items)
                if needle in str(item).lower()]
    if not hits:
        return json.dumps({"error": f"No {field} entry matches {text!r}.",
                           field: items})
    if len(hits) > 1:
        return json.dumps({
            "error": f"{len(hits)} entries match {text!r}; nothing was "
                     "removed.",
            "matches": [items[i] for i in hits],
            "hint": "Pass the full text of the one to remove."})

    removed = items.pop(hits[0])
    _save_memory(user["user_id"], memory, version)
    return json.dumps({"ok": True, "deleted": {field: removed},
                       "undo": "restore_version(document=\"memory\")"})
