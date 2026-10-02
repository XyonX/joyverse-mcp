"""Path construction for stored files.

Files live at:

    users/<user_id>/storage/<client>/<relative path>

Two rules, and only the first is negotiable:

* The client root is ENFORCED. A client may write anywhere inside its own
  folder and nowhere else. This is the security boundary.
* Sub-folders are FREE. Agents organise as they like -- images/, renders/,
  docs/ -- because no fixed taxonomy anticipates what they will produce.

Path traversal is handled by resolving the path and then checking that the
result is still inside the root, rather than by searching the string for
"..". Rejecting a substring misses percent-encoded forms, doubled separators
and mid-path segments; resolving cannot be fooled that way.
"""
import posixpath
import re
from typing import Tuple

from joyverse.config import STORAGE_ROOT

# Client names become path segments, so they are deliberately narrow:
# lowercase, no separators, no dots-only, bounded length.
CLIENT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")

# Characters allowed in a client-relative path. Deliberately excludes
# backslash so a Windows-style separator cannot survive normalisation.
PATH_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")

MAX_PATH_DEPTH = 12
MAX_PATH_LENGTH = 512


def normalise_client_name(name: str) -> str:
    """Validate and canonicalise a client name.

    Case-folded so `ChatGPT` and `chatgpt` are one client rather than two
    folders that later need reconciling.
    """
    if not isinstance(name, str):
        raise ValueError("Client name must be a string.")
    cleaned = name.strip().lower()
    if not cleaned:
        raise ValueError("Client name cannot be empty.")
    if cleaned == "_registry" or cleaned == STORAGE_ROOT:
        raise ValueError(f"Client name '{cleaned}' is reserved.")
    if not CLIENT_NAME_PATTERN.match(cleaned):
        raise ValueError(
            "Client name must be 2-32 characters: letters, digits, dot, "
            "underscore or hyphen, starting with a letter or digit.")
    return cleaned


def storage_prefix(user_id: str) -> str:
    return f"users/{user_id}/{STORAGE_ROOT}/"


def client_prefix(user_id: str, client: str) -> str:
    return f"{storage_prefix(user_id)}{normalise_client_name(client)}/"


def _normalise_relative(path: str) -> str:
    """Resolve a client-relative path, rejecting anything that escapes.

    Returns the cleaned relative path. Raises ValueError on traversal, on an
    absolute path, or on a segment that is not a plain name.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("File path cannot be empty.")

    raw = path.strip().replace("\\", "/")

    if raw.startswith("/"):
        raise ValueError("File path must be relative to the client folder.")

    segments = [s for s in raw.split("/") if s not in ("", ".")]

    # ".." is rejected outright rather than resolved, so a path that *tries*
    # to escape is reported instead of quietly clamped back inside.
    for segment in segments:
        if segment == "..":
            raise ValueError("File path may not contain '..'.")
        if not PATH_SEGMENT_PATTERN.match(segment):
            raise ValueError(
                f"Invalid path segment {segment!r}. Use letters, digits, "
                "dot, underscore, hyphen or space.")

    if not segments:
        raise ValueError("File path resolves to nothing.")

    if len(segments) > MAX_PATH_DEPTH:
        raise ValueError(
            f"File path is too deep (max {MAX_PATH_DEPTH} folders).")
    if len("/".join(segments)) > MAX_PATH_LENGTH:
        raise ValueError(f"File path is too long (max {MAX_PATH_LENGTH}).")

    return "/".join(segments)


def resolve_file_path(user_id: str, client: str, path: str) -> Tuple[str, str]:
    """Return (full R2 key, client-relative path).

    Defence in depth: the path is validated segment by segment, then the
    assembled key is checked for containment. Either check alone would do;
    both together mean a bug in one is not exploitable.
    """
    clean_client = normalise_client_name(client)
    relative = _normalise_relative(path)

    key = f"{client_prefix(user_id, clean_client)}{relative}"

    root = client_prefix(user_id, clean_client)
    normalised_key = posixpath.normpath(key)
    if not normalised_key.startswith(root):
        raise ValueError("Resolved path escaped the client folder.")
    if posixpath.isabs(normalised_key) or ".." in normalised_key.split("/"):
        raise ValueError("Resolved path escaped the client folder.")

    return normalised_key, relative


def is_inside_storage(user_id: str, key: str) -> bool:
    """True when `key` sits under this user's storage root.

    Used when listing or reading, so a crafted key cannot read outside.
    """
    root = storage_prefix(user_id)
    normalised = posixpath.normpath(key)
    return normalised.startswith(root) and ".." not in normalised.split("/")
