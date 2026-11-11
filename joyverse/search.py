"""Find something without knowing where, or when, it was stored.

Every other read tool needs the caller to already know the address: a date
for the log, a topic name for data, a path for a file. An agent asked "when
did I last work on the pricing page" had to guess dates one at a time. This
is the tool that answers by content instead.

It is a scan, not an index. One person's text is small -- a few hundred day
files of a few KB, a few dozen topics -- so reading it is cheaper and far
less to keep correct than an index that every write would have to update.
The scan is bounded (newest MAX_LOG_DAYS days) and the reply says when the
bound was hit.

Matching is plain and predictable: case-insensitive, every word of the
query must appear. When nothing contains every word, entries containing any
of them are returned instead and the reply says so -- a near miss is more
use to a model than an empty list.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional

from joyverse.atomic import is_not_found
from joyverse.config import (
    BUCKET_NAME, get_bio_key, get_log_key, get_memory_key, get_profile_key,
)

AREAS = ("log", "data", "profile", "bio", "memory", "files", "journals")
# Journals are agents' working notes -- large and rarely what a question
# about the user is after -- so they are searched only when asked for.
DEFAULT_AREAS = ("log", "data", "profile", "bio", "memory", "files")

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_LOG_DAYS = 400
MAX_PER_TOPIC = 5
SNIPPET_CHARS = 240
_READERS = 8

# Fields that name an item inside a list, used to print a path an agent can
# hand straight to edit_data as `match`.
_NAME_FIELDS = ("name", "title", "id", "slug", "key")


# ==========================================
# MATCHING
# ==========================================

def _terms(query: str) -> List[str]:
    return [t for t in query.lower().split() if t]


def _score(text: str, phrase: str, terms: List[str], any_term: bool) -> int:
    """0 for no match; higher is better. The whole phrase beats the words."""
    hay = text.lower()
    if phrase in hay:
        return 3
    found = sum(1 for t in terms if t in hay)
    if found == len(terms):
        return 2
    if any_term and found:
        return 1
    return 0


def _snippet(text: str, terms: List[str]) -> str:
    """The part of a long text around the first match."""
    text = " ".join(str(text).split())
    if len(text) <= SNIPPET_CHARS:
        return text
    low = text.lower()
    at = min((low.find(t) for t in terms if t in low), default=0)
    start = max(0, at - SNIPPET_CHARS // 3)
    end = min(len(text), start + SNIPPET_CHARS)
    return ("..." if start else "") + text[start:end] + \
        ("..." if end < len(text) else "")


def _read_text(key: str) -> Optional[str]:
    from joyverse.config import r2_client

    try:
        body = r2_client.get_object(Bucket=BUCKET_NAME, Key=key)
        return body["Body"].read().decode("utf-8", "replace")
    except Exception as e:
        if is_not_found(e):
            return None
        raise


def _read_many(keys: Iterable[str]) -> Dict[str, Optional[str]]:
    """Read several objects at once; a failed read counts as absent."""
    keys = list(keys)

    def one(key):
        try:
            return _read_text(key)
        except Exception:
            return None

    if len(keys) <= 1:
        return {k: one(k) for k in keys}
    with ThreadPoolExecutor(max_workers=_READERS) as pool:
        return dict(zip(keys, pool.map(one, keys)))


# ==========================================
# ONE SEARCHER PER STORE
#
# Each yields (text to match against, hit to return, sort stamp).
# ==========================================

def _jsonl(raw: Optional[str]) -> List[dict]:
    out = []
    for line in (raw or "").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _entry_text(entry: dict) -> str:
    return " ".join([str(entry.get("summary", "")),
                     " ".join(str(t) for t in entry.get("tags", []) or []),
                     " ".join(str(f) for f in entry.get("files", []) or [])])


def _days_in_range(days: Iterable[str], since, until, notes: dict) -> List[str]:
    days = sorted((d for d in days
                   if (not since or d >= since) and (not until or d <= until)),
                  reverse=True)
    if len(days) > MAX_LOG_DAYS:
        notes["log_days_not_searched"] = len(days) - MAX_LOG_DAYS
        notes["oldest_day_searched"] = days[MAX_LOG_DAYS - 1]
        days = days[:MAX_LOG_DAYS]
    return days


def _search_log(user_id, since, until, notes):
    from joyverse.logs import _logged_days

    days = _days_in_range(_logged_days(user_id), since, until, notes)
    bodies = _read_many(get_log_key(user_id, d) for d in days)
    for day in days:
        for entry in _jsonl(bodies.get(get_log_key(user_id, day))):
            hit = {"where": "log", "date": day,
                   "ts": entry.get("ts", f"{day}T00:00:00Z")}
            for field in ("tags", "client", "files"):
                if entry.get(field):
                    hit[field] = entry[field]
            hit["_text_field"] = "summary"
            yield _entry_text(entry), hit, hit["ts"], entry.get("summary", "")


def _search_journals(user_id, since, until, notes):
    from joyverse.logs import _days_under, journal_prefix
    from joyverse.storage import _load_clients

    for client in sorted(_load_clients(user_id)["clients"]):
        try:
            prefix = journal_prefix(user_id, client)
        except ValueError:
            continue
        days = _days_in_range(_days_under(prefix), since, until, notes)
        bodies = _read_many(f"{prefix}{d}.jsonl" for d in days)
        for day in days:
            for entry in _jsonl(bodies.get(f"{prefix}{day}.jsonl")):
                hit = {"where": "journal", "client": client, "date": day,
                       "ts": entry.get("ts", f"{day}T00:00:00Z")}
                if entry.get("tags"):
                    hit["tags"] = entry["tags"]
                hit["_text_field"] = "summary"
                yield (_entry_text(entry), hit, hit["ts"],
                       entry.get("summary", ""))


def _leaves(node: Any, path: str = ""):
    """Every scalar in a JSON document, with a readable path to it."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaves(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            label = str(index)
            if isinstance(item, dict):
                for field in _NAME_FIELDS:
                    if isinstance(item.get(field), (str, int)):
                        label = f"{field}={item[field]}"
                        break
            yield from _leaves(item, f"{path}[{label}]")
    elif node is not None:
        yield path, str(node)


