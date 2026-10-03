"""Client registration and file storage.

Clients are shallowly identified: an agent just says `client="chatgpt"`. The
opaque client_id exists only so a rename later does not require moving files.

Access is never granted by client identity -- it always comes from the OAuth
token's user_id. Registering as "chatgpt" therefore reaches only your own
files, never anyone else's, whether or not that name was free to claim.
"""
import hashlib
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional
from urllib.parse import urlparse

from joyverse.config import (
    MAX_FILE_BYTES, MAX_USER_BYTES, FETCH_TIMEOUT_SECONDS, MAX_REDIRECTS,
    BUCKET_NAME, MB, GB, STORAGE_ROOT,
)
from joyverse.paths import (
    client_prefix, normalise_client_name, resolve_file_path, storage_prefix,
)

# Internal metadata objects live under these names inside the storage root.
# They are excluded from user-facing listings: an agent has no use for the
# client registry and must not be able to fetch it as if it were a file.
CLIENTS_FILE = "_clients.json"
INTERNAL_FILES = (CLIENTS_FILE,)


def clients_key(user_id: str) -> str:
    """Registry key, scoped to the user.

    A single shared registry was the original design and it leaked: list_clients
    returned every user's client names, and register_client rejected a name
    another user had already claimed, so one account could deny another the
    name "chatgpt". Each user now owns its own registry under its own prefix.
    """
    return f"users/{user_id}/{STORAGE_ROOT}/{CLIENTS_FILE}"

# Usage is cached because a full scan on every upload would be wasteful, and
# invalidated on any write or delete so it cannot drift for long.
_USAGE_CACHE: Dict[str, tuple] = {}
USAGE_TTL_SECONDS = 60


# ==========================================
# ERROR REPORTING
# ==========================================

class StorageError(Exception):
    """Raised with a JSON-ready payload so tools can return it verbatim."""

    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__(payload.get("error", "storage error"))


def _over_limit(scope: str, used: int, limit: int, size: int, detail: str):
    used_gb = round(used / GB, 2)
    limit_gb = round(limit / GB, 2)
    size_mb = round(size / MB, 1)
    raise StorageError({
        "error": "Storage limit reached",
        "scope": scope,
        "used_gb": used_gb,
        "limit_gb": limit_gb,
        "file_mb": size_mb,
        "detail": detail,
        "hint": "Delete old files with delete_file, or raise "
                "JOYVERSE_MAX_USER_GB.",
    })


def _too_big(size: int):
    raise StorageError({
        "error": "File too large",
        "file_mb": round(size / MB, 1),
        "limit_mb": MAX_FILE_BYTES // MB,
        "hint": "Files must be under JOYVERSE_MAX_FILE_MB.",
    })


# ==========================================
# CLIENT REGISTRY
# ==========================================

def _load_clients(user_id: str) -> dict:
    from joyverse.config import r2_client

    try:
        body = r2_client.get_object(Bucket=BUCKET_NAME, Key=clients_key(user_id))
        return json.loads(body["Body"].read().decode("utf-8"))
    except Exception as e:
        if "NoSuchKey" in str(e) or "does not exist" in str(e):
            return {"clients": {}}
        raise


def _save_clients(user_id: str, data: dict):
    from joyverse.config import r2_client

    r2_client.put_object(Bucket=BUCKET_NAME, Key=clients_key(user_id),
                          Body=json.dumps(data, indent=2).encode("utf-8"),
                          ContentType="application/json")


