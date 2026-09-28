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

r2_client = boto3.client(
    "s3",
    endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto"
)

# ==========================================
# DYNAMIC KEY BUILDERS (Multi-user)
# ==========================================

def get_profile_key(username: str) -> str:
    return f"users/{username}/profile.md"

def get_bio_key(username: str) -> str:
    return f"users/{username}/bio.md"

def get_memory_key(username: str) -> str:
    return f"users/{username}/memory.json"

def get_data_key(username: str, topic: str) -> str:
    return f"users/{username}/data/{topic}/progress.json"