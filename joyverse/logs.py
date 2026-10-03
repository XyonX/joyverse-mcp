"""Daily conversation log: what happened, when, and with which agent.

One R2 object per day, one JSON entry per line. A day is small enough to read
whole, so retrieval needs no index -- asking about a date reads one object.

Appending is a read-modify-write because object storage has no append. At this
volume (tens of entries a day, one user) that is fine, but it is not atomic:
two appends racing can lose one. A local database would not fix this -- same
race, plus a second source of truth to disagree with R2.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from joyverse.config import BUCKET_NAME, get_log_key

# Default window when the caller names no date at all: the last 24 hours.
# A rolling window rather than "today", so "what have we been doing" answers
# the same way at 00:10 as at 23:50.
DEFAULT_WINDOW_HOURS = 24
DEFAULT_LIMIT = 20

# Guards a caller from asking for a window that spans more days than we care
# to scan. Reads are capped rather than unbounded.
MAX_DAYS_SCANNED = 400


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


def parse_when(value: str, field: str, end_of_day: bool = False) -> datetime:
    """Accept either a bare date or a full timestamp.

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
            day = datetime.strptime(raw, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
            if end_of_day:
                return day + timedelta(hours=23, minutes=59, seconds=59)
            return day
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            # Naive input is read as UTC, matching how timestamps are stored.
            dt = dt.replace(tzinfo=timezone.utc)
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

def _read_day(user_id: str, day: str) -> List[dict]:
    """Every entry for one day, tolerating a corrupt line.

    A day is read whole -- it is a few KB -- so there is no index to consult.
    A line that will not parse is skipped rather than failing the whole day,
    so one bad append cannot make a day unreadable.
    """
    from joyverse.config import r2_client

    try:
        body = r2_client.get_object(Bucket=BUCKET_NAME,
                                    Key=get_log_key(user_id, day))
    except Exception as e:
        if "NoSuchKey" in str(e) or "does not exist" in str(e):
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
    from joyverse.config import r2_client

    key = get_log_key(user_id, day)
    existing = ""
    try:
        existing = r2_client.get_object(Bucket=BUCKET_NAME,
                                        Key=key)["Body"].read().decode("utf-8")
    except Exception as e:
        if "NoSuchKey" not in str(e) and "does not exist" not in str(e):
            raise

    if existing and not existing.endswith("\n"):
        existing += "\n"
    body = existing + json.dumps(entry, separators=(",", ":")) + "\n"
    r2_client.put_object(Bucket=BUCKET_NAME, Key=key, Body=body.encode("utf-8"),
                         ContentType="application/x-ndjson")


# ==========================================
# TOOLS
# ==========================================

def add_to_log(summary: str, client: str = None, tags: Optional[list] = None,
               files: Optional[list] = None, user: dict = None) -> str:
    """Record one thing that happened, so a later session can recall it.

    Call this before finishing a conversation that did real work. The summary
    is the only part anyone reads back, so make it specific: say what was done
    and what it was about, not "worked on stuff".
    """
    user_id = user["user_id"]
    if not isinstance(summary, str) or not summary.strip():
        return json.dumps({
            "error": "summary is required.",
            "hint": "One line: what was done, and what it was about.",
        })

    now = _now()
    entry = {
        "ts": _iso(now),
        "summary": summary.strip()[:1000],
    }
    if client:
        entry["client"] = str(client)[:64]
    if tags:
        if not isinstance(tags, list):
            return json.dumps({"error": "tags must be a list of strings."})
        entry["tags"] = [str(t)[:48] for t in tags][:12]
    if files:
        if not isinstance(files, list):
            return json.dumps({"error": "files must be a list of paths."})
        entry["files"] = [str(f)[:300] for f in files][:20]

    try:
        _append_day(user_id, _day_str(now), entry)
    except ValueError as e:
        return json.dumps({"error": f"Could not log: {e}"})
    except Exception as e:
        return json.dumps({"error": f"Could not log: {e}"})

    return json.dumps({
        "ok": True,
        "logged": entry,
        "date": _day_str(now),
        "hint": "Use the files returned by save_file_* in `files` to link this "
                "entry to what you stored.",
    })


def _resolve_window(date, since, until):
    """Work out which days to read and the exact instant bounds.

    Three ways in, deliberately kept distinct because they answer different
    questions:
      * a date         -> that whole calendar day
      * since/until    -> an arbitrary range, which may cross midnight
      * neither        -> the rolling last N hours
    """
    if date:
        day = parse_when(date, "date")
        start = day
        end = day + timedelta(hours=23, minutes=59, seconds=59)
        return start, end

    if since or until:
        start = parse_when(since, "since") if since else None
        end = parse_when(until, "until", end_of_day=True) if until else None
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
    """Read back what was logged — recent activity, a day, or a time range.

    With no arguments this returns the last 24 hours, which is the usual
    question ("what have we been doing"). Pass `date` for one whole day, or
    `since`/`until` for a range such as 12pm-2pm. Both accept a bare date or a
    full timestamp.
    """
    user_id = user["user_id"]
    try:
        limit = int(limit) if limit else DEFAULT_LIMIT
        if limit < 1:
            raise LogError({"error": "limit must be at least 1."})
        limit = min(limit, 500)

        order = (order or "newest").lower()
        if order not in ("newest", "oldest"):
            raise LogError({"error": "order must be 'newest' or 'oldest'."})

        start, end = _resolve_window(date, since, until)

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

        entries = []
        for day in wanted:
            for entry in _read_day(user_id, day):
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
            "days_read": wanted,
        }, indent=2)
    except LogError as e:
        return json.dumps(e.payload)
    except ValueError as e:
        return json.dumps({"error": f"Invalid argument: {e}"})
    except Exception as e:
        return json.dumps({"error": f"Could not read log: {e}"})


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


def list_log_days(limit: int = 30, include_counts: bool = False,
                   user: dict = None) -> str:
    """See which days have a log, so you know what to ask for.

    Start here when you do not know whether something was recorded -- guessing
    a date and getting an error wastes a round trip.
    """
    from joyverse.config import r2_client

    user_id = user["user_id"]
    try:
        limit = min(int(limit) if limit else 30, 400)
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