def _search_data(user_id, since, until, notes):
    from joyverse.config import r2_client
    from joyverse.data import _data_prefix, _is_internal, _topic_from_key

    prefix = _data_prefix(user_id)
    listing = r2_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=prefix)
    keys = {}
    for obj in listing.get("Contents", []):
        key = obj.get("Key", "")
        topic = _topic_from_key(key, prefix)
        if topic and not _is_internal(key):
            keys.setdefault(topic, key)
    bodies = _read_many(keys.values())
    for topic in sorted(keys):
        try:
            doc = json.loads(bodies.get(keys[topic]) or "")
        except json.JSONDecodeError:
            continue
        for path, value in _leaves(doc):
            # The field name is searchable too, so "status" finds statuses.
            leaf = path.rsplit(".", 1)[-1]
            hit = {"where": "data", "topic": topic, "path": path,
                   "_text_field": "text"}
            yield f"{topic} {leaf} {value}", hit, "", value


def _markdown_blocks(text: str):
    """(section, line) for every non-empty line of a markdown document."""
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip()
            continue
        if stripped and not stripped.startswith("# "):
            yield section, stripped


def _search_markdown(where: str, key: str):
    text = _read_text(key)
    for section, line in _markdown_blocks(text or ""):
        hit = {"where": where, "_text_field": "text"}
        if section:
            hit["section"] = section
        yield f"{section} {line}", hit, "", line


def _search_memory(user_id):
    try:
        memory = json.loads(_read_text(get_memory_key(user_id)) or "{}")
    except json.JSONDecodeError:
        return
    for path, value in _leaves(memory):
        if path == "last_updated" or not value.strip():
            continue
        hit = {"where": "memory", "field": path, "_text_field": "text"}
        yield f"{path} {value}", hit, "", value


def _search_files(user_id):
    from joyverse.paths import storage_prefix
    from joyverse.storage import INTERNAL_FILES, _list_all

    root = storage_prefix(user_id)
    for obj in _list_all(root):
        rest = obj["Key"][len(root):]
        if rest in INTERNAL_FILES or "/" not in rest:
            continue
        client, path = rest.split("/", 1)
        if path.startswith("journal/"):
            continue
        hit = {"where": "files", "client": client,
               "size_bytes": obj.get("Size", 0),
               "modified": str(obj.get("LastModified", ""))[:19],
               "_text_field": "path"}
        # Separators become spaces so "pricing page" finds pricing-page.png.
        words = path.replace("/", " ").replace("-", " ").replace("_", " ")
        yield f"{path} {words}", hit, hit["modified"], path


# ==========================================
# THE TOOL
# ==========================================

def _areas(where) -> tuple:
    if not where:
        return DEFAULT_AREAS, None
    if isinstance(where, str):
        where = [w for w in where.replace(",", " ").split() if w]
    if not isinstance(where, list):
        return None, {"error": "where must be a list of areas.",
                      "areas": list(AREAS)}
    alias = {"logs": "log", "journal": "journals", "topics": "data",
             "file": "files"}
    chosen = []
    for item in where:
        name = str(item).strip().lower()
        if name == "all":
            return AREAS, None
        name = alias.get(name, name)
        if name not in AREAS:
            return None, {"error": f"Unknown area: {item!r}",
                          "areas": list(AREAS)}
        if name not in chosen:
            chosen.append(name)
    return tuple(chosen), None


