"""Two logs, deliberately separate.

THE USER'S LOG -- what the *user* did: practised DSA, worked on a project,
shipped something. It is the answer to "what have I been doing", so it has to
stay short and human. Lives at users/<id>/logs/<date>.jsonl.

AN AGENT'S JOURNAL -- an agent's own working notes: routing ticks, status
sweeps, handoffs, what it tried. Each agent has one, inside its own storage
folder at storage/<client>/journal/<date>.jsonl, next to the files it saves.

They were one log, and an autonomous agent ticking every few minutes wrote
ninety-odd percent of it, burying the user's own activity. Telling agents
which log is which is not enough on its own, so add_to_log also routes: an
entry that is plainly operational is filed in the caller's journal rather
than the user's log, and the reply says so.

Both use one R2 object per day, one JSON entry per line. A day is small
enough to read whole, so asking about a date reads one object.

Appending is a read-modify-write because object storage has no append. The
write is conditional on the version that was read, so two appends racing
cannot lose one: the loser's write is refused and it appends again to the
fresh day.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from joyverse.atomic import (
    get_versioned, is_not_found, put_versioned, retry_on_conflict,
)
from joyverse.config import (
    BUCKET_NAME, DATE_PATTERN, USER_LOG_DAILY_PER_CLIENT, get_log_key,
)

# Default window when the caller names no date at all: the last 24 hours.
# A rolling window rather than "today", so "what have we been doing" answers
# the same way at 00:10 as at 23:50.
DEFAULT_WINDOW_HOURS = 24
DEFAULT_LIMIT = 20

# Guards a caller from asking for a window that spans more days than we care
# to scan. Reads are capped rather than unbounded.
MAX_DAYS_SCANNED = 400

# Windows longer than this list the days that exist before reading any.
LIST_BEFORE_READING_DAYS = 3


# Tags that mark an entry as an agent's own bookkeeping, not user activity.
OPERATIONAL_TAGS = frozenset({
    "tick", "route", "digest", "quiet", "quiet-tick", "quiet-window",
    "heartbeat", "monitor", "monitors", "sweep", "poll", "polling", "cron",
    "lease-refresh", "status-check", "healthcheck", "dispatch", "addendum",
})

USER_SUMMARY_MAX = 1000
# A journal entry is a working note, not a one-liner, so it gets more room.
JOURNAL_SUMMARY_MAX = 4000


class LogError(Exception):
    """Raised with a JSON-ready payload so tools return it verbatim."""

    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__(payload.get("error", "log error"))


# ==========================================
# TIME PARSING
# ==========================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_when(value: str, field: str, end_of_day: bool = False,
               tz=timezone.utc) -> datetime:
    """Accept either a bare date or a full timestamp.

    `tz` is the user's timezone: a bare date is THEIR calendar day and a
    timestamp with no offset is THEIR wall-clock time. The result is always
    UTC, which is how entries are stored.

    One parameter covers both styles, so `since="2026-10-01"` means midnight
    and `since="2026-10-01T12:00"` means noon. A bare date on `until` means the
    END of that day, so `since`/`until` around one date returns that whole day
    instead of an empty window.
    """
    if not isinstance(value, str) or not value.strip():
        raise LogError({"error": f"{field} must be a non-empty string."})
    raw = value.strip()
    try:
        if len(raw) == 10:                       # YYYY-MM-DD
            day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=tz)
            if end_of_day:
                day += timedelta(hours=23, minutes=59, seconds=59)
            return day.astimezone(timezone.utc)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            # Naive input is read in the user's timezone (UTC when they
            # have none), the way they would say a time out loud.
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(timezone.utc)
    except ValueError:
        raise LogError({
            "error": f"Could not read {field}={value!r}",
            "hint": "Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ.",
        })


def _day_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


# ==========================================
# STORAGE
# ==========================================

def journal_prefix(user_id: str, client: str) -> str:
    """Where one agent's journal lives: inside its own storage folder."""
    from joyverse.paths import client_prefix

    return f"{client_prefix(user_id, client)}journal/"


