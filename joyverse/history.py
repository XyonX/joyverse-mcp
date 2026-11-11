"""Earlier versions of the profile, bio and memory.

These three documents are rewritten whole on every change, by models, with
no undo. One wrong `update_bio` replaced a section the user had dictated and
it was simply gone. Data topics already keep the version they replaced
(restore_data); this gives the other three the same safety, a few versions
deep, because they are edited in bursts -- onboarding writes six sections in
a row, and one level of undo would only bring back the fifth.

Every write keeps the version it replaced under
users/<id>/history/<document>/<timestamp>.bak and prunes to the newest KEEP.
Restoring is itself a write, so it keeps the version it displaced and can be
undone the same way.
"""
import json
from datetime import datetime, timezone
from typing import List, Optional

from joyverse.atomic import (
    get_versioned, put_versioned, retry_on_conflict,
)
from joyverse.config import (
    BUCKET_NAME, get_bio_key, get_memory_key, get_profile_key,
)

KEEP = 10

DOCUMENTS = {
    "profile": (get_profile_key, "text/markdown"),
    "bio": (get_bio_key, "text/markdown"),
    "memory": (get_memory_key, "application/json"),
}


def _prefix(user_id: str, document: str) -> str:
    # get_*_key validates the user id; reuse that rather than trusting it.
    root = DOCUMENTS[document][0](user_id).rsplit("/", 1)[0]
    return f"{root}/history/{document}/"


def _versions(user_id: str, document: str) -> List[dict]:
    """Stored versions, newest first."""
    from joyverse.storage import _list_all

    prefix = _prefix(user_id, document)
    out = []
    for obj in _list_all(prefix):
        name = obj["Key"][len(prefix):]
        if name.endswith(".bak") and "/" not in name:
            out.append({"version": name[:-len(".bak")], "key": obj["Key"],
                        "size_bytes": obj.get("Size", 0)})
    out.sort(key=lambda v: v["version"], reverse=True)
    return out


def keep(user_id: str, document: str, raw: Optional[bytes]):
    """Store `raw` -- the version about to be replaced. Never raises.

    A failed snapshot must not block the write it protects.
    """
    if not raw:
        return
    from joyverse.config import r2_client

    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        r2_client.put_object(
            Bucket=BUCKET_NAME, Key=f"{_prefix(user_id, document)}{stamp}.bak",
            Body=raw, ContentType=DOCUMENTS[document][1])
        for old in _versions(user_id, document)[KEEP:]:
            r2_client.delete_object(Bucket=BUCKET_NAME, Key=old["key"])
    except Exception:
        pass


def _saved_at(version: str) -> str:
    try:
        return datetime.strptime(version, "%Y%m%dT%H%M%S.%fZ").strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return version


def _document(document) -> Optional[str]:
    name = str(document or "").strip().lower()
    return name if name in DOCUMENTS else None


def list_history(document: str, user: dict = None) -> str:
    """The earlier versions kept for the profile, bio or memory."""
    name = _document(document)
    if not name:
        return json.dumps({"error": f"Unknown document: {document!r}",
                           "documents": sorted(DOCUMENTS)})
    try:
        versions = _versions(user["user_id"], name)
    except Exception as e:
        return json.dumps({"error": f"Could not read history: {e}"})
    return json.dumps({
        "document": name,
        "versions": [{"version": v["version"],
                      "replaced_at": _saved_at(v["version"]),
                      "size_bytes": v["size_bytes"]} for v in versions],
        "count": len(versions),
        "keeps": KEEP,
        "hint": "restore_version(document) brings back the newest one; pass "
                "version to pick another. Data topics use restore_data."
                if versions else
                "No earlier version yet: one is kept each time the document "
                "is changed.",
    }, indent=2)


@retry_on_conflict
def restore_version(document: str, version: str = None,
                    user: dict = None) -> str:
    """Bring back an earlier version of the profile, bio or memory."""
    from joyverse.config import r2_client
    from joyverse import timezones

    name = _document(document)
    if not name:
        return json.dumps({"error": f"Unknown document: {document!r}",
                           "documents": sorted(DOCUMENTS)})
    user_id = user["user_id"]
    try:
        versions = _versions(user_id, name)
    except Exception as e:
        return json.dumps({"error": f"Could not read history: {e}"})
    if not versions:
        return json.dumps({
            "error": f"There is no earlier version of the {name} to restore.",
            "hint": "One is kept each time the document is changed."})

    chosen = versions[0]
    if version:
        wanted = str(version).strip()
        chosen = next((v for v in versions if v["version"] == wanted), None)
        if chosen is None:
            return json.dumps({
                "error": f"No such version: {wanted}",
                "versions": [v["version"] for v in versions]})

    key, content_type = DOCUMENTS[name][0](user_id), DOCUMENTS[name][1]
    try:
        body = r2_client.get_object(Bucket=BUCKET_NAME,
                                    Key=chosen["key"])["Body"].read()
        current, etag = get_versioned(r2_client, BUCKET_NAME, key)
    except Exception as e:
        return json.dumps({"error": f"R2 error: {e}"})
    if current == body:
        return json.dumps({"ok": True, "document": name, "changed": False,
                           "note": "That version is identical to the current "
                                   "one, so nothing was changed."})

    put_versioned(r2_client, BUCKET_NAME, key, body, etag,
                  ContentType=content_type)
    # After the write, so a lost race does not leave a stray snapshot.
    keep(user_id, name, current)
    if name == "profile":
        timezones.forget(user_id)
    return json.dumps({
        "ok": True, "document": name, "changed": True,
        "restored_version": chosen["version"],
        "replaced_at": _saved_at(chosen["version"]),
        "hint": "The version this replaced was kept too: call list_history "
                "to see it, and restore_version to go back.",
    })
