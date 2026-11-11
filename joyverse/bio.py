import json
from joyverse.atomic import (
    Conflict, get_versioned, is_not_found, put_versioned, retry_on_conflict,
)
from joyverse.config import (
    r2_client, BUCKET_NAME, get_bio_key, ensure_doc_size,
)


def get_bio(user: dict) -> str:
    """Reads the user's biography from R2."""
    user_id = user["user_id"]
    try:
        key = get_bio_key(user_id)
    except ValueError as e:
        return json.dumps({"error": f"Invalid user_id: {e}"})

    try:
        response = r2_client.get_object(Bucket=BUCKET_NAME, Key=key)
        return response["Body"].read().decode("utf-8")
    except Exception as e:
        if is_not_found(e):
            return json.dumps(
                {"error": f"No bio found for {user_id}. Build it with update_bio first."})
        return json.dumps({"error": f"R2 error: {str(e)}"})


@retry_on_conflict
def update_bio(section: str, content: str, user: dict) -> str:
    """Writes or replaces a `## Section` block in the user's biography.

    The bio is narrative prose grouped under `## headers`, so it is updated a
    section at a time rather than as flat key:value pairs. If the section
    already exists its body is replaced in place; otherwise it is appended.
    """
    user_id = user["user_id"]
    try:
        key = get_bio_key(user_id)
    except ValueError as e:
        return f"Error: Invalid user_id: {e}"

    section = section.strip()
    if not section:
        return "Error: Section name cannot be empty."

    # Normalise: accept "## Journey", "Journey", or "# Journey"
    section = section.lstrip("#").strip()
    if not section:
        return "Error: Section name cannot be empty."

    try:
        raw, version = get_versioned(r2_client, BUCKET_NAME, key)
    except Exception as e:
        return f"Error: R2 error: {str(e)}"
    # No bio yet -- create one with just this section.
    content_existing = raw.decode("utf-8") if raw is not None else ""

    lines = content_existing.split("\n")
    header = f"## {section}"

    # Find the header and the extent of its block
    start = None
    # Matched case-insensitively, keeping the spelling already there, so
    # "journey" replaces "## Journey" instead of adding a second section.
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## ") and \
                stripped[3:].strip().lower() == section.lower():
            start = i
            header = stripped
            break

    if start is None:
        # Append a new section at the end
        block = [header, "", content.strip()]
        # Trim trailing blank lines so we don't accumulate them
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.extend(block)
        new_content = "\n".join(lines)
        action = f"Added section '{section}'"
    else:
        # Replace the body of the existing section
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].strip().startswith("## "):
                end = j
                break
        new_lines = lines[:start] + [header, "", content.strip()] + lines[end:]
        new_content = "\n".join(new_lines)
        action = f"Updated section '{section}'"

    body = new_content.encode("utf-8")
    try:
        ensure_doc_size(body, "bio")
    except ValueError as e:
        return f"Error: {e}"

    try:
        put_versioned(r2_client, BUCKET_NAME, key, body, version,
                      ContentType="text/markdown")
    except Conflict:
        raise
    except Exception as e:
        return f"Error writing to R2: {str(e)}"
    from joyverse import history
    history.keep(user_id, "bio", raw)
    return f"{action} ({len(content)} chars)"


@retry_on_conflict
def delete_bio_section(section: str, user: dict = None) -> str:
    """Remove one `## Section` from the bio, heading and body.

    The version before the removal is kept (see restore_version).
    """
    from joyverse import history

    user_id = user["user_id"]
    try:
        key = get_bio_key(user_id)
    except ValueError as e:
        return json.dumps({"error": f"Invalid user_id: {e}"})
    name = str(section or "").lstrip("#").strip()
    if not name:
        return json.dumps({"error": "section is required."})

    try:
        raw, version = get_versioned(r2_client, BUCKET_NAME, key)
    except Exception as e:
        return json.dumps({"error": f"R2 error: {e}"})
    if raw is None:
        return json.dumps({"error": "There is no bio yet."})
    lines = raw.decode("utf-8").split("\n")

    def is_header(line):
        return line.strip().startswith("## ")

    start = next((i for i, line in enumerate(lines) if is_header(line)
                  and line.strip()[3:].strip().lower() == name.lower()), None)
    if start is None:
        return json.dumps({
            "error": f"The bio has no section named {name!r}.",
            "sections": [line.strip()[3:].strip() for line in lines
                         if is_header(line)]})
    end = next((j for j in range(start + 1, len(lines))
                if is_header(lines[j])), len(lines))
    removed = lines[start].strip()[3:].strip()
    lines = lines[:start] + lines[end:]
    while lines and not lines[-1].strip():
        lines.pop()

    try:
        put_versioned(r2_client, BUCKET_NAME, key,
                      "\n".join(lines).encode("utf-8"), version,
                      ContentType="text/markdown")
    except Conflict:
        raise
    except Exception as e:
        return json.dumps({"error": f"Error writing to R2: {e}"})
    history.keep(user_id, "bio", raw)
    return json.dumps({"ok": True, "deleted": {"section": removed,
                                               "lines_removed": end - start},
                       "undo": "restore_version(document=\"bio\")"})