def journal_key(user_id: str, client: str, date: str) -> str:
    if not DATE_PATTERN.match(date or ""):
        raise ValueError(f"Invalid date: {date!r}. Use YYYY-MM-DD.")
    return f"{journal_prefix(user_id, client)}{date}.jsonl"


def _read_day(user_id: str, day: str) -> List[dict]:
    """Every entry in the USER'S log for one day."""
    return _read_entries(get_log_key(user_id, day), day)


def _read_entries(key: str, day: str) -> List[dict]:
    """Every entry in one day object, tolerating a corrupt line.

    A day is read whole -- it is a few KB -- so there is no index to consult.
    A line that will not parse is skipped rather than failing the whole day,
    so one bad append cannot make a day unreadable.
    """
    from joyverse.config import r2_client

    try:
        body = r2_client.get_object(Bucket=BUCKET_NAME, Key=key)
    except Exception as e:
        if is_not_found(e):
            return []
        raise

    out = []
    for line in body["Body"].read().decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entry.setdefault("ts", f"{day}T00:00:00Z")
            out.append(entry)
    return out


def _append_day(user_id: str, day: str, entry: dict):
    """Append to the USER'S log."""
    _append_entry(get_log_key(user_id, day), entry)


@retry_on_conflict
def _append_entry(key: str, entry: dict):
    from joyverse.config import r2_client

    raw, version = get_versioned(r2_client, BUCKET_NAME, key)
    existing = raw.decode("utf-8") if raw else ""

    if existing and not existing.endswith("\n"):
        existing += "\n"
    body = existing + json.dumps(entry, separators=(",", ":")) + "\n"
    put_versioned(r2_client, BUCKET_NAME, key, body.encode("utf-8"), version,
                  ContentType="application/x-ndjson")


# ==========================================
# TOOLS
# ==========================================

def _build_entry(summary, tags, files, max_summary: int) -> tuple:
    """Validate the common fields. Returns (entry, None) or (None, error)."""
    if not isinstance(summary, str) or not summary.strip():
        return None, {
            "error": "summary is required.",
            "hint": "One line: what was done, and what it was about.",
        }
    entry = {"ts": _iso(_now()), "summary": summary.strip()[:max_summary]}
    if tags:
        if not isinstance(tags, list):
            return None, {"error": "tags must be a list of strings."}
        entry["tags"] = [str(t)[:48] for t in tags][:12]
    if files:
        if not isinstance(files, list):
            return None, {"error": "files must be a list of paths."}
        entry["files"] = [str(f)[:300] for f in files][:20]
    return entry, None


def _registered_clients(user_id: str):
    """The user's client names, or None when the registry cannot be read."""
    try:
        from joyverse.storage import _load_clients
        return sorted(_load_clients(user_id)["clients"])
    except Exception:
        # Fail open: a registry hiccup must never block a log.
        return None


def _operational_reason(entry: dict, user_id: str, day: str, name):
    """Why an entry belongs in an agent's journal, or None if it does not.

    Two signals, both about the entry rather than about who sent it -- an
    agent may perfectly well record something the user did:

    * it is tagged as bookkeeping (tick, route, digest, ...)
    * the same agent has already put a day's worth of entries in the user's
      log, which is what keeping a running log there looks like
    """
    marked = sorted({str(t).strip().lower() for t in entry.get("tags", [])}
                    & OPERATIONAL_TAGS)
    if marked:
        return (f"it is tagged {', '.join(marked)}, which marks an agent's "
                "own bookkeeping")
    if name:
        try:
            today = sum(1 for e in _read_day(user_id, day)
                        if e.get("client") == name)
        except Exception:
            today = 0
        if today >= USER_LOG_DAILY_PER_CLIENT:
            return (f"'{name}' has already written {today} entries to the "
                    "user's log today, which is a running log rather than "
                    "the user's activity")
    return None


