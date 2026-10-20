import copy
import json
from datetime import datetime
from joyverse.config import r2_client, BUCKET_NAME, get_memory_key

DEFAULT_MEMORY = {
    "personality": [],
    "observed_patterns": [],
    "preferences": {"explanation_style": "", "code_style": "", "feedback_style": ""},
    "current_context": {"main_focus": "", "immediate_next": "", "mood": ""},
    "last_updated": ""
}

def _load_memory(user_id: str) -> dict:
    key = get_memory_key(user_id)
    try:
        response = r2_client.get_object(Bucket=BUCKET_NAME, Key=key)
        return json.loads(response["Body"].read().decode("utf-8"))
    except Exception as e:
        if "NoSuchKey" in str(e):
            fresh = copy.deepcopy(DEFAULT_MEMORY)
            _save_memory(user_id, fresh)
            return fresh
        raise e

def _save_memory(user_id: str, data: dict):
    key = get_memory_key(user_id)
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    r2_client.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=json.dumps(data, indent=2).encode("utf-8"),
        ContentType="application/json"
    )

def get_memory(user: dict) -> str:
    user_id = user["user_id"]
    return json.dumps(_load_memory(user_id))

def add_memory_trait(trait: str, user: dict) -> str:
    user_id = user["user_id"]
    memory = _load_memory(user_id)
    if len(memory["personality"]) >= 20:
        memory["personality"].pop(0)
    memory["personality"].append(trait)
    _save_memory(user_id, memory)
    return f"Added trait: {trait}"

def update_focus(focus: str, user: dict) -> str:
    user_id = user["user_id"]
    memory = _load_memory(user_id)
    memory["current_context"]["main_focus"] = focus
    _save_memory(user_id, memory)
    return f"Updated focus to: {focus}"