def register_client(name: str, platform: Optional[str] = None,
                    user: dict = None) -> str:
    """Register a client name for this user. Returns the storage root path.

    Names are unique per user: a second claim of the same name is rejected
    rather than silently attaching to the existing folder, so a client
    cannot quietly take over another's identity.
    """
    user_id = user["user_id"]
    try:
        clean = normalise_client_name(name)
    except ValueError as e:
        return json.dumps({"error": f"Invalid client name: {e}"})

    registry = _load_clients(user_id)
    existing = registry["clients"].get(clean)
    if existing:
        return json.dumps({
            "error": "Client name already registered",
            "name": clean,
            "client_id": existing["client_id"],
            # This hint used to say "choose a different name", which
            # instructed the agent to do exactly the wrong thing: every
            # collision then produced another client and split the user's
            # files across folders. The fix is to reuse, never to rename.
            "hint": f"You already have a client called '{clean}'. Reuse it as "
                    f"client=\"{clean}\" -- do not register a new name. Call "
                    "list_clients to see all of your clients, and pick the one "
                    "matching the product you are.",
        })

    # Opaque, derived from the name but not reversible into anything usable.
    client_id = "c_" + hashlib.sha256(
        f"{user_id}:{clean}".encode()).hexdigest()[:12]

    # platform was stored verbatim, so the same product accumulated spellings
    # like "chatgpt", "ChatGPT" and "claude.ai". It is descriptive only and
    # never an identity, so folding it costs nothing.
    clean_platform = platform.strip().lower() if isinstance(platform, str) \
        and platform.strip() else None

    registry["clients"][clean] = {
        "client_id": client_id,
        "name": clean,
        "platform": clean_platform,
        "first_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_clients(user_id, registry)

    return json.dumps({
        "ok": True,
        "client_id": client_id,
        "name": clean,
        "platform": clean_platform,
        "storage_path": client_prefix(user_id, clean),
    })


def list_clients(user: dict) -> str:
    """List this user's clients only.

    Takes the user from the token, never from a parameter, so one user cannot
    enumerate another's registrations by naming them.
    """
    user_id = user["user_id"]
    clients = _load_clients(user_id)["clients"]
    for name, entry in list(clients.items()):
        clients[name] = dict(entry, storage_path=client_prefix(user_id, name))
    return json.dumps({
        "clients": clients,
        "count": len(clients),
        "note": "Client names are yours alone. Another user may hold the same "
                "name without any overlap in files.",
    }, indent=2)


# ==========================================
# USAGE ACCOUNTING
# ==========================================

def _scan(prefix: str) -> tuple:
    """Total bytes and object count under a prefix, following pagination.

    R2 returns at most 1000 keys per call, so the loop is driven by the
    response's own IsTruncated/NextContinuationToken rather than by
    assuming a fixed page size.
    """
    from joyverse.config import r2_client

    total, count, token = 0, 0, None
    while True:
        kwargs = {"Bucket": BUCKET_NAME, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = r2_client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            total += obj.get("Size", 0)
            count += 1
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            break
    return total, count


def user_usage(user_id: str, use_cache: bool = True) -> tuple:
    """Bytes used and object count under this user's storage root."""
    now = time.time()
    if use_cache:
        hit = _USAGE_CACHE.get(user_id)
        if hit and now - hit[0] < USAGE_TTL_SECONDS:
            return hit[1], hit[2]
    total, count = _scan(storage_prefix(user_id))
    _USAGE_CACHE[user_id] = (now, total, count)
    return total, count


def invalidate_usage(user_id: str):
    _USAGE_CACHE.pop(user_id, None)


def existing_size(key: str) -> int:
    """Size of the object already at `key`, or 0.

    Overwriting is not a new file. Without this, replacing a 400 MB file with
    another 400 MB file counted as 800 MB and wrongly refused a user who had
    room for the replacement.
    """
    from joyverse.config import r2_client

    try:
        return r2_client.head_object(Bucket=BUCKET_NAME,
                                     Key=key).get("ContentLength", 0) or 0
    except Exception:
        return 0


def check_capacity(user_id: str, incoming: int, replacing: int = 0):
    """Reject an upload that would breach the per-file or per-user limit.

    `replacing` is the size of the object being overwritten, credited back
    before the total is compared.
    """
    if incoming > MAX_FILE_BYTES:
        _too_big(incoming)
    used, _ = user_usage(user_id)
    if used - replacing + incoming > MAX_USER_BYTES:
        _over_limit("user", used, MAX_USER_BYTES, incoming,
                    "This file would exceed your total storage limit.")
    return used


# ==========================================
# URL FETCHING
# ==========================================

# Blocked before any connection is made. A fetch tool pointed at these would
# reach cloud instance metadata or services on the private network.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # incl. 169.254.169.254
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT / Tailscale
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),         # unique local
    ipaddress.ip_network("fe80::/10"),        # link local
]