def add_to_log(summary: str, client: str = None, tags: Optional[list] = None,
               files: Optional[list] = None, user: dict = None) -> str:
    """Record something the USER did, so a later conversation can recall it.

    This is the user's own activity log -- "practised graphs", "shipped the
    pricing page" -- not a record of what an agent did on their behalf. An
    agent's working notes go in its journal (add_to_agent_log).

    `client` is the assistant writing the entry, never the subject of the
    work, which belongs in `tags`. It can be left out when the server already
    knows who is calling.
    """
    from joyverse import clients
    from joyverse.paths import normalise_client_name

    user_id = user["user_id"]
    entry, problem = _build_entry(summary, tags, files, USER_SUMMARY_MAX)
    if problem:
        return json.dumps(problem)

    registered = _registered_clients(user_id)
    explicit = bool(client)
    name = None
    if client:
        # Folded onto the spelling this user already has, so "gpt" is
        # attributed to "chatgpt" rather than starting a second identity.
        name = clients.canonical(str(client)[:64], registered or ())
    elif (user or {}).get("client"):
        name = clients.canonical(user["client"], registered or ())
        if registered is not None and name not in registered:
            try:
                from joyverse.storage import ensure_client
                ensure_client(user_id, name, user.get("client_source"))
                registered = registered + [name]
            except Exception:
                pass
    if name:
        entry["client"] = name

    # A name the model supplied that is not in the registry is almost always
    # the project it was working on rather than the assistant it is. The
    # entry is still stored as given -- rejecting would trade a mislabelled
    # row for a lost one -- but the reply carries a warning the model can act
    # on in the same turn.
    warning = None
    if explicit and registered is not None and name not in registered:
        warning = {
            "client": str(client),
            "message": "client is YOUR name as the assistant doing the "
                       "work (e.g. chatgpt, claude), not the project or "
                       "brand the work was about -- the subject goes in "
                       "tags.",
            "registered_clients": registered,
            "hint": "Log again with client set to your own name. If "
                    f"'{client}' really is the product you are, register "
                    "it once with register_client and keep using it.",
        }
        if name in {str(t).strip().lower() for t in entry.get("tags", [])}:
            warning["message"] += (" This name also appears in your tags, "
                                   "which is the tell-tale sign it is a "
                                   "topic, not an assistant.")

    day = _day_str(_now())
    reason = _operational_reason(entry, user_id, day, name)
    if reason:
        try:
            journal_client = normalise_client_name(name) if name else None
        except ValueError:
            journal_client = None
        if not journal_client:
            return json.dumps({
                "error": "This is an agent's own bookkeeping, and the user's "
                         "log only records what the user did.",
                "reason": reason,
                "hint": "Write it to your own journal with add_to_agent_log.",
            })
        try:
            _append_entry(journal_key(user_id, journal_client, day), entry)
        except Exception as e:
            return json.dumps({"error": f"Could not log: {e}"})
        return json.dumps({
            "ok": True,
            "stored_in": "agent_journal",
            "client": journal_client,
            "logged": entry,
            "date": day,
            "note": "Filed in your journal, not the user's log, because "
                    f"{reason}. The user's log is only for what the USER "
                    "did. Use add_to_agent_log for your own working notes; "
                    "read them back with get_agent_log.",
        })

    try:
        _append_day(user_id, day, entry)
    except Exception as e:
        return json.dumps({"error": f"Could not log: {e}"})

    out = {
        "ok": True,
        "stored_in": "user_log",
        "logged": entry,
        "date": day,
        "hint": "Use the files returned by save_file_* in `files` to link this "
                "entry to what you stored.",
    }
    if warning:
        out["warning"] = warning
    return json.dumps(out)


