"""One-off repairs to data written before agents were identified reliably.

Two jobs, both driven by scripts/migrate_agents.py:

* merge_clients     -- fold duplicate client names into one (gpt -> chatgpt,
                       hermes-atlas -> atlas): storage folder, registry entry
                       and log attribution.
* move_agent_logs   -- take agents' entries out of the user's log and put
                       them in each agent's own journal.

Every function takes `apply`. Without it nothing is written and the returned
report says what would happen. With it, each log day is backed up before it
is rewritten, and rewrites are conditional on the version that was read, so a
day an agent appends to mid-run is skipped rather than clobbered.
"""
import json
import time
from typing import Dict, Iterable, List, Optional

from joyverse import clients as client_names
from joyverse.atomic import Conflict, get_versioned, put_versioned
from joyverse.logs import OPERATIONAL_TAGS, journal_key
from joyverse.paths import client_prefix, normalise_client_name, storage_prefix
from joyverse.storage import CLIENTS_FILE, clients_key


def _list(r2, bucket: str, prefix: str) -> List[dict]:
    out, token = [], None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = r2.list_objects_v2(**kwargs)
        out.extend(page.get("Contents", []))
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token:
            return out


def _lines(raw: Optional[bytes]) -> List[dict]:
    out = []
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _dump(entries: Iterable[dict]) -> bytes:
    return "".join(json.dumps(e, separators=(",", ":")) + "\n"
                   for e in entries).encode("utf-8")


