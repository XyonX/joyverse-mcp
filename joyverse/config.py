import os
from dotenv import load_dotenv
import boto3
from botocore.config import Config

load_dotenv()

# R2 Configuration
ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID")
ACCESS_KEY = os.getenv("CLOUDFLARE_R2_ACCESS_KEY_ID")
SECRET_KEY = os.getenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY")
BUCKET_NAME = os.getenv("CLOUDFLARE_R2_BUCKET_NAME")

_r2_client = None

def get_r2_client():
    """Lazy-initialized R2 client. Allows server to start without R2 creds."""
    global _r2_client
    if _r2_client is None:
        if not all([ACCOUNT_ID, ACCESS_KEY, SECRET_KEY, BUCKET_NAME]):
            raise RuntimeError("R2 credentials not configured. Set CLOUDFLARE_* env vars.")
        _r2_client = boto3.client(
            "s3",
            endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
            config=Config(signature_version="s3v4"),
            region_name="auto"
        )
    return _r2_client

# For backward compat with tests that monkeypatch r2_client
# Use a callable that can be replaced
class _R2ClientProxy:
    def get_object(self, **kwargs):
        return get_r2_client().get_object(**kwargs)
    def put_object(self, **kwargs):
        return get_r2_client().put_object(**kwargs)

r2_client = _R2ClientProxy()

# ==========================================
# DYNAMIC KEY BUILDERS (Multi-user)
# ==========================================

def _safe_segment(value: str) -> str:
    """Rejects path-traversal attempts in a user-supplied key segment.

    Mirrors the check in jwt_auth: without it, a topic like '../../other'
    would build an R2 key escaping the user's own users/<name>/ prefix.
    """
    if "/" in value or "\\" in value or ".." in value or value.strip() == "":
        raise ValueError(f"Invalid key segment: {value!r}")
    return value


def get_profile_key(username: str) -> str:
    return f"users/{_safe_segment(username)}/profile.md"

def get_bio_key(username: str) -> str:
    return f"users/{_safe_segment(username)}/bio.md"

def get_memory_key(username: str) -> str:
    return f"users/{_safe_segment(username)}/memory.json"

def get_data_key(username: str, topic: str) -> str:
    return f"users/{_safe_segment(username)}/data/{_safe_segment(topic)}/progress.json"