def _date_only(value, field: str):
    """A since/until bound as YYYY-MM-DD, or None."""
    from joyverse.logs import parse_when

    if not value:
        return None
    return parse_when(value, field).strftime("%Y-%m-%d")


def search(query: str, where: Optional[list] = None, since: str = None,
           until: str = None, limit: int = DEFAULT_LIMIT,
           user: dict = None) -> str:
    """Search everything stored for this user by content.

    Looks through the user's log, data topics, profile, bio, memory and file
    names; pass where=["journals"] (or "all") to include agents' journals.
    since/until are dates and narrow the log and journals only.
    """
    from joyverse.logs import LogError

    user_id = user["user_id"]
    if not isinstance(query, str) or not query.strip():
        return json.dumps({"error": "query is required.",
                           "hint": "A word or short phrase to look for."})
    phrase = " ".join(query.lower().split())
    terms = _terms(query)
    areas, problem = _areas(where)
    if problem:
        return json.dumps(problem)
    try:
        limit = max(1, min(int(limit) if limit else DEFAULT_LIMIT, MAX_LIMIT))
    except (TypeError, ValueError):
        return json.dumps({"error": "limit must be a number."})
    try:
        since_day = _date_only(since, "since")
        until_day = _date_only(until, "until")
    except LogError as e:
        return json.dumps(e.payload)

    notes: dict = {}
    sources = {
        "log": lambda: _search_log(user_id, since_day, until_day, notes),
        "journals": lambda: _search_journals(user_id, since_day, until_day,
                                             notes),
        "data": lambda: _search_data(user_id, since_day, until_day, notes),
        "profile": lambda: _search_markdown("profile",
                                            get_profile_key(user_id)),
        "bio": lambda: _search_markdown("bio", get_bio_key(user_id)),
        "memory": lambda: _search_memory(user_id),
        "files": lambda: _search_files(user_id),
    }

    # Everything is read once and scored twice at most: strictly (every word)
    # and, only if that finds nothing, loosely (any word).
    candidates, failed = [], []
    for area in areas:
        try:
            candidates.extend(sources[area]())
        except Exception as e:
            failed.append({"area": area, "error": str(e)})

    matched = "all_words"
    scored = [(s, c) for c in candidates
              if (s := _score(c[0], phrase, terms, False))]
    if not scored and len(terms) > 1:
        matched = "any_word"
        scored = [(s, c) for c in candidates
                  if (s := _score(c[0], phrase, terms, True))]

    # Best match first, then newest. Undated stores sort after dated ones of
    # the same score, in a stable order.
    scored.sort(key=lambda sc: (sc[0], sc[1][2]), reverse=True)

    results, per_topic, by_area, trimmed = [], {}, {}, 0
    for score, (_text, hit, _stamp, display) in scored:
        area = hit["where"]
        by_area[area] = by_area.get(area, 0) + 1
        if area == "data":
            seen = per_topic.get(hit["topic"], 0)
            per_topic[hit["topic"]] = seen + 1
            if seen >= MAX_PER_TOPIC:
                trimmed += 1
                continue
        if len(results) >= limit:
            continue
        out = {k: v for k, v in hit.items() if k != "_text_field"}
        out[hit["_text_field"]] = _snippet(display, terms)
        results.append(out)

    total = len(scored) - trimmed
    reply = {
        "query": query.strip(),
        "results": results,
        "count": len(results),
        "total_matched": total,
        "has_more": total > len(results),
        "matched": matched,
        "matches_by_area": by_area,
        "searched": list(areas),
    }
    if matched == "any_word" and results:
        reply["note"] = ("Nothing contained every word, so these contain at "
                         "least one of them.")
    if trimmed:
        reply["note_data"] = (f"At most {MAX_PER_TOPIC} matches are shown per "
                              "data topic; call get_data for the whole topic.")
    if notes:
        reply["limits"] = notes
    if failed:
        reply["could_not_search"] = failed
    if not results:
        reply["hint"] = ("Try a single distinctive word. Journals are only "
                         "searched with where=[\"journals\"]."
                         if "journals" not in areas else
                         "Try a single distinctive word.")
    else:
        reply["next_step"] = ("log -> get_log(date=...); data -> "
                              "get_data(topic=...); files -> get_file(path=..., "
                              "client=...).")
    return json.dumps(reply, indent=2)