def _validate_url(url: str):
    """Reject anything that is not a public http(s) URL.

    Resolving the host is required: a hostname can resolve to a private
    address, and a redirect can move to one after this check, so the same
    guard is reapplied at every hop.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise StorageError({
            "error": "Unsupported URL scheme",
            "scheme": parsed.scheme,
            "hint": "Only http and https URLs can be fetched.",
        })
    host = parsed.hostname
    if not host:
        raise StorageError({"error": "URL has no host"})

    try:
        infos = socket.getaddrinfo(host, parsed.port or
                                   (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise StorageError({"error": f"Could not resolve host: {host}"})

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_loopback or addr.is_link_local or addr.is_private \
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified:
            raise StorageError({
                "error": "Refusing to fetch a non-public address",
                "host": host,
                "address": str(addr),
                "hint": "Loopback, private and link-local addresses are "
                        "blocked to prevent internal network access.",
            })
        for net in _BLOCKED_NETWORKS:
            if addr in net:
                raise StorageError({
                    "error": "Refusing to fetch a non-public address",
                    "host": host,
                    "address": str(addr),
                    "hint": "Loopback, private and link-local addresses are "
                            "blocked to prevent internal network access.",
                })
    return parsed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse automatic redirects so each hop can be re-validated."""

    def redirect_request(self, *a, **k):
        return None


def _open(url: str, timeout: int = FETCH_TIMEOUT_SECONDS, method: str = "GET"):
    """Open a URL with redirects handled manually and re-checked each hop.

    `method` matters for correctness, not just politeness: probe_url must send
    HEAD, or it downloads the entire body just to learn the size.
    """
    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _validate_url(current)
        req = urllib.request.Request(current, method=method,
                                     headers={"User-Agent": "joyverse-mcp/1.0"})
        try:
            return opener.open(req, timeout=timeout), current
        except urllib.error.HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise StorageError({
                    "error": f"Could not fetch URL (HTTP {e.code})",
                    "url": current,
                })
            location = e.headers.get("Location")
            if not location:
                raise StorageError({"error": "Redirect with no Location",
                                    "url": current})
            current = urllib.parse.urljoin(current, location)
    raise StorageError({
        "error": "Too many redirects",
        "limit": MAX_REDIRECTS,
    })


def probe_url(url: str) -> tuple:
    """HEAD the URL to learn type and size without downloading the body.

    Lets an oversized or wrong-typed file be rejected in milliseconds rather
    than after pulling hundreds of megabytes.
    """
    resp, final = _open(url, timeout=FETCH_TIMEOUT_SECONDS, method="HEAD")
    try:
        size = resp.headers.get("Content-Length")
        return (int(size) if size and size.isdigit() else None,
                resp.headers.get("Content-Type"), final)
    finally:
        resp.close()


class _StreamReader:
    """File-like wrapper so the HTTP body streams straight into R2.

    boto3 accepts a readable stream as Body and uploads it in multipart
    chunks. The previous version accumulated every chunk in a list and joined
    it, so a 500 MB file was held in memory in full -- and the docstring claimed
    otherwise.
    """

    def __init__(self, resp, limit: int):
        self._resp = resp
        self._limit = limit
        self._read = 0
        self._primed = None

    def read(self, size=-1):
        # The emptiness check primes the stream, and the caller has already
        # consumed that chunk. Returning it again is what keeps the uploaded
        # bytes identical to the source -- without this the first chunk was
        # silently dropped and every file was truncated by up to 256 KB.
        if self._primed is not None:
            chunk, self._primed = self._primed, None
            return chunk
        chunk = self._resp.read(1024 * 256 if size is None or size < 0 else size)
        self._read += len(chunk)
        if self._read > self._limit:
            raise StorageError({
                "error": "File too large",
                "limit_mb": self._limit // MB,
                "detail": "Stopped mid-download; nothing was stored.",
            })
        return chunk

    def prime(self):
        """Read once to detect an empty body, keeping the chunk for R2."""
        self._primed = self.read()
        return bool(self._primed)


