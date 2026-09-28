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
            return json.dumps({"error": f"No profile found for {username}. Run setup_profile first."})
        return json.dumps({"error": f"R2 error: {str(e)}"})

def update_profile(field: str, value: str, user: dict) -> str:
    """Updates a specific field in the profile on R2."""
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
            return "Error: Profile does not exist. Run setup_profile first."
        return f"Error: R2 error: {str(e)}"
    
    lines = content.split("\n")
    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{field}:"):
            lines[i] = f"{field}: {value}"
            updated = True
            break
    
    if not updated:
        lines.append(f"{field}: {value}")
    
    new_content = "\n".join(lines)
    
    try:
        r2_client.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=new_content.encode("utf-8"),
            ContentType="text/markdown"
        )
        return f"Updated {field} to: {value}"
    except Exception as e:
        return f"Error writing to R2: {str(e)}"