def add_to_agent_log(summary: str, tags: Optional[list] = None,
                     files: Optional[list] = None, client: str = None,
                     user: dict = None) -> str:
    """Write a note in your own journal -- the agent's log book.

    For anything about YOUR work rather than the user's: a routing tick, a
    status sweep, what you tried, a handoff for your next session. It is kept
    in your storage folder under journal/, one file per day, and never shows
    up in the user's log.
    """
    from joyverse.storage import StorageError, check_capacity, pick_client

    user_id = user["user_id"]
    client, problem = pick_client(client, user, register=True)
    if problem:
        return json.dumps(problem)
    entry, problem = _build_entry(summary, tags, files, JOURNAL_SUMMARY_MAX)
    if problem:
        return json.dumps(problem)

    day = _day_str(_now())
    try:
        key = journal_key(user_id, client, day)
        # The journal lives in file storage, so it counts against the quota.
        check_capacity(user_id, len(json.dumps(entry)) + 1)
        _append_entry(key, entry)
    except StorageError as e:
        return json.dumps(e.payload)
    except ValueError as e:
        return json.dumps({"error": f"Invalid client: {e}"})
    except Exception as e:
        return json.dumps({"error": f"Could not log: {e}"})

    return json.dumps({
        "ok": True,
        "stored_in": "agent_journal",
        "client": client,
        "logged": entry,
        "date": day,
        "path": f"journal/{day}.jsonl",
    })


def _resolve_window(date, since, until, tz=timezone.utc):
    """Work out which days to read and the exact instant bounds.

    Three ways in, deliberately kept distinct because they answer different
    questions:
      * a date         -> that whole calendar day
      * since/until    -> an arbitrary range, which may cross midnight
      * neither        -> the rolling last N hours
    """
    if date:
        day = parse_when(date, "date", tz=tz)
        start = day
        end = day + timedelta(hours=23, minutes=59, seconds=59)
        return start, end

    if since or until:
        start = parse_when(since, "since", tz=tz) if since else None
        end = parse_when(until, "until", end_of_day=True, tz=tz) \
            if until else None
        if end and not start:
            # `until` alone means "the day or so leading up to then". The
            # window used to be reported as open-ended while only the last
            # 24 hours were actually read.
            start = end - timedelta(hours=DEFAULT_WINDOW_HOURS)
        if start and end and end < start:
            raise LogError({
                "error": "until is before since.",
                "hint": f"since={since} is later than until={until}.",
            })
        return start, end

    now = _now()
    return now - timedelta(hours=DEFAULT_WINDOW_HOURS), now


def get_log(date: str = None, since: str = None, until: str = None,
            limit: int = DEFAULT_LIMIT, order: str = "newest",
            tags: Optional[list] = None, user: dict = None) -> str:
    """Read back the USER'S log — recent activity, a day, or a time range.

    With no arguments this returns the last 24 hours, which is the usual
    question ("what have I been doing"). Pass `date` for one whole day, or
    `since`/`until` for a range such as 12pm-2pm. Both accept a bare date or a
    full timestamp. Agents' own journals are not included; see get_agent_log.
    """
    user_id = user["user_id"]
    return _query(lambda day: _read_day(user_id, day),
                  lambda: _logged_days(user_id),
                  date, since, until, limit, order, tags,
                  _user_zone(user_id))


def get_agent_log(client: str = None, date: str = None, since: str = None,
                  until: str = None, limit: int = DEFAULT_LIMIT,
                  order: str = "newest", tags: Optional[list] = None,
                  user: dict = None) -> str:
    """Read an agent's journal -- your own by default.

    Same windowing as get_log: no arguments gives the last 24 hours, `date`
    one day, `since`/`until` a range. Pass `client` to read another agent's
    journal, which is how one agent picks up where another left off.
    """
    from joyverse.storage import pick_client

    user_id = user["user_id"]
    client, problem = pick_client(client, user)
    if problem:
        return json.dumps(problem)
    try:
        prefix = journal_prefix(user_id, client)
    except ValueError as e:
        return json.dumps({"error": f"Invalid client: {e}"})

    out = json.loads(_query(
        lambda day: _read_entries(f"{prefix}{day}.jsonl", day),
        lambda: _days_under(prefix),
        date, since, until, limit, order, tags, _user_zone(user_id)))
    if "error" not in out:
        out["client"] = client
    return json.dumps(out, indent=2)


def _user_zone(user_id: str) -> tuple:
    """(tzinfo, label) for this user; UTC when they have not set one."""
    from joyverse import timezones

    tz, label, _stated = timezones.for_user(user_id)
    return tz, label