def fetch_to_r2(url: str, key: str, metadata: dict,
                max_bytes: int = MAX_FILE_BYTES):
    """Stream a URL straight into R2 without buffering the file in memory."""
    from joyverse.config import r2_client

    resp, final = _open(url)
    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        resp.close()
        _too_big(int(declared))

    mime = resp.headers.get("Content-Type") or "application/octet-stream"
    # A link that lands on an HTML page is almost certainly a login wall, not
    # the file that was asked for.
    if mime.startswith("text/html"):
        resp.close()
        raise StorageError({
            "error": "URL returned an HTML page, not a file",
            "url": final,
            "hint": "The link may point at a sign-in page. Pass a direct "
                    "file URL, or save the content with save_file_text.",
        })

    try:
        reader = _StreamReader(resp, max_bytes)
        if not reader.prime():
            raise StorageError({"error": "URL returned an empty body",
                                "url": final})
        r2_client.put_object(
            Bucket=BUCKET_NAME, Key=key, Body=reader,
            ContentType=mime, Metadata=metadata)
        written = reader._read
    except StorageError:
        resp.close()
        raise
    except Exception:
        resp.close()
        raise
    finally:
        resp.close()

    return written, mime, final


# ==========================================
# FILE TOOLS
# ==========================================

def _meta(client: str, source=None, mime=None, extra=None) -> dict:
    """Object metadata, stored on the object so it travels with it.

    No separate manifest to keep in sync, and list_objects_v2 returns it.
    """
    meta = {"client": client}
    if source:
        meta["source"] = source[:500]
    if mime:
        meta["mime"] = mime[:120]
    if extra:
        meta.update({k: str(v)[:200] for k, v in extra.items()})
    return meta


