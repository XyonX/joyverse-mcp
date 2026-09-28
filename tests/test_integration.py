"""End-to-end protocol tests: real JSON-RPC over HTTP, real tools, fake R2.

Exercises the full stack -- auth -> router -> tool -> R2 -- the way an MCP
client would, so these would catch a break in any layer.
"""
import json
import pytest
import jwt as pyjwt
import time

from conftest import TEST_SECRET
from mcppro import MCPServer
import joyverse.auth as jv_auth


def build_server():
    """Recreate run.py's server against the test secret."""
    jv_auth.JWT_SECRET = TEST_SECRET
    from joyverse.profile import get_profile, update_profile
    from joyverse.memory import get_memory, add_memory_trait, update_focus
    from joyverse.data import get_data, update_data
    from joyverse.prompts import USER_DATA

    srv = MCPServer(
        name="joyverse-mcp", version="1.0.0",
        auth=jv_auth.jwt_auth, instructions=USER_DATA,
    )
    srv.tool(description="Get the user's personal profile")(get_profile)
    srv.tool(description="Update a field in the user profile")(update_profile)
    srv.tool(description="Get the user's LLM memory model")(get_memory)
    srv.tool(description="Add a personality trait to memory")(add_memory_trait)
    srv.tool(description="Update the current main focus")(update_focus)
    srv.tool(description="Get structured data logs by topic")(get_data)
    srv.tool(description="Update structured data logs for a topic")(update_data)
    return srv


@pytest.fixture
def app(fake_r2):
    from fastapi.testclient import TestClient
    return TestClient(build_server()._app)


@pytest.fixture
def tok(make_token):
    return make_token("joydip", secret=TEST_SECRET)


def call(app, tool, arguments=None, token=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}}}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = app.post("/mcp", json=body, headers=headers)
    if r.status_code != 200:
        return {"http_status": r.status_code, "detail": r.json().get("detail")}
    for line in r.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])


def text_of(payload):
    return payload["result"]["content"][0]["text"]


pytestmark = pytest.mark.integration


