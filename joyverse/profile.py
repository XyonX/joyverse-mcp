import json
from joyverse.config import r2_client, BUCKET_NAME, get_profile_key

def get_profile(user: dict) -> str:
    """Reads the user profile from R2."""
    username = user["username"]
    try:
        key = get_profile_key(username)
    except ValueError as e:
        return json.dumps({"error": f"Invalid username: {e}"})

    try:
        response = r2_client.get_object(Bucket=BUCKET_NAME, Key=key)
        return response["Body"].read().decode("utf-8")
    except Exception as e:
        if "NoSuchKey" in str(e):
            return json.dumps(
                {"error": f"No profile found for {username}. "
                          f"Create one with update_profile."})
        return json.dumps({"error": f"R2 error: {str(e)}"})

def update_profile(field: str, value: str, user: dict) -> str:
    """Writes or updates a field in the profile on R2.

    Creates the profile if it does not exist yet, so a brand-new user can be
    onboarded through this tool alone. A field may be a plain key ("age") or a
    section header ("Identity" / "## Identity"), in which case the value is
    written as a block under that section.
    """
    username = user["username"]
    try:
        key = get_profile_key(username)
    except ValueError as e:
        return f"Error: Invalid username: {e}"

    try:
        response = r2_client.get_object(Bucket=BUCKET_NAME, Key=key)
        content = response["Body"].read().decode("utf-8")
    except Exception as e:
        if "NoSuchKey" in str(e):
            # No profile yet -- seed one and fall through so the first call
            # creates it instead of failing.
            content = "# User Profile\n"
        else:
            return f"Error: R2 error: {str(e)}"

    field = field.strip()
    if not field:
        return "Error: Field name cannot be empty."

    # A section-only field means "write this block under that header".
    # LLMs naturally try this when building a profile from scratch. We only
    # treat it as a section when it is a plausible heading (Title Case, or
    # already spelled with a leading '#'), so ordinary keys like "age" or
    # "current role" keep the key:value behaviour.
    bare = field.lstrip("#").strip()
    looks_like_heading = field.startswith("#") or (
        bare.isidentifier()
        and bare[:1].isupper()
        and not any(c in bare for c in " \t")
    )
    is_section = looks_like_heading

    lines = content.split("\n")

    if is_section:
        header = f"## {field}"
        if not any(line.strip() == header for line in lines):
            while lines and not lines[-1].strip():
                lines.pop()
            if lines and lines[0].startswith("# "):
                lines.extend(["", header, ""])
            else:
                lines = [f"# User Profile", "", header, ""]
        for vline in value.split("\n"):
            lines.append(vline)
        new_content = "\n".join(lines)
        result_msg = f"Updated section '{field}'"
    else:
        # Parse by sections and update exact key matches
        updated = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(f"{field}:") and not stripped.startswith("#"):
                # Ensure it's a key:value line (not a comment or header)
                if ":" in stripped and stripped.index(":") == len(field):
                    lines[i] = f"{field}: {value}"
                    updated = True
                    break

        if not updated:
            lines.append(f"{field}: {value}")
        new_content = "\n".join(lines)
        result_msg = f"Updated {field} to: {value}"

    try:
        r2_client.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=new_content.encode("utf-8"),
            ContentType="text/markdown"
        )
        return result_msg
    except Exception as e:
        return f"Error writing to R2: {str(e)}"