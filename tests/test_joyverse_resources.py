"""Tests for the joyverse-side resource wiring.

mcppro/resources.py is covered by test_resources.py. This checks that the
actual joyverse:// resources are registered, resolve, and cannot be used to
reach another account.
"""
import json

import pytest

from conftest import TEST_USER_ID as UID, TEST_USER_ID_2 as UID2
from joyverse import identity


@pytest.fixture
def client(fake_r2):
    """A TestClient over the real run.py server, with a bearer token."""
    import os
    import time
    from pathlib import Path

    import jwt as pyjwt
    from dotenv import load_dotenv
    from fastapi.testclient import TestClient

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    import run

    # resolve_handle writes to the registry, which the fixture has faked.
    fake_r2.put_object(
        Bucket="test-bucket",
        Key="users/_registry/identity.json",
        Body=json.dumps({"users": {}, "handles": {}, "identities": {},
                         "emails": {}}).encode(),
        ContentType="application/json")

    token = pyjwt.encode({"handle": "restest", "exp": int(time.time()) + 300},
                         os.environ.get("JWT_SECRET", "test-secret"),
                         algorithm="HS256")
    c = TestClient(run.server._app)
    c.token = token
    c.user_id = identity.resolve_handle("restest")
    return c


def rpc(client, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    r = client.post("/mcp", json=body,
                    headers={"Authorization": f"Bearer {client.token}",
                             "Accept": "text/event-stream"})
    for line in r.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return None


def seed(client, **objs):
    for rel, body in objs.items():
        key = f"users/{client.user_id}/{rel}"
        from joyverse import profile as prof
        prof.r2_client.put_object(Bucket="test-bucket", Key=key,
                                  Body=body.encode() if isinstance(body, str) else body,
                                  ContentType="text/plain")


class TestRegistration:
    def test_capability_is_advertised(self, client):
        caps = rpc(client, "initialize")["result"]["capabilities"]
        assert caps["resources"] == {}

    def test_four_fixed_resources_are_listed(self, client):
        uris = {r["uri"] for r in
                rpc(client, "resources/list")["result"]["resources"]}
        assert {"joyverse://profile", "joyverse://bio", "joyverse://memory",
                "joyverse://topics"} <= uris

    def test_data_template_is_advertised(self, client):
        tpl = [t["uriTemplate"] for t in
               rpc(client, "resources/templates/list")["result"]["resourceTemplates"]]
        assert "joyverse://data/{topic}" in tpl

    def test_priorities_are_declared(self, client):
        got = {r["uri"]: r.get("annotations", {}).get("priority")
               for r in rpc(client, "resources/list")["result"]["resources"]}
        assert got["joyverse://profile"] == 0.9
        assert got["joyverse://bio"] == 0.8


class TestReads:
    def test_profile(self, client):
        seed(client, **{"profile.md": "# User Profile\nname: Joydip"})
        out = rpc(client, "resources/read",
                  {"uri": "joyverse://profile"})["result"]["contents"][0]
        assert "Joydip" in out["text"]
        assert out["mimeType"] == "text/markdown"

    def test_bio(self, client):
        seed(client, **{"bio.md": "## Journey\nBuilt things."})
        out = rpc(client, "resources/read",
                  {"uri": "joyverse://bio"})["result"]["contents"][0]
        assert "Built things" in out["text"]

    def test_topics_is_the_catalogue(self, client):
        seed(client, **{"data/dsa/progress.json": '{"summary":"150 problems"}'})
        out = json.loads(rpc(client, "resources/read",
                             {"uri": "joyverse://topics"})["result"]["contents"][0]["text"])
        assert "dsa" in [t["topic"] for t in out["topics"]]

    def test_data_log_through_the_template(self, client):
        seed(client, **{"data/dsa/progress.json": '{"summary":"150 problems"}'})
        out = rpc(client, "resources/read",
                  {"uri": "joyverse://data/dsa"})["result"]["contents"][0]
        assert "150 problems" in out["text"]
        assert out["mimeType"] == "application/json"


class TestLiveEnumeration:
    def test_a_real_topic_appears_in_the_list(self, client):
        seed(client, **{"data/dsa/progress.json": '{"summary":"150 problems"}'})
        uris = {r["uri"] for r in
                rpc(client, "resources/list")["result"]["resources"]}
        assert "joyverse://data/dsa" in uris

    def test_enumerated_resource_carries_the_summary(self, client):
        seed(client, **{"data/dsa/progress.json": '{"summary":"150 problems"}'})
        res = next(r for r in rpc(client, "resources/list")["result"]["resources"]
                   if r["uri"] == "joyverse://data/dsa")
        assert res["description"] == "150 problems"

    def test_a_stored_topic_can_be_read_from_the_listed_uri(self, client):
        seed(client, **{"data/dsa/progress.json": '{"summary":"150 problems"}'})
        out = rpc(client, "resources/read",
                  {"uri": "joyverse://data/dsa"})["result"]["contents"][0]
        assert "150 problems" in out["text"]


class TestTraversalIsBlocked:
    """The storage user_id comes from auth, never from the URI."""

    @pytest.mark.parametrize("uri", [
        "joyverse://data/../other",
        "joyverse://data/%2e%2e/other",
        "joyverse://data/a/b",
    ])
    def test_path_traversal_returns_resource_not_found(self, client, uri):
        res = rpc(client, "resources/read", {"uri": uri})
        assert res.get("error", {}).get("code") == -32002

    def test_slash_in_the_topic_cannot_escape(self, client):
        # {topic} must not swallow a slash
        res = rpc(client, "resources/read", {"uri": "joyverse://data/u_other/dsa"})
        assert "error" in res


class TestIsolation:
    def test_resources_read_the_callers_own_data_only(self, client, fake_r2):
        seed(client, **{"data/dsa/progress.json": '{"summary":"mine"}'})
        from joyverse import profile as prof
        prof.r2_client.seed(
            f"users/{UID2}/data/dsa/progress.json",
            '{"summary":"someone elses"}')
        out = json.loads(rpc(client, "resources/read",
                             {"uri": "joyverse://data/dsa"})["result"]["contents"][0]["text"])
        assert out["summary"] == "mine"
        assert "someone elses" not in json.dumps(out)

    def test_topic_list_is_scoped_to_the_caller(self, client, fake_r2):
        from joyverse import profile as prof
        prof.r2_client.seed(f"users/{UID2}/data/secret/progress.json", "{}")
        out = json.loads(rpc(client, "resources/read",
                             {"uri": "joyverse://topics"})["result"]["contents"][0]["text"])
        assert "secret" not in [t["topic"] for t in out["topics"]]
