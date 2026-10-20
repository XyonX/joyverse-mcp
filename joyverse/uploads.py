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
    check_capacity, existing_size, invalidate_usage, user_usage,
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
    if not client:
        return json.dumps({"error": "client is required.",
                           "hint": "Call register_client first."})
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
