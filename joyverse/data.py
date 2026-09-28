import json
from joyverse.config import r2_client, BUCKET_NAME, get_data_key

def get_data(topic: str, user: dict) -> str:
    username = user["username"]
    try:
        key = get_data_key(username, topic)
    except ValueError as e:
        return json.dumps({"error": f"Invalid topic: {e}"})
    try:
        response = r2_client.get_object(Bucket=BUCKET_NAME, Key=key)
        return response["Body"].read().decode("utf-8")
    except Exception as e:
        if "NoSuchKey" in str(e):
            return json.dumps({"error": f"No data found for topic: {topic}"})
        return json.dumps({"error": f"R2 error: {str(e)}"})

def update_data(topic: str, data: str, user: dict) -> str:
    username = user["username"]
    try:
        key = get_data_key(username, topic)
    except ValueError as e:
        return f"Error: Invalid topic: {e}"
    try:
        parsed = json.loads(data)
        r2_client.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=json.dumps(parsed, indent=2).encode("utf-8"),
            ContentType="application/json"
        )
        return f"Updated data for {topic}"
    except json.JSONDecodeError:
        return "Error: Invalid JSON data"
    except Exception as e:
        return f"Error writing to R2: {str(e)}"