def save_file_from_url(url: str, path: str, client: str = None,
                       user: dict = None) -> str:
    """Fetch a URL and store the file under this client's folder.

    Works for any file type -- the bytes are moved without interpreting them,
    so an image, a document or a video all take the same path.
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
        # HEAD first: reject an oversized or wrong-typed file before pulling
        # a single byte of its body.
        replacing = existing_size(key)
        size, mime, final_url = probe_url(url)
        if size is not None:
            check_capacity(user_id, size, replacing)

        resp_meta = _meta(client, source=url, mime=mime)
        written, mime, final_url = fetch_to_r2(url, key, resp_meta)
        invalidate_usage(user_id)
    except StorageError as e:
        return json.dumps(e.payload)
    except Exception as e:
        return json.dumps({"error": f"Could not save file: {e}"})

    used, count = user_usage(user_id, use_cache=False)
    return json.dumps({
        "ok": True,
        "path": relative,
        "client": client,
        "size_bytes": written,
        "mime_type": mime,
        "source_url": final_url,
        "used_gb": round(used / GB, 3),
        "limit_gb": MAX_USER_BYTES // GB,
    })


def save_file_text(path: str, content: str, client: str = None,
                   user: dict = None) -> str:
    """Store text the agent produced itself -- no URL fetch involved."""
    from joyverse.config import r2_client

    user_id = user["user_id"]
    if not client:
        return json.dumps({"error": "client is required."})
    try:
        key, relative = resolve_file_path(user_id, client, path)
    except ValueError as e:
        return json.dumps({"error": f"Invalid path: {e}"})

    body = content.encode("utf-8")
    try:
        check_capacity(user_id, len(body), existing_size(key))
        r2_client.put_object(
            Bucket=BUCKET_NAME, Key=key, Body=body,
            ContentType="text/plain; charset=utf-8",
            Metadata=_meta(client, mime="text/plain"))
        invalidate_usage(user_id)
    except StorageError as e:
        return json.dumps(e.payload)
    except Exception as e:
        return json.dumps({"error": f"Could not save file: {e}"})

    used, _ = user_usage(user_id, use_cache=False)
    return json.dumps({"ok": True, "path": relative, "client": client,
                       "size_bytes": len(body), "mime_type": "text/plain",
                       "used_gb": round(used / GB, 3)})


def get_file(path: str, client: str = None, user: dict = None) -> str:
    """Return a time-limited download link for a stored file.

    The link is regenerated on demand, so the expiry is invisible to callers
    that ask again later.
    """
    from joyverse.config import r2_client

    user_id = user["user_id"]
    if not client:
        return json.dumps({"error": "client is required."})
    try:
        key, relative = resolve_file_path(user_id, client, path)
    except ValueError as e:
        return json.dumps({"error": f"Invalid path: {e}"})

    try:
        head = r2_client.head_object(Bucket=BUCKET_NAME, Key=key)
    except Exception:
        return json.dumps({
            "error": f"No file at {relative}",
            "client": client,
            "hint": "Call list_files to see what is stored.",
        })

    url = r2_client.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET_NAME, "Key": key},
        ExpiresIn=7 * 24 * 3600)
    return json.dumps({
        "ok": True,
        "path": relative,
        "client": client,
        "size_bytes": head.get("ContentLength"),
        "mime_type": head.get("ContentType"),
        "url": url,
        "expires_in_hours": 168,
        "note": "This link is signed and expires. Call get_file again for a "
                "fresh one.",
    })


def list_files(prefix: str = None, client: str = None,
               user: dict = None) -> str:
    """List stored files for one client, or for every client."""
    from joyverse.config import r2_client

    user_id = user["user_id"]
    scan = storage_prefix(user_id)
    if client:
        try:
            scan = client_prefix(user_id, client)
        except ValueError as e:
            return json.dumps({"error": f"Invalid client: {e}"})

    total, count = user_usage(user_id, use_cache=False)
    entries = []
    for obj in _list_all(scan):
        relative = obj["Key"][len(scan):]
        if relative in INTERNAL_FILES or any(
                relative.endswith("/" + f) for f in INTERNAL_FILES):
            continue
        if prefix and not obj["Key"].startswith(scan + prefix):
            continue
        entries.append({
            "path": relative,
            "size_bytes": obj.get("Size", 0),
            "modified": str(obj.get("LastModified", ""))[:19],
        })
    entries.sort(key=lambda e: e["path"])

    by_client = {}
    for name in _load_clients(user_id)["clients"]:
        used, _ = _scan(client_prefix(user_id, name))
        if used:
            by_client[name] = round(used / GB, 3)

    registry_bytes = 0
    try:
        registry_bytes = r2_client.head_object(
            Bucket=BUCKET_NAME, Key=clients_key(user_id)).get(
                "ContentLength", 0) or 0
    except Exception:
        pass

    return json.dumps({
        "files": entries,
        "count": len(entries),
        "usage": {
            "total_gb": round(total / GB, 3),
            "file_gb": round((total - registry_bytes) / GB, 3),
            "limit_gb": MAX_USER_BYTES // GB,
            "objects": count,
            "by_client_gb": by_client,
        },
    }, indent=2)


def delete_file(path: str, client: str = None, user: dict = None) -> str:
    """Remove a stored file and free its space."""
    from joyverse.config import r2_client

    user_id = user["user_id"]
    if not client:
        return json.dumps({"error": "client is required."})
    try:
        key, relative = resolve_file_path(user_id, client, path)
    except ValueError as e:
        return json.dumps({"error": f"Invalid path: {e}"})

    try:
        r2_client.delete_object(Bucket=BUCKET_NAME, Key=key)
        invalidate_usage(user_id)
    except Exception as e:
        return json.dumps({"error": f"Could not delete: {e}"})

    used, _ = user_usage(user_id, use_cache=False)
    return json.dumps({"ok": True, "deleted": relative,
                       "used_gb": round(used / GB, 3)})


def _list_all(prefix: str) -> List[dict]:
    """Every object under a prefix, following pagination."""
    from joyverse.config import r2_client

    out, token = [], None
    while True:
        kwargs = {"Bucket": BUCKET_NAME, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = r2_client.list_objects_v2(**kwargs)
        out.extend(page.get("Contents", []))
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            break
    return out