def _with_local(entry: dict, tz) -> dict:
    """A copy of an entry with its time in the user's timezone added."""
    try:
        local = parse_when(str(entry.get("ts", "")), "ts").astimezone(tz)
    except LogError:
        return entry
    return dict(entry, local=local.strftime("%Y-%m-%d %H:%M"))


def _query(read_day, logged_days, date, since, until, limit, order,
           tags, zone=None) -> str:
    """Windowed read shared by the user's log and the agent journals.

    `read_day(day)` returns one day's entries; `logged_days()` returns the
    set of dates that exist. `zone` is the user's (tzinfo, label): dates in
    the request are read in it, and each entry gains a `local` time.
    """
    tz, tz_label = zone or (timezone.utc, "UTC")
    try:
        limit = int(limit) if limit else DEFAULT_LIMIT
        if limit < 1:
            raise LogError({"error": "limit must be at least 1."})
        limit = min(limit, 500)

        order = (order or "newest").lower()
        if order not in ("newest", "oldest"):
            raise LogError({"error": "order must be 'newest' or 'oldest'."})

        start, end = _resolve_window(date, since, until, tz)

        # Bound the scan. A caller asking for years of history gets an error
        # rather than a very large read.
        anchor_end = end or _now()
        anchor_start = start or (anchor_end - timedelta(hours=DEFAULT_WINDOW_HOURS))
        span_days = (anchor_end - anchor_start).days + 1
        if span_days > MAX_DAYS_SCANNED:
            raise LogError({
                "error": "Date range too wide to read.",
                "days": span_days,
                "max_days": MAX_DAYS_SCANNED,
                "hint": "Narrow the range, or use list_log_days first.",
            })

        # Every day the window touches, then filter by exact instant. A range
        # can partially overlap the days at either end, so filtering after
        # reading is what makes a midnight-spanning range correct.
        wanted = []
        cursor = anchor_start.replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor <= anchor_end:
            wanted.append(_day_str(cursor))
            cursor += timedelta(days=1)

        # A long range is mostly days with nothing logged. Reading each one
        # is a request apiece, so find out which days exist first and read
        # only those. Short windows skip the listing -- it would cost more
        # than the reads it saves.
        to_read = wanted
        if len(wanted) > LIST_BEFORE_READING_DAYS:
            existing = logged_days()
            to_read = [day for day in wanted if day in existing]

        entries = []
        for day in to_read:
            for entry in read_day(day):
                if tags:
                    have = {str(t).lower() for t in entry.get("tags", [])}
                    if not have & {str(t).lower() for t in tags}:
                        continue
                entries.append(entry)

        entries = _within(entries, start, end)
        entries.sort(key=lambda e: str(e.get("ts", "")),
                     reverse=(order == "newest"))

        total = len(entries)
        page = entries[:limit]
        if tz_label != "UTC":
            page = [_with_local(entry, tz) for entry in page]
        return json.dumps({
            "entries": page,
            "count": len(page),
            # Without this an agent that hits the limit concludes it has seen
            # everything, which is the failure that makes a truncated log
            # quietly misleading.
            "has_more": total > len(page),
            "total_matched": total,
            "limit": limit,
            "order": order,
            "window_start": _iso(start) if start else None,
            "window_end": _iso(end) if end else None,
            # Which timezone dates in the request were read in. `ts` is
            # always UTC; `local` on each entry is this timezone.
            "timezone": tz_label,
            "days_read": to_read,
        }, indent=2)
    except LogError as e:
        return json.dumps(e.payload)
    except ValueError as e:
        return json.dumps({"error": f"Invalid argument: {e}"})
    except Exception as e:
        return json.dumps({"error": f"Could not read log: {e}"})


def _logged_days(user_id: str) -> set:
    """Every date the USER'S log has an object for."""
    return _days_under(f"users/{user_id}/logs/")


