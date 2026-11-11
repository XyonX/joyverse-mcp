"""Inline upload for files the agent already holds.

The gap this fills: an agent can save text it produced, and can hand us a
public URL to fetch, but a file it was given directly -- a reference image in
the conversation, say -- has neither. Those bytes exist only inside the model,
so the only way out is for it to send them to us.

Base64 is the transport because JSON has no binary type and every MCP client
already speaks it. The cost is real: the payload is charged to the agent's
context, which is why MAX_INLINE_BYTES is small. This is the small-file path
only. Large files need a transport that does not route bytes through the
model, which does not exist yet.
"""
import base64
import binascii
import json
from typing import Optional

from joyverse.config import (
    BUCKET_NAME, GB, MAX_FILE_BYTES, MAX_INLINE_BYTES, MAX_USER_BYTES, MB,
)
from joyverse.paths import resolve_file_path
from joyverse.storage import (
    check_capacity, existing_size, invalidate_usage, pick_client, user_usage,
    StorageError,
)


def _decode(data: str) -> bytes:
    """Strict base64 decode.

    validate=True so stray characters raise instead of being silently
    discarded. Without it a corrupted payload would decode to fewer bytes,
    upload cleanly, and store a file that is quietly wrong.
    """
    if not isinstance(data, str):
        raise StorageError({
            "error": "data must be a base64 string.",
            "hint": "Encode with base64.b64encode(raw_bytes).decode('ascii').",
        })
    if not data:
        raise StorageError({"error": "data is empty."})
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise StorageError({
            "error": f"data is not valid base64: {e}",
            "hint": "Encode with base64.b64encode(raw_bytes).decode('ascii'). "
                    "Do not wrap, prefix, or URL-encode it.",
        })


def save_file_base64(path: str, data: str, client: str = None,
                     mime_type: str = None, user: dict = None) -> str:
    """Store a file the agent holds directly, as base64 bytes.

    Use this for images, documents or media already in your context -- an
    attached file, or something you generated. Do not use it to fetch a public
    URL (save_file_from_url is better for that) or to move a large file: past a
    few megabytes the payload becomes expensive in your context window.
    """
    from joyverse.config import r2_client

    user_id = user["user_id"]
    client, problem = pick_client(client, user, register=True)
    if problem:
        return json.dumps(problem)
    try:
        key, relative = resolve_file_path(user_id, client, path)
    except ValueError as e:
        return json.dumps({"error": f"Invalid path: {e}"})

    try:
        body = _decode(data)
        if len(body) > MAX_INLINE_BYTES:
            raise StorageError({
                "error": "File too large for an inline upload",
                "file_mb": round(len(body) / MB, 2),
                "limit_mb": MAX_INLINE_BYTES // MB,
                "hint": "Base64 uploads are capped because the bytes are "
                        "charged to your context window. For larger files, "
                        "host the file at a public URL and use "
                        "save_file_from_url instead.",
            })
        if len(body) > MAX_FILE_BYTES:
            raise StorageError({
                "error": "File too large",
                "file_mb": round(len(body) / MB, 2),
                "limit_mb": MAX_FILE_BYTES // MB,
            })

        mime = mime_type or "application/octet-stream"
        check_capacity(user_id, len(body), existing_size(key))
        r2_client.put_object(
            Bucket=BUCKET_NAME, Key=key, Body=body, ContentType=mime,
            Metadata={"client": client, "mime": mime[:120],
                      "upload": "inline"})
        invalidate_usage(user_id)
    except StorageError as e:
        return json.dumps(e.payload)
    except Exception as e:
        return json.dumps({"error": f"Could not save file: {e}"})

    used, _ = user_usage(user_id, use_cache=False)
    return json.dumps({
        "ok": True,
        "path": relative,
        "client": client,
        "size_bytes": len(body),
        "mime_type": mime,
        "used_gb": round(used / GB, 3),
        "limit_gb": MAX_USER_BYTES // GB,
    })


# ==========================================
# LARGE FILES: AN UPLOAD LINK
#
# The transport the module docstring says did not exist. A file too big for
# base64 and with no public URL -- a video an agent rendered on its own
# machine -- is sent straight to storage with an HTTP PUT, so the bytes never
# pass through the model or through this server.
#
# Two steps, because the server cannot see a direct upload happen:
#
#   request_upload   checks the name, size and quota, and returns a link
#   complete_upload  confirms the file arrived and is what was declared
#
# The link writes to the file's final location. A signed link cannot be
# trusted to enforce the declared size, so completion re-checks it: a file
# bigger than declared, or one that breaks the quota, is deleted there.
# ==========================================

PENDING_MAX_AGE_SECONDS = 24 * 3600


def _pending_key(user_id: str, upload_id: str) -> str:
    from joyverse.config import _safe_user_id

    if not upload_id.isalnum():
        raise ValueError("Invalid upload id.")
    return f"users/{_safe_user_id(user_id)}/uploads/{upload_id}.json"


def _forget_stale(user_id: str):
    """Drop upload records nobody completed. Best effort."""
    import time
    from joyverse.config import r2_client
    from joyverse.storage import _list_all

    try:
        cutoff = time.time() - PENDING_MAX_AGE_SECONDS
        for obj in _list_all(f"users/{user_id}/uploads/"):
            modified = obj.get("LastModified")
            stamp = modified.timestamp() if hasattr(modified, "timestamp") \
                else None
            if stamp is not None and stamp < cutoff:
                r2_client.delete_object(Bucket=BUCKET_NAME, Key=obj["Key"])
    except Exception:
        pass


