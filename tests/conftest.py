"""Shared fixtures: a fake in-memory R2, JWT helpers, and a TestClient."""
import io
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# auth.py reads JWT_SECRET at import time and only sees the real .env when
# joyverse.config (which calls load_dotenv) was imported first. Do that here so
# every test module authenticates with the same secret regardless of ordering.
os.environ.setdefault("JWT_SECRET", "test-secret-not-a-real-one")

from joyverse import config as jv_config  # noqa: E402
import joyverse.auth as jv_auth  # noqa: E402

TEST_SECRET = "test-secret-not-a-real-one"
TEST_BUCKET = "test-bucket"


@pytest.fixture
def anyio_backend():
    """Pin anyio to asyncio only -- trio is not a dependency."""
    return "asyncio"


class NoSuchKey(Exception):
    """Stands in for the botocore ClientError the real R2 raises on a miss."""


class FakeR2:
    """Minimal in-memory stand-in for the boto3 S3 client.

    Records every write so tests can assert on the exact keys written.
    """

    def __init__(self):
        self.store = {}   # key -> bytes
        self.puts = []    # list of kwargs dicts

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise NoSuchKey(f"NoSuchKey: The specified key does not exist. {Key}")
        return {"Body": io.BytesIO(self.store[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        if isinstance(Body, str):
            Body = Body.encode("utf-8")
        self.store[Key] = Body
        self.puts.append({"Bucket": Bucket, "Key": Key,
                          "Body": Body, "ContentType": ContentType})
        return {}

    def seed(self, key, content):
        """Pre-populate an object (str or bytes)."""
        if isinstance(content, str):
            content = content.encode("utf-8")
        self.store[key] = content

    def get_json(self, key):
        return json.loads(self.store[key].decode("utf-8"))


@pytest.fixture
def fake_r2(monkeypatch):
    """Patch the r2_client reference into every module that imported it.

    Each module did `from joyverse.config import r2_client`, so rebinding
    config's attribute alone would not update their local names.
    """
    fake = FakeR2()
    modules = ["config", "profile", "memory", "data"]
    for name in modules:
        mod = __import__(f"joyverse.{name}", fromlist=["r2_client"])
        monkeypatch.setattr(mod, "r2_client", fake, raising=False)
    monkeypatch.setattr(jv_config, "r2_client", fake, raising=False)
    return fake


@pytest.fixture
def make_token():
    """Build a signed JWT for arbitrary claims."""
    import jwt as pyjwt

    def _make(username="joydip", secret=None, exp_delta=3600, **extra):
        import time
        payload = {"username": username, "exp": int(time.time()) + exp_delta}
        payload.update(extra)
        return pyjwt.encode(payload, secret or TEST_SECRET, algorithm="HS256")

    return _make


@pytest.fixture
def valid_token(make_token):
    return make_token("joydip")


@pytest.fixture
def client_factory():
    """Build a TestClient around a freshly-configured MCPServer.

    Deliberately does NOT import run.py, which builds a real server and real
    boto3 client at import time.
    """
    from fastapi.testclient import TestClient
    from mcppro import MCPServer

    def _build(auth=None, instructions=""):
        srv = MCPServer(name="test-server", version="9.9.9",
                        auth=auth, instructions=instructions)
        return TestClient(srv._app), srv

    return _build


@pytest.fixture
def sse_rpc():
    """POST a JSON-RPC body and return the decoded SSE payload."""
    def _rpc(client, method, params=None, req_id=1, token=None):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            body["params"] = params
        resp = client.post("/mcp", json=body, headers=headers)
        if resp.status_code != 200:
            return resp
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        return None
    return _rpc