def _days_under(prefix: str) -> set:
    """Every date with a day object under a prefix, from one listing."""
    from joyverse.config import r2_client

    days, token = set(), None
    while True:
        kwargs = {"Bucket": BUCKET_NAME, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = r2_client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            name = obj["Key"].split("/")[-1]
            if name.endswith(".jsonl"):
                days.add(name[:-len(".jsonl")])
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token:
            break
    return days


def _within(entries: List[dict], start, end) -> List[dict]:
    """Keep entries inside the instant bounds, ignoring unparseable stamps."""
    out = []
    for entry in entries:
        raw = str(entry.get("ts", ""))
        try:
            ts = parse_when(raw, "ts")
        except LogError:
            continue
        if start and ts < start:
            continue
        if end and ts > end:
            continue
        out.append(entry)
    return out


def _local_days(user_id, utc_days, limit, total, include_counts, tz,
                tz_label) -> str:
    """list_log_days for a user whose day is not the UTC day.

    Storage is one object per UTC day, so the user's calendar days can only
    be known by reading the entries: one stored day holds parts of two of
    theirs. One extra stored day is read so the oldest day shown is whole.
    """
    from joyverse.search import _read_many

    keys = {day: get_log_key(user_id, day) for day in utc_days[:limit + 1]}
    bodies = _read_many(keys.values())
    by_day: dict = {}
    for day, key in keys.items():
        for line in (bodies.get(key) or "").splitlines():
            try:
                entry = json.loads(line)
                ts = parse_when(str(entry.get("ts") or f"{day}T00:00:00Z"),
                                "ts")
            except (json.JSONDecodeError, LogError, AttributeError):
                continue
            local = ts.astimezone(tz).strftime("%Y-%m-%d")
            by_day.setdefault(local, []).append(entry)

    out = []
    for day in sorted(by_day, reverse=True)[:limit]:
        row = {"date": day}
        if include_counts:
            row["entries"] = len(by_day[day])
            names = sorted({str(e.get("client")) for e in by_day[day]
                            if e.get("client")})
            if names:
                row["clients"] = names
        out.append(row)
    return json.dumps({
        "days": out,
        "count": len(out),
        "total_days": max(total, len(out)),
        "has_more": total > limit,
        "timezone": tz_label,
        "next_step": f"Call get_log(date=\"{out[0]['date']}\") to read "
                     "a day." if out else "No log entries yet.",
    }, indent=2)


def list_log_days(limit: int = 30, include_counts: bool = False,
                   user: dict = None) -> str:
    """See which days have a log, so you know what to ask for.

    Start here when you do not know whether something was recorded -- guessing
    a date and getting an error wastes a round trip.
    """
    from joyverse.config import r2_client

    user_id = user["user_id"]
    try:
        limit = max(1, min(int(limit) if limit else 30, 400))
        prefix = f"users/{user_id}/logs/"

        days, token = [], None
        while True:
            kwargs = {"Bucket": BUCKET_NAME, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = r2_client.list_objects_v2(**kwargs)
            for obj in page.get("Contents", []):
                name = obj["Key"].split("/")[-1]
                if name.endswith(".jsonl"):
                    days.append((name[:-len(".jsonl")],
                                 obj.get("Size", 0)))
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
            if not token:
                break

        days.sort(reverse=True)
        total = len(days)
        shown = days[:limit]

        tz, tz_label = _user_zone(user_id)
        if tz_label != "UTC" and days:
            return _local_days(user_id, [d for d, _ in days], limit, total,
                               include_counts, tz, tz_label)

        # Counts need a read per day. Off by default so a 30-day listing is one
        # call rather than thirty; pass include_counts when the numbers matter.
        out = []
        for day, _size in shown:
            row = {"date": day}
            if include_counts:
                entries = _read_day(user_id, day)
                row["entries"] = len(entries)
                clients = sorted({str(e.get("client")) for e in entries
                                  if e.get("client")})
                if clients:
                    row["clients"] = clients
            out.append(row)

        return json.dumps({
            "days": out,
            "count": len(out),
            "total_days": total,
            "has_more": total > len(out),
            "next_step": f"Call get_log(date=\"{out[0]['date']}\") to read "
                         "a day." if out else "No log entries yet.",
        }, indent=2)
    except LogError as e:
        return json.dumps(e.payload)
    except Exception as e:
        return json.dumps({"error": f"Could not list log days: {e}"})


# ==========================================
# DELETING ONE ENTRY
# ==========================================

def _brief(entry: dict) -> dict:
    out = {"ts": entry.get("ts"), "summary": str(entry.get("summary", ""))[:160]}
    if entry.get("client"):
        out["client"] = entry["client"]
    return out


@retry_on_conflict
def _remove_entry(key: str, day: str, when: datetime, match: str) -> dict:
    """Remove the one entry at `when` from a day object.

    Works on the stored lines rather than re-serialising parsed entries, so
    every other line -- including one that will not parse -- is written back
    byte for byte.
    """
    from joyverse.config import r2_client

    raw, version = get_versioned(r2_client, BUCKET_NAME, key)
    lines = (raw or b"").decode("utf-8").splitlines()
    parsed = []
    for index, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            parsed.append((index, entry))

    def at_that_time(entry):
        try:
            return parse_when(str(entry.get("ts") or f"{day}T00:00:00Z"),
                              "ts") == when
        except LogError:
            return False

    hits = [(i, e) for i, e in parsed if at_that_time(e)]
    if match:
        hits = [(i, e) for i, e in hits
                if match.lower() in str(e.get("summary", "")).lower()]
    if not hits:
        return {"error": f"No entry at {_iso(when)}"
                         + (f" containing {match!r}" if match else "") + ".",
                "entries_that_day": [_brief(e) for _, e in parsed][:30],
                "hint": "Pass ts exactly as get_log returned it."}
    if len(hits) > 1:
        return {"error": f"{len(hits)} entries share that timestamp; "
                         "nothing was removed.",
                "matches": [_brief(e) for _, e in hits],
                "hint": "Pass match with a few words from the summary of "
                        "the one to remove."}

    index, entry = hits[0]
    del lines[index]
    remaining = [line for line in lines if line.strip()]
    if remaining:
        put_versioned(r2_client, BUCKET_NAME, key,
                      ("\n".join(remaining) + "\n").encode("utf-8"), version,
                      ContentType="application/x-ndjson")
    else:
        r2_client.delete_object(Bucket=BUCKET_NAME, Key=key)
    return {"ok": True, "deleted": entry, "entries_left_that_day":
            len(remaining)}


def delete_log_entry(ts: str, match: str = None, journal: bool = False,
                     client: str = None, user: dict = None) -> str:
    """Remove one entry from the user's log, or from an agent's journal.

    `ts` is the entry's timestamp exactly as get_log returned it. When two
    entries share a timestamp, `match` (words from the summary) picks one.
    Set journal=true to remove from an agent's journal instead -- yours by
    default, or `client`'s. The removed entry is returned, so it can be
    logged again if this was a mistake.
    """
    user_id = user["user_id"]
    try:
        when = parse_when(ts, "ts", tz=_user_zone(user_id)[0])
    except LogError as e:
        return json.dumps(e.payload)
    if isinstance(ts, str) and len(ts.strip()) == 10:
        return json.dumps({
            "error": "ts must be a full timestamp, not a date.",
            "hint": "Read the day with get_log(date=...) and pass the ts of "
                    "the entry to remove."})
    day = _day_str(when)

    try:
        if journal or client:
            from joyverse.storage import invalidate_usage, pick_client

            name, problem = pick_client(client, user)
            if problem:
                return json.dumps(problem)
            out = _remove_entry(journal_key(user_id, name, day), day, when,
                                (match or "").strip())
            if out.get("ok"):
                out["removed_from"] = f"journal of {name}"
                invalidate_usage(user_id)
        else:
            out = _remove_entry(get_log_key(user_id, day), day, when,
                                (match or "").strip())
            if out.get("ok"):
                out["removed_from"] = "user_log"
    except ValueError as e:
        return json.dumps({"error": f"Invalid argument: {e}"})
    except Exception as e:
        return json.dumps({"error": f"Could not delete: {e}"})
    return json.dumps(out, indent=2)