def _merge_entries(existing: List[dict], incoming: List[dict]) -> List[dict]:
    """Union of two entry lists in time order, without exact duplicates."""
    seen, out = set(), []
    for entry in existing + incoming:
        marker = json.dumps(entry, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            out.append(entry)
    out.sort(key=lambda e: str(e.get("ts", "")))
    return out


def _log_days(r2, bucket: str, user_id: str) -> List[str]:
    prefix = f"users/{user_id}/logs/"
    return sorted(o["Key"] for o in _list(r2, bucket, prefix)
                  if o["Key"][len(prefix):].endswith(".jsonl")
                  and "/" not in o["Key"][len(prefix):])


def _backup(r2, bucket: str, user_id: str, stamp: str, key: str, raw: bytes):
    name = key.rsplit("/", 1)[-1]
    r2.put_object(Bucket=bucket,
                  Key=f"users/{user_id}/backups/logs-{stamp}/{name}",
                  Body=raw, ContentType="application/x-ndjson")


# ==========================================
# MERGE DUPLICATE CLIENT NAMES
# ==========================================

def merge_clients(r2, bucket: str, user_id: str, mapping: Dict[str, str],
                  apply: bool = False) -> dict:
    """Fold each `old` client name in `mapping` into its `new` name."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    mapping = {normalise_client_name(old): normalise_client_name(new)
               for old, new in mapping.items()}
    report = {"applied": apply, "files_moved": [], "file_conflicts": [],
              "journal_days_merged": 0, "log_entries_relabelled": {},
              "registry_removed": [], "registry_added": [],
              "days_skipped_busy": []}

    # 1. Storage folders.
    for old, new in mapping.items():
        old_root = client_prefix(user_id, old)
        new_root = client_prefix(user_id, new)
        existing = {o["Key"] for o in _list(r2, bucket, new_root)}
        for obj in _list(r2, bucket, old_root):
            relative = obj["Key"][len(old_root):]
            target = new_root + relative
            is_journal = relative.startswith("journal/")
            if target in existing and not is_journal:
                # Never overwrite: two different files under one name need a
                # human to decide.
                report["file_conflicts"].append(f"{old}/{relative}")
                continue
            report["files_moved"].append(f"{old}/{relative} -> {new}/{relative}")
            if is_journal and target in existing:
                report["journal_days_merged"] += 1
            if not apply:
                continue
            body = r2.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            if is_journal and target in existing:
                current = r2.get_object(Bucket=bucket,
                                        Key=target)["Body"].read()
                body = _dump(_merge_entries(_lines(current), _lines(body)))
            head = {}
            try:
                head = r2.head_object(Bucket=bucket, Key=obj["Key"])
            except Exception:
                pass
            r2.put_object(
                Bucket=bucket, Key=target, Body=body,
                ContentType=head.get("ContentType")
                or "application/octet-stream",
                Metadata=dict(head.get("Metadata") or {}, client=new))
            r2.delete_object(Bucket=bucket, Key=obj["Key"])

    # 2. Registry.
    raw, version = get_versioned(r2, bucket, clients_key(user_id))
    registry = json.loads(raw.decode("utf-8")) if raw else {"clients": {}}
    entries = registry.setdefault("clients", {})
    for old, new in mapping.items():
        gone = entries.pop(old, None)
        if gone is not None:
            report["registry_removed"].append(old)
        if new not in entries:
            report["registry_added"].append(new)
            record = dict(gone or {}, name=new)
            record.setdefault("first_seen", time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            record["merged_from"] = [old]
            entries[new] = record
        elif gone is not None:
            entries[new].setdefault("merged_from", []).append(old)
            if gone.get("first_seen", "~") < entries[new].get("first_seen",
                                                              "~"):
                entries[new]["first_seen"] = gone["first_seen"]
    if apply and (report["registry_removed"] or report["registry_added"]):
        put_versioned(r2, bucket, clients_key(user_id),
                      json.dumps(registry, indent=2).encode("utf-8"), version,
                      ContentType="application/json")

    # 3. Attribution in the user's log.
    for key in _log_days(r2, bucket, user_id):
        raw, version = get_versioned(r2, bucket, key)
        entries_today = _lines(raw)
        changed = 0
        for entry in entries_today:
            current = client_names.clean(entry.get("client") or "")
            if current in mapping:
                counts = report["log_entries_relabelled"]
                label = f"{current} -> {mapping[current]}"
                counts[label] = counts.get(label, 0) + 1
                entry["client"] = mapping[current]
                changed += 1
        if changed and apply:
            try:
                _backup(r2, bucket, user_id, stamp, key, raw)
                put_versioned(r2, bucket, key, _dump(entries_today), version,
                              ContentType="application/x-ndjson")
            except Conflict:
                report["days_skipped_busy"].append(key.rsplit("/", 1)[-1])
    if apply:
        report["backup"] = f"users/{user_id}/backups/logs-{stamp}/"
    return report


# ==========================================
# MOVE AGENT ENTRIES OUT OF THE USER'S LOG
# ==========================================

def is_operational(entry: dict) -> bool:
    return bool({str(t).strip().lower() for t in entry.get("tags", [])}
                & OPERATIONAL_TAGS)


def move_agent_logs(r2, bucket: str, user_id: str,
                    all_from: Iterable[str] = (),
                    rename: Optional[Dict[str, str]] = None,
                    apply: bool = False) -> dict:
    """Move agents' entries from the user's log into their own journals.

    An entry moves when it is tagged as bookkeeping, or when its client is in
    `all_from` (every entry by that agent, whatever its tags). `rename` maps
    old client spellings to the journal they belong in. An entry with no
    usable client name has no journal to go to and is left where it is.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    all_from = {client_names.clean(c) for c in all_from}
    rename = {client_names.clean(k): client_names.clean(v)
              for k, v in (rename or {}).items()}
    report = {"applied": apply, "moved": {}, "kept": 0,
              "left_without_client": 0, "days_rewritten": 0,
              "days_emptied": 0, "days_skipped_busy": []}

    for key in _log_days(r2, bucket, user_id):
        day = key.rsplit("/", 1)[-1][:-len(".jsonl")]
        raw, version = get_versioned(r2, bucket, key)
        keep, outgoing = [], {}
        for entry in _lines(raw):
            name = client_names.clean(entry.get("client") or "")
            name = rename.get(name, name)
            if not (is_operational(entry) or name in all_from):
                keep.append(entry)
                continue
            try:
                name = normalise_client_name(name)
            except ValueError:
                report["left_without_client"] += 1
                keep.append(entry)
                continue
            outgoing.setdefault(name, []).append(dict(entry, client=name))
            report["moved"][name] = report["moved"].get(name, 0) + 1
        report["kept"] += len(keep)
        if not outgoing:
            continue
        report["days_rewritten"] += 1
        if not keep:
            report["days_emptied"] += 1
        if not apply:
            continue

        try:
            _backup(r2, bucket, user_id, stamp, key, raw)
            # Journals first: if the run stops here the entries exist twice,
            # never zero times, and a re-run de-duplicates them.
            for name, entries in outgoing.items():
                target = journal_key(user_id, name, day)
                current, jversion = get_versioned(r2, bucket, target)
                put_versioned(
                    r2, bucket, target,
                    _dump(_merge_entries(_lines(current), entries)),
                    jversion, ContentType="application/x-ndjson")
            if keep:
                put_versioned(r2, bucket, key, _dump(keep), version,
                              ContentType="application/x-ndjson")
            else:
                r2.delete_object(Bucket=bucket, Key=key)
        except Conflict:
            report["days_skipped_busy"].append(day)
    if apply:
        report["backup"] = f"users/{user_id}/backups/logs-{stamp}/"
    return report


def client_summary(r2, bucket: str, user_id: str) -> dict:
    """Read-only overview used to decide what to merge and move."""
    raw, _ = get_versioned(r2, bucket, clients_key(user_id))
    registered = sorted(json.loads(raw.decode("utf-8"))["clients"]) \
        if raw else []
    root = storage_prefix(user_id)
    folders: Dict[str, int] = {}
    for obj in _list(r2, bucket, root):
        rest = obj["Key"][len(root):]
        if "/" in rest and rest != CLIENTS_FILE:
            name = rest.split("/", 1)[0]
            folders[name] = folders.get(name, 0) + 1
    in_log: Dict[str, List[int]] = {}
    for key in _log_days(r2, bucket, user_id):
        raw, _ = get_versioned(r2, bucket, key)
        for entry in _lines(raw):
            name = entry.get("client") or "(none)"
            row = in_log.setdefault(name, [0, 0])
            row[0] += 1
            row[1] += is_operational(entry)
    return {"registered": registered, "files_per_folder": folders,
            "log_entries": {k: {"total": v[0], "operational": v[1]}
                            for k, v in sorted(in_log.items(),
                                               key=lambda x: -x[1][0])}}