class TestHandshake:
    def test_initialize_succeeds(self, app, sse_rpc, tok):
        res = sse_rpc(app, "initialize", token=tok)
        assert res["result"]["serverInfo"]["name"] == "joyverse-mcp"

    def test_all_seven_tools_registered(self, app, sse_rpc, tok):
        tools = sse_rpc(app, "tools/list", token=tok)["result"]["tools"]
        assert {t["name"] for t in tools} == {
            "get_profile", "update_profile", "get_memory",
            "add_memory_trait", "update_focus", "get_data", "update_data"}

    def test_instructions_reach_the_client(self, app, sse_rpc, tok):
        res = sse_rpc(app, "initialize", token=tok)
        assert "User Data System" in res["result"]["instructions"]

    def test_initialized_notification_is_silent(self, app, tok):
        r = app.post("/mcp", headers={"Authorization": f"Bearer {tok}"},
                     json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert r.text.strip() == ""


class TestAuthGate:
    def test_anonymous_is_rejected(self, app):
        assert call(app, "get_profile")["http_status"] == 401

    def test_bad_token_is_rejected(self, app):
        assert call(app, "get_profile", token="garbage")["http_status"] == 401

    def test_expired_token_is_rejected(self, app, make_token):
        old = make_token("joydip", secret=TEST_SECRET, exp_delta=-60)
        assert call(app, "get_profile", token=old)["http_status"] == 401

    def test_token_forged_with_wrong_secret_rejected(self, app):
        bad = pyjwt.encode({"username": "joydip", "exp": int(time.time()) + 60},
                           "attacker", algorithm="HS256")
        assert call(app, "get_profile", token=bad)["http_status"] == 401

    def test_traversal_username_rejected(self, app, make_token):
        bad = make_token("../../etc", secret=TEST_SECRET)
        assert call(app, "get_profile", token=bad)["http_status"] == 401


class TestProfileFlow:
    def test_read_missing_profile_returns_error_not_crash(self, app, tok):
        assert "error" in json.loads(text_of(call(app, "get_profile", token=tok)))

    def test_update_then_read_roundtrip(self, app, tok, fake_r2):
        fake_r2.seed("users/joydip/profile.md", "name: Joydip\nage: 22\n")
        call(app, "update_profile", {"field": "age", "value": "23"}, token=tok)
        assert "age: 23" in text_of(call(app, "get_profile", token=tok))

    def test_update_writes_the_users_key(self, app, tok, fake_r2):
        fake_r2.seed("users/joydip/profile.md", "age: 1\n")
        call(app, "update_profile", {"field": "age", "value": "5"}, token=tok)
        assert "age: 5" in fake_r2.store["users/joydip/profile.md"].decode()

    def test_update_missing_profile_reports_error(self, app, tok):
        out = text_of(call(app, "update_profile",
                           {"field": "age", "value": "5"}, token=tok))
        assert "does not exist" in out


class TestMemoryFlow:
    def test_first_get_creates_defaults(self, app, tok):
        data = json.loads(text_of(call(app, "get_memory", token=tok)))
        assert data["personality"] == []

    def test_add_trait_then_read_back(self, app, tok):
        call(app, "add_memory_trait", {"trait": "curious"}, token=tok)
        data = json.loads(text_of(call(app, "get_memory", token=tok)))
        assert "curious" in data["personality"]

    def test_update_focus_persists(self, app, tok):
        call(app, "update_focus", {"focus": "ship MVP"}, token=tok)
        data = json.loads(text_of(call(app, "get_memory", token=tok)))
        assert data["current_context"]["main_focus"] == "ship MVP"

    def test_traits_do_not_leak_across_users(self, app, tok, make_token):
        call(app, "add_memory_trait", {"trait": "joydip-secret"}, token=tok)
        other = make_token("alice", secret=TEST_SECRET)
        data = json.loads(text_of(call(app, "get_memory", token=other)))
        assert "joydip-secret" not in data["personality"]


class TestDataFlow:
    def test_write_then_read_roundtrip(self, app, tok, fake_r2):
        call(app, "update_data", {"topic": "dsa", "data": '{"done": 5}'}, token=tok)
        payload = call(app, "get_data", {"topic": "dsa"}, token=tok)
        assert json.loads(text_of(payload)) == {"done": 5}

    def test_missing_topic_reports_error(self, app, tok):
        out = text_of(call(app, "get_data", {"topic": "ghost"}, token=tok))
        assert "error" in json.loads(out)

    def test_invalid_json_is_rejected(self, app, tok):
        out = text_of(call(app, "update_data",
                           {"topic": "dsa", "data": "{bad"}, token=tok))
        assert "Invalid JSON" in out

    def test_traversal_topic_cannot_write_outside(self, app, tok, fake_r2):
        call(app, "update_data", {"topic": "../../../victim", "data": '{"p":1}'},
             token=tok)
        assert not any("victim" in p["Key"] for p in fake_r2.puts)


class TestUserIsolation:
    def test_two_users_get_separate_profiles(self, app, tok, make_token, fake_r2):
        fake_r2.seed("users/joydip/profile.md", "name: Joydip\n")
        fake_r2.seed("users/alice/profile.md", "name: Alice\n")
        alice = make_token("alice", secret=TEST_SECRET)
        assert "Alice" in text_of(call(app, "get_profile", token=alice))
        assert "Joydip" in text_of(call(app, "get_profile", token=tok))

    def test_user_a_update_does_not_touch_user_b(self, app, tok, make_token, fake_r2):
        fake_r2.seed("users/alice/profile.md", "age: 99\n")
        fake_r2.seed("users/joydip/profile.md", "age: 1\n")
        call(app, "update_profile", {"field": "age", "value": "50"}, token=tok)
        assert "age: 99" in fake_r2.store["users/alice/profile.md"].decode()

    def test_client_cannot_supply_its_own_user_param(self, app, tok):
        out = text_of(call(app, "get_profile", {"user": {"username": "alice"}},
                           token=tok))
        assert "error" in out or "alice" not in out.lower()


class TestProtocolEdgeCases:
    def test_unknown_tool_returns_error_result(self, app, tok):
        assert call(app, "nonexistent_tool", token=tok)["result"]["isError"] is True

    def test_unknown_method_returns_jsonrpc_error(self, app, tok):
        r = app.post("/mcp", headers={"Authorization": f"Bearer {tok}"},
                     json={"jsonrpc": "2.0", "id": 1, "method": "bogus"})
        assert json.loads(r.text[6:])["error"]["code"] == -32601

    def test_every_response_is_a_single_sse_frame(self, app, tok):
        r = app.post("/mcp", headers={"Authorization": f"Bearer {tok}"},
                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.text.count("data: ") == 1

    def test_request_id_is_echoed(self, app, tok):
        assert call(app, "get_profile", token=tok, req_id=99)["id"] == 99

    def test_schemas_hide_the_injected_user_param(self, app, sse_rpc, tok):
        tools = {t["name"]: t for t in
                 sse_rpc(app, "tools/list", token=tok)["result"]["tools"]}
        for name in ("get_profile", "update_profile", "get_memory"):
            assert "user" not in tools[name]["inputSchema"]["properties"]


class TestRunPySmoke:
    """Guards the actual run.py wiring, which the fixtures bypass."""

    def test_run_module_registers_all_seven_tools(self):
        import run
        assert {s.name for s in run.server._tool_schemas} == {
            "get_profile", "update_profile", "get_memory",
            "add_memory_trait", "update_focus", "get_data", "update_data"}

    def test_run_module_carries_instructions(self):
        import run
        assert run.server.instructions

    def test_run_module_uses_jwt_auth(self):
        import run
        assert run.server._auth_dependency is jv_auth.jwt_auth