def request_upload(path: str, size_bytes: int, mime_type: str = None,
                   client: str = None, overwrite: bool = False,
                   user: dict = None) -> str:
    """Get a link to upload a large file directly to storage.

    Send the file with an HTTP PUT to the returned url, then call
    complete_upload. For files too large for save_file_base64 that have no
    public URL for save_file_from_url.
    """
    import secrets
    import time
    from joyverse.config import UPLOAD_LINK_SECONDS, r2_client

    user_id = user["user_id"]
    client, problem = pick_client(client, user, register=True)
    if problem:
        return json.dumps(problem)
    try:
        size = int(size_bytes)
    except (TypeError, ValueError):
        return json.dumps({"error": "size_bytes must be the file's size in "
                                    "bytes."})
    if size < 1:
        return json.dumps({"error": "size_bytes must be at least 1."})
    try:
        key, relative = resolve_file_path(user_id, client, path)
    except ValueError as e:
        return json.dumps({"error": f"Invalid path: {e}"})

    try:
        replacing = existing_size(key)
        if replacing and not overwrite:
            return json.dumps({
                "error": f"A file already exists at {relative}.",
                "hint": "Choose another path, or pass overwrite=true to "
                        "replace it. The old file is replaced the moment "
                        "the upload starts."})
        check_capacity(user_id, size, replacing=replacing)
    except StorageError as e:
        return json.dumps(e.payload)

    content_type = (mime_type or "application/octet-stream").strip()
    upload_id = secrets.token_hex(12)
    try:
        _forget_stale(user_id)
        url = r2_client.generate_presigned_url(
            "put_object",
            Params={"Bucket": BUCKET_NAME, "Key": key,
                    "ContentType": content_type},
            ExpiresIn=UPLOAD_LINK_SECONDS)
        r2_client.put_object(
            Bucket=BUCKET_NAME, Key=_pending_key(user_id, upload_id),
            Body=json.dumps({
                "client": client, "path": relative, "key": key,
                "size_bytes": size, "mime_type": content_type,
                "replaced_bytes": replacing,
                "requested_at": int(time.time())}).encode("utf-8"),
            ContentType="application/json")
    except Exception as e:
        return json.dumps({"error": f"Could not create an upload link: {e}"})

    return json.dumps({
        "ok": True,
        "upload_id": upload_id,
        "url": url,
        "method": "PUT",
        "headers": {"Content-Type": content_type},
        "expires_in_minutes": UPLOAD_LINK_SECONDS // 60,
        "path": relative,
        "client": client,
        "example": f"curl -X PUT -H 'Content-Type: {content_type}' "
                   "--upload-file <local file> '<url>'",
        "next_step": f"After the PUT succeeds, call complete_upload("
                     f"upload_id=\"{upload_id}\"). The Content-Type header "
                     "must be sent exactly as shown or the link is refused.",
    }, indent=2)


def complete_upload(upload_id: str, user: dict = None) -> str:
    """Confirm a file sent to a request_upload link arrived intact."""
    from joyverse.atomic import is_not_found
    from joyverse.config import r2_client

    user_id = user["user_id"]
    try:
        pending_key = _pending_key(user_id, str(upload_id or "").strip())
    except ValueError as e:
        return json.dumps({"error": str(e)})
    try:
        pending = json.loads(r2_client.get_object(
            Bucket=BUCKET_NAME, Key=pending_key)["Body"].read())
    except Exception as e:
        if is_not_found(e):
            return json.dumps({
                "error": "No such upload, or it was already completed.",
                "hint": "Call list_files to check whether the file is "
                        "there, or request_upload for a new link."})
        return json.dumps({"error": f"Could not reach storage: {e}"})

    key, relative = pending["key"], pending["path"]
    try:
        head = r2_client.head_object(Bucket=BUCKET_NAME, Key=key)
    except Exception as e:
        if is_not_found(e):
            return json.dumps({
                "error": "Nothing has been uploaded to that link yet.",
                "path": relative,
                "hint": "PUT the file to the url from request_upload first. "
                        "If the link expired, request a new one."})
        return json.dumps({"error": f"Could not reach storage: {e}"})

    actual = head.get("ContentLength") or 0
    declared = pending["size_bytes"]
    invalidate_usage(user_id)

    def reject(error: dict) -> str:
        try:
            r2_client.delete_object(Bucket=BUCKET_NAME, Key=key)
            r2_client.delete_object(Bucket=BUCKET_NAME, Key=pending_key)
        except Exception:
            pass
        invalidate_usage(user_id)
        return json.dumps(dict(error, deleted=relative))

    # An unfinished upload that was replacing a file leaves the OLD file at
    # the path. Same size as the one it replaced and not the size declared
    # means nothing new has arrived.
    if actual == pending.get("replaced_bytes") and actual != declared:
        return json.dumps({
            "error": "The file at that path is still the one that was there "
                     "before; the upload has not arrived.", "path": relative})
    if actual > declared or actual > MAX_FILE_BYTES:
        return reject({
            "error": f"The uploaded file is {actual} bytes, more than the "
                     f"{declared} declared, so it was removed.",
            "hint": "Request a new link with the real size."})
    used, _ = user_usage(user_id, use_cache=False)
    if used > MAX_USER_BYTES:
        return reject({
            "error": "The uploaded file took the account over its storage "
                     "limit, so it was removed.",
            "used_gb": round(used / GB, 3),
            "limit_gb": MAX_USER_BYTES // GB})

    try:
        r2_client.delete_object(Bucket=BUCKET_NAME, Key=pending_key)
    except Exception:
        pass
    out = {"ok": True, "path": relative, "client": pending["client"],
           "size_bytes": actual, "mime_type": head.get("ContentType"),
           "used_gb": round(used / GB, 3)}
    if actual < declared:
        out["warning"] = (f"Only {actual} of the {declared} declared bytes "
                          "arrived. If the upload was cut short, upload it "
                          "again with a new link.")
    return json.dumps(out)
