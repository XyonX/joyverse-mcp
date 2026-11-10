import json
from joyverse.atomic import (
    Conflict, get_versioned, is_not_found, put_versioned, retry_on_conflict,
)
from joyverse.config import (
    r2_client, BUCKET_NAME, get_profile_key, ensure_doc_size,
)

# Facts that are a single value, not a block. A model often capitalises them
# ("Name", "Age"), and a capitalised word otherwise reads as a section
# heading -- which turned `Name = Joy` into an empty-looking "## Name"
# section instead of the `name: Joy` line every reader expects.
_SCALAR_KEYS = {
    "name", "age", "location", "occupation", "email", "phone", "timezone",
    "pronouns", "birthday", "nationality", "languages", "language", "city",
    "country", "company", "role", "website", "github", "linkedin", "gender",
}

# New key:value facts are filed here when the section exists, rather than at
# the end of the file under whatever section happens to be last.
_FACTS_SECTION = "identity"


def _is_header(line: str) -> bool:
    return line.strip().startswith("## ")


def _header_name(line: str) -> str:
    return line.strip()[3:].strip()


def _find_key_line(lines, field: str):
    """Index of the `field: value` line, matched case-insensitively."""
    wanted = field.lower()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or ":" not in stripped:
            continue
        if stripped.split(":", 1)[0].strip().lower() == wanted:
            return i
    return None


def get_profile(user: dict) -> str:
    """Reads the user profile from R2."""
    user_id = user["user_id"]
    try:
        key = get_profile_key(user_id)
    except ValueError as e:
        return json.dumps({"error": f"Invalid user_id: {e}"})

    try:
        response = r2_client.get_object(Bucket=BUCKET_NAME, Key=key)
        return response["Body"].read().decode("utf-8")
    except Exception as e:
        if is_not_found(e):
            return json.dumps(
                {"error": f"No profile found for {user_id}. "
                          f"Create one with update_profile."})
        return json.dumps({"error": f"R2 error: {str(e)}"})

def _insert_fact(lines, fact: str):
    """Add a new key:value line where facts live.

    Inside the Identity section when there is one; otherwise at the end, as
    before. Appending blindly put a new fact under whichever section came
    last -- usually a paragraph of narrative.
    """
    start = None
    for i, line in enumerate(lines):
        if _is_header(line) and _header_name(line).lower() == _FACTS_SECTION:
            start = i
            break
    if start is None:
        return lines + [fact]

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _is_header(lines[j]):
            end = j
            break
    # Step back over the blank lines that separate this section from the next.
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    return lines[:insert_at] + [fact] + lines[insert_at:]


@retry_on_conflict
def update_profile(field: str, value: str, user: dict) -> str:
    """Writes or updates a field in the profile on R2.

    Creates the profile if it does not exist yet, so a brand-new user can be
    onboarded through this tool alone. A field may be a plain key ("age") or a
    section header ("Identity" / "## Identity"), in which case the value is
    written as a block under that section.
    """
    user_id = user["user_id"]
    try:
        key = get_profile_key(user_id)
    except ValueError as e:
        return f"Error: Invalid user_id: {e}"

    try:
        raw, version = get_versioned(r2_client, BUCKET_NAME, key)
    except Exception as e:
        return f"Error: R2 error: {str(e)}"
    # No profile yet -- seed one and fall through so the first call creates
    # it instead of failing.
    content = raw.decode("utf-8") if raw is not None else "# User Profile\n"

    field = field.strip()
    if not field:
        return "Error: Field name cannot be empty."

    # A section-only field means "write this block under that header".
    # LLMs naturally try this when building a profile from scratch. We only
    # treat it as a section when it is a plausible heading (Title Case, or
    # already spelled with a leading '#'), so ordinary keys like "age" or
    # "current role" keep the key:value behaviour.
    bare = field.lstrip("#").strip()
    if not bare:
        return "Error: Field name cannot be empty."
    value = str(value)
    lines = content.split("\n")

    # An explicit '#' always means a section. Otherwise a capitalised word is
    # a section UNLESS it names a single-value fact: one the profile already
    # holds as a key line, or a well-known one like Name or Age.
    if field.startswith("#"):
        is_section = True
    else:
        is_section = (
            bare.isidentifier()
            and bare[:1].isupper()
            and not any(c in bare for c in " \t")
            and bare.lower() not in _SCALAR_KEYS
            and _find_key_line(lines, bare) is None
        )
    if not is_section:
        field = bare

    if is_section:
        # Built from the bare name: "## Identity" used to come out as
        # "## ## Identity". An existing header is matched case-insensitively
        # and keeps its own spelling, so "identity" cannot fork "Identity".
        header = f"## {bare}"
        start = None
        for i, line in enumerate(lines):
            if _is_header(line) and \
                    _header_name(line).lower() == bare.lower():
                start = i
                header = line.strip()
                break

        if start is None:
            # New section: append it at the end.
            while lines and not lines[-1].strip():
                lines.pop()
            if lines and lines[0].startswith("# "):
                lines.extend(["", header, ""])
            else:
                lines = ["# User Profile", "", header, ""]
            lines.extend(value.split("\n"))
        else:
            # Existing section: replace its body, up to the next header.
            end = len(lines)
            for j in range(start + 1, len(lines)):
                if lines[j].strip().startswith("## "):
                    end = j
                    break
            lines = lines[:start + 1] + [""] + value.split("\n") + lines[end:]
        new_content = "\n".join(lines)
        result_msg = f"Updated section '{bare}'"
    else:
        # A key keeps the spelling it already has, so "Age" updates the
        # existing "age:" line instead of adding a second one beside it.
        existing = _find_key_line(lines, field)
        if existing is not None:
            label = lines[existing].strip().split(":", 1)[0].strip()
            lines[existing] = f"{label}: {value}"
        else:
            if field.lower() in _SCALAR_KEYS:
                field = field.lower()
            lines = _insert_fact(lines, f"{field}: {value}")
        new_content = "\n".join(lines)
        result_msg = f"Updated {field} to: {value}"

    body = new_content.encode("utf-8")
    try:
        ensure_doc_size(body, "profile")
    except ValueError as e:
        return f"Error: {e}"

    try:
        put_versioned(r2_client, BUCKET_NAME, key, body, version,
                      ContentType="text/markdown")
        return result_msg
    except Conflict:
        raise
    except Exception as e:
        return f"Error writing to R2: {str(e)}"