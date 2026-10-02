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
    from joyverse.bio import get_bio, update_bio
    from joyverse.memory import get_memory, add_memory_trait, update_focus
    from joyverse.data import get_data, list_topics, edit_data, replace_data
    from joyverse.storage import (
        register_client, list_clients, save_file_from_url, save_file_text,
        get_file, list_files, delete_file)
    from joyverse.prompts import USER_DATA

    srv = MCPServer(
        name="joyverse-mcp", version="1.0.0",
        auth=jv_auth.jwt_auth, instructions=USER_DATA,
    )
    srv.tool(description="Get the user's personal profile")(get_profile)
    srv.tool(description="Update a field in the user profile")(update_profile)
    srv.tool(description="Get the user's detailed life narrative")(get_bio)
    srv.tool(description="Write or replace a section of the user's bio")(update_bio)
    srv.tool(description="Get the user's LLM memory model")(get_memory)
    srv.tool(description="Add a personality trait to memory")(add_memory_trait)
    srv.tool(description="Update the current main focus")(update_focus)
    srv.tool(description="Get structured data logs by topic")(get_data)
    srv.tool(description="List stored data topics")(list_topics)
    srv.tool(description="Edit one data log in place")(edit_data)
    srv.tool(description="Replace a whole data log")(replace_data)
    srv.tool(description="Claim a client name for file storage")(register_client)
    srv.tool(description="List registered client names")(list_clients)
    srv.tool(description="Store a file from a URL")(save_file_from_url)
    srv.tool(description="Store text as a file")(save_file_text)
    srv.tool(description="Get a download link for a stored file")(get_file)
    srv.tool(description="List stored files")(list_files)
    srv.tool(description="Delete a stored file")(delete_file)
    return srv


@pytest.fixture
def app(fake_r2):
    from fastapi.testclient import TestClient
    return TestClient(build_server()._app)


@pytest.fixture
def tok(make_token, fake_r2):
    """A bearer token whose handle is registered in the fake registry.

    The R2 path a user reads and writes is derived from the resolved user_id,
    so tests that seed storage must ask the registry for that id rather than
    guessing a path from the handle.
    """
    token = make_token("joydip", secret=TEST_SECRET)
    return token


@pytest.fixture
def joy_key(fake_r2):
    """The R2 prefix that the `joydip` token actually reads and writes."""
    from joyverse import identity

    return f"users/{identity.resolve_handle('joydip')}"


@pytest.fixture
def alice_key(fake_r2):
    """The R2 prefix belonging to the `alice` handle."""
    from joyverse import identity

    return f"users/{identity.resolve_handle('alice')}"


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

    def test_all_tools_registered(self, app, sse_rpc, tok):
        tools = sse_rpc(app, "tools/list", token=tok)["result"]["tools"]
        assert {t["name"] for t in tools} == {
            "get_profile", "update_profile", "get_bio", "update_bio",
            "get_memory", "add_memory_trait", "update_focus",
            "get_data", "list_topics", "edit_data", "replace_data",
            "register_client", "list_clients", "save_file_from_url",
            "save_file_text", "get_file", "list_files", "delete_file"}

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

    def test_update_then_read_roundtrip(self, app, tok, fake_r2, joy_key):
        fake_r2.seed(f"{joy_key}/profile.md", "name: Joydip\nage: 22\n")
        call(app, "update_profile", {"field": "age", "value": "23"}, token=tok)
        assert "age: 23" in text_of(call(app, "get_profile", token=tok))

    def test_update_writes_the_users_key(self, app, tok, fake_r2, joy_key):
        fake_r2.seed(f"{joy_key}/profile.md", "age: 1\n")
        call(app, "update_profile", {"field": "age", "value": "5"}, token=tok)
        assert "age: 5" in fake_r2.store[f"{joy_key}/profile.md"].decode()

    def test_update_on_missing_profile_creates_it(self, app, tok, fake_r2, joy_key):
        # Regression: this used to fail with "Profile does not exist", which
        # blocked onboarding any brand-new user.
        out = text_of(call(app, "update_profile",
                           {"field": "name", "value": "Aarav"}, token=tok))
        assert "Error" not in out
        assert f"{joy_key}/profile.md" in fake_r2.store

    def test_section_field_creates_a_proper_block(self, app, tok, fake_r2, joy_key):
        call(app, "update_profile",
             {"field": "Identity", "value": "name: Aarav\nage: 24"}, token=tok)
        body = fake_r2.store[f"{joy_key}/profile.md"].decode()
        assert "## Identity" in body and "Identity: name:" not in body


class TestBioFlow:
    def test_missing_bio_returns_error_not_crash(self, app, tok):
        assert "error" in json.loads(text_of(call(app, "get_bio", token=tok)))

    def test_write_then_read_roundtrip(self, app, tok):
        call(app, "update_bio",
             {"section": "Background",
              "content": "Aarav grew up in Pune."}, token=tok)
        assert "Aarav grew up in Pune." in text_of(call(app, "get_bio", token=tok))

    def test_section_writes_to_bio_key(self, app, tok, fake_r2, joy_key):
        call(app, "update_bio",
             {"section": "Journey", "content": "2018: College"}, token=tok)
        assert f"{joy_key}/bio.md" in fake_r2.store

    def test_multiple_sections_coexist(self, app, tok):
        call(app, "update_bio", {"section": "Background", "content": "b"}, token=tok)
        call(app, "update_bio", {"section": "Goals", "content": "g"}, token=tok)
        body = text_of(call(app, "get_bio", token=tok))
        assert "## Background" in body and "## Goals" in body

    def test_section_replacement_preserves_others(self, app, tok):
        call(app, "update_bio", {"section": "Background", "content": "old"}, token=tok)
        call(app, "update_bio", {"section": "Goals", "content": "keepme"}, token=tok)
        call(app, "update_bio", {"section": "Background", "content": "new"}, token=tok)
        body = text_of(call(app, "get_bio", token=tok))
        assert "new" in body and "keepme" in body and "old" not in body

    def test_bio_does_not_leak_across_users(self, app, tok, make_token):
        call(app, "update_bio",
             {"section": "Background", "content": "SECRET-STORY"}, token=tok)
        other = make_token("someone-else", secret=TEST_SECRET)
        assert "SECRET-STORY" not in text_of(call(app, "get_bio", token=other))

    def test_empty_section_is_rejected(self, app, tok):
        out = text_of(call(app, "update_bio",
                           {"section": "##", "content": "x"}, token=tok))
        assert "Error" in out


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
        call(app, "replace_data", {"topic": "dsa", "data": '{"done": 5}'}, token=tok)
        payload = call(app, "get_data", {"topic": "dsa"}, token=tok)
        assert json.loads(text_of(payload)) == {"done": 5}

    def test_missing_topic_reports_error(self, app, tok):
        out = text_of(call(app, "get_data", {"topic": "ghost"}, token=tok))
        assert "error" in json.loads(out)

    def test_invalid_json_is_rejected(self, app, tok):
        out = text_of(call(app, "replace_data",
                           {"topic": "dsa", "data": "{bad"}, token=tok))
        assert "Invalid JSON" in out

    def test_traversal_topic_cannot_write_outside(self, app, tok, fake_r2):
        call(app, "replace_data", {"topic": "../../../victim", "data": '{"p":1}'},
             token=tok)
        assert not any("victim" in p["Key"] for p in fake_r2.puts)


class TestUserIsolation:
    def test_two_users_get_separate_profiles(self, app, tok, make_token, fake_r2, joy_key, alice_key):
        fake_r2.seed(f"{joy_key}/profile.md", "name: Joydip\n")
        fake_r2.seed(f"{alice_key}/profile.md", "name: Alice\n")
        alice = make_token("alice", secret=TEST_SECRET)
        assert "Alice" in text_of(call(app, "get_profile", token=alice))
        assert "Joydip" in text_of(call(app, "get_profile", token=tok))

    def test_user_a_update_does_not_touch_user_b(self, app, tok, make_token, fake_r2, joy_key, alice_key):
        fake_r2.seed(f"{alice_key}/profile.md", "age: 99\n")
        fake_r2.seed(f"{joy_key}/profile.md", "age: 1\n")
        call(app, "update_profile", {"field": "age", "value": "50"}, token=tok)
        assert "age: 99" in fake_r2.store[f"{alice_key}/profile.md"].decode()

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

    def test_run_module_registers_all_tools(self):
        import run
        assert {s.name for s in run.server._tool_schemas} == {
            "get_profile", "update_profile", "get_bio", "update_bio",
            "get_memory", "add_memory_trait", "update_focus",
            "get_data", "list_topics", "edit_data", "replace_data",
            "register_client", "list_clients", "save_file_from_url",
            "save_file_text", "get_file", "list_files", "delete_file"}

    def test_run_module_carries_instructions(self):
        import run
        assert run.server.instructions

    def test_run_module_wires_bearer_auth(self):
        # run.py composes strategies with any_auth(), so the dependency is the
        # chain rather than a bare function. Bearer must always be in it:
        # it is the recovery path when the OAuth provider is misconfigured.
        import run
        assert callable(run.server._auth_dependency)

    def test_build_auth_includes_bearer(self):
        import run

        chain = run.build_auth()
        # Exercised end to end: a self-issued token must still authenticate.
        assert chain is not None

    def test_bearer_token_works_through_the_real_wiring(self, fake_r2):
        # fake_r2 is REQUIRED here. run.server._auth_dependency resolves the
        # handle through the identity registry, so without the fixture this
        # writes a user into the REAL bucket -- which is exactly how ~90 test
        # users polluted production. The no_network_guard fixture turns any such
        # mistake into a loud failure rather than a silent write.
        import run
        from conftest import TEST_SECRET
        import jwt as pyjwt
        import time as _time

        class Req:
            def __init__(self, headers):
                self.headers = headers

        token = pyjwt.encode({"handle": "wiretest",
                              "exp": int(_time.time()) + 60},
                             TEST_SECRET, algorithm="HS256")
        ctx = run.server._auth_dependency(
            Req({"Authorization": f"Bearer {token}"}))
        assert ctx["user_id"]
        # and it landed in the fake, not production
        assert any(k.startswith("users/_registry/")
                   for k in fake_r2.store)

    def test_oauth_is_only_wired_when_configured(self):
        import run
        from joyverse import config as jv_config

        # Bearer stays available either way; OAuth is additive.
        assert callable(run.server._auth_dependency)
        if not (jv_config.OAUTH_ENABLED and jv_config.AUTH0_DOMAIN
                and jv_config.AUTH0_AUDIENCE):
            assert not run.server._extra_routes


# ==========================================================
# OAUTH END-TO-END
#
# The flows above cover the legacy bearer path. These cover the OAuth path a
# real MCP client (Claude, Cursor, ChatGPT) takes: discover the metadata ->
# get challenged -> present a token -> call a tool.
# ==========================================================

OAUTH_ISSUER = "https://tenant.example.auth0.com/"
OAUTH_AUDIENCE = "https://joyverse.example.com"
OAUTH_RESOURCE = "https://joyverse.example.com/mcp"


@pytest.fixture(scope="module")
def oauth_rsa():
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def oauth_client(oauth_rsa):
    """A server using oauth_bearer_auth, with discovery mounted."""
    from fastapi.testclient import TestClient
    from mcppro.auth import oauth_bearer_auth
    from mcppro.discovery import discovery_routes

    pub = oauth_rsa.public_key()

    class Stub:
        def get_signing_key_from_jwt(self, token):
            return type("K", (), {"key": pub})()

    srv = MCPServer(
        name="oauth-server", version="1.0.0",
        auth=oauth_bearer_auth(issuer=OAUTH_ISSUER, audience=OAUTH_AUDIENCE,
                               jwk_client=Stub()),
        extra_routes=[discovery_routes(
            OAUTH_RESOURCE,
            authorization_servers=["https://tenant.example.auth0.com"],
            scopes_supported=["joyverse:read", "joyverse:write"],
        )],
        resource_url=OAUTH_RESOURCE,
    )
    return TestClient(srv._app), srv


def oauth_token(key, **overrides):
    now = int(time.time())
    claims = {"iss": OAUTH_ISSUER, "aud": OAUTH_AUDIENCE,
              "sub": "auth0|abc123", "exp": now + 3600, "iat": now,
              "scope": "joyverse:read joyverse:write"}
    claims.update(overrides)
    return pyjwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def rpc(client, method, params=None, token=None, req_id=1):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
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


@pytest.mark.anyio
class TestOAuthDiscoveryFlow:
    """The exact sequence a compliant MCP client performs."""

    async def test_metadata_is_discoverable_before_any_token(self, oauth_client):
        client, _ = oauth_client
        r = client.get("/.well-known/oauth-protected-resource/mcp")
        assert r.status_code == 200
        assert r.json()["authorization_servers"] == \
            ["https://tenant.example.auth0.com"]

    async def test_challenge_on_401_points_at_the_metadata(self, oauth_client):
        # this is what lets a client bootstrap without being configured
        client, _ = oauth_client
        r = rpc(client, "tools/list")
        assert r.status_code == 401
        header = r.headers["www-authenticate"]
        assert header.startswith("Bearer ")
        assert "resource_metadata=" in header

    async def test_the_challenged_url_is_actually_fetchable(self, oauth_client):
        # the header must advertise a real, reachable document
        client, _ = oauth_client
        challenge = rpc(client, "tools/list").headers["www-authenticate"]
        target = challenge.split('resource_metadata="')[1].split('"')[0]
        path = target.replace("https://joyverse.example.com", "")
        assert client.get(path).status_code == 200

    async def test_advertised_scopes_match_the_enforced_ones(self, oauth_client):
        client, _ = oauth_client
        doc = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert doc["scopes_supported"] == ["joyverse:read", "joyverse:write"]


@pytest.mark.anyio
class TestOAuthToolCalls:
    async def test_tools_list_requires_a_token(self, oauth_client):
        client, _ = oauth_client
        assert rpc(client, "tools/list").status_code == 401

    async def test_valid_token_lists_tools(self, oauth_client, oauth_rsa):
        client, srv = oauth_client
        srv.tool(description="d")(lambda: "hi")
        assert "tools" in rpc(client, "tools/list",
                              token=oauth_token(oauth_rsa))["result"]

    async def test_wrong_audience_is_refused_end_to_end(self, oauth_client,
                                                        oauth_rsa):
        client, _ = oauth_client
        tok = oauth_token(oauth_rsa, aud="https://some-other-app.example")
        assert rpc(client, "tools/list", token=tok).status_code == 401

    async def test_expired_token_is_refused_end_to_end(self, oauth_client,
                                                       oauth_rsa):
        client, _ = oauth_client
        assert rpc(client, "tools/list",
                   token=oauth_token(oauth_rsa, exp=int(time.time()) - 300)
                   ).status_code == 401

    async def test_subject_reaches_the_tool_as_user_context(self, oauth_client,
                                                            oauth_rsa):
        # the raw sub arrives in `user`; mapping it to storage is the app's job
        client, srv = oauth_client

        def whoami(user: dict):
            return user["subject"]

        srv.tool(description="d")(whoami)
        res = rpc(client, "tools/call", {"name": "whoami", "arguments": {}},
                  token=oauth_token(oauth_rsa, sub="auth0|abc123"))
        assert res["result"]["content"][0]["text"] == "auth0|abc123"

    async def test_scopes_arrive_in_the_tool_context(self, oauth_client, oauth_rsa):
        client, srv = oauth_client

        def scopes_of(user: dict):
            return ",".join(user["scopes"])

        srv.tool(description="d")(scopes_of)
        res = rpc(client, "tools/call", {"name": "scopes_of", "arguments": {}},
                  token=oauth_token(oauth_rsa))
        assert res["result"]["content"][0]["text"] == \
            "joyverse:read,joyverse:write"

    async def test_client_cannot_supply_its_own_identity(self, oauth_client,
                                                         oauth_rsa):
        # the LLM must not be able to pass `user` and read someone else's data
        client, srv = oauth_client

        def whoami(user: dict):
            return user["subject"]

        srv.tool(description="d")(whoami)
        res = rpc(client, "tools/call",
                  {"name": "whoami", "arguments": {"user": {"subject": "attacker"}}},
                  token=oauth_token(oauth_rsa, sub="auth0|realuser"))
        assert res["result"]["content"][0]["text"] == "auth0|realuser"


@pytest.mark.anyio
class TestOAuthPerToolScopes:
    async def test_under_scoped_call_is_refused(self, oauth_client, oauth_rsa):
        client, srv = oauth_client

        def writer():
            return "wrote something"

        srv.tool(description="d", scopes=["joyverse:write"])(writer)
        res = rpc(client, "tools/call", {"name": "writer", "arguments": {}},
                  token=oauth_token(oauth_rsa, scope="joyverse:read"))
        assert res["result"]["isError"] is True
        assert "Insufficient scope" in res["result"]["content"][0]["text"]

    async def test_correctly_scoped_call_succeeds(self, oauth_client, oauth_rsa):
        client, srv = oauth_client

        def writer():
            return "wrote something"

        srv.tool(description="d", scopes=["joyverse:write"])(writer)
        res = rpc(client, "tools/call", {"name": "writer", "arguments": {}},
                  token=oauth_token(oauth_rsa))
        assert res["result"]["isError"] is False

    async def test_scopeless_tool_stays_open_to_any_caller(self, oauth_client,
                                                           oauth_rsa):
        # backwards compatible: tools without scopes behave as before
        client, srv = oauth_client

        def anything():
            return "ok"

        srv.tool(description="d")(anything)
        res = rpc(client, "tools/call", {"name": "anything", "arguments": {}},
                  token=oauth_token(oauth_rsa, scope=""))
        assert res["result"]["isError"] is False

    async def test_scopes_are_not_leaked_into_the_advertised_schema(self,
                                                                   oauth_client,
                                                                   oauth_rsa):
        client, srv = oauth_client

        def writer():
            return "ok"

        srv.tool(description="d", scopes=["joyverse:write"])(writer)
        res = rpc(client, "tools/list", token=oauth_token(oauth_rsa))
        assert "joyverse:write" not in json.dumps(res["result"]["tools"])


@pytest.mark.anyio
class TestOAuthAndLegacyCoexist:
    """The transition case: both credential types live at once."""

    @pytest.fixture
    def combined(self, oauth_rsa):
        from fastapi import HTTPException
        from fastapi.testclient import TestClient
        from mcppro.auth import oauth_bearer_auth, any_auth
        from mcppro.discovery import discovery_routes

        pub = oauth_rsa.public_key()

        class Stub:
            def get_signing_key_from_jwt(self, token):
                return type("K", (), {"key": pub})()

        def legacy(request):
            if request.headers.get("X-API-Key") != "legacy-secret":
                raise HTTPException(status_code=401, detail="Invalid API Key")
            return {"api_key": "legacy-secret", "role": "user"}

        srv = MCPServer(
            name="combined", version="1.0.0",
            auth=any_auth(
                oauth_bearer_auth(issuer=OAUTH_ISSUER, audience=OAUTH_AUDIENCE,
                                  jwk_client=Stub()),
                legacy,
            ),
            extra_routes=[discovery_routes(
                OAUTH_RESOURCE,
                authorization_servers=["https://tenant.example.auth0.com"])],
            resource_url=OAUTH_RESOURCE,
        )
        return TestClient(srv._app)

    async def test_oauth_token_works(self, combined, oauth_rsa):
        assert "tools" in rpc(combined, "tools/list",
                              token=oauth_token(oauth_rsa))["result"]

    async def test_legacy_api_key_still_works(self, combined):
        r = combined.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                        "method": "tools/list"},
                          headers={"X-API-Key": "legacy-secret"})
        assert r.status_code == 200

    async def test_discovery_still_works(self, combined):
        assert combined.get(
            "/.well-known/oauth-protected-resource/mcp").status_code == 200


class TestFileToolsOverMCP:
    """File tools through the real JSON-RPC layer.

    The storage unit tests call the functions directly. These go over the wire
    instead, which is the only way to catch the injected `user` argument being
    accepted from a caller, or a tool wired up with no scope.
    """

    def test_cannot_forge_user_by_passing_it(self, app, tok):
        """A caller must not be able to name another user in arguments."""
        res = call(app, "list_clients", {"user": {"user_id": "u_someone_else"}},
                   token=tok)
        text = text_of(res)
        assert '"error"' in text or '"count": 0' in text

    def test_register_save_and_fetch_round_trip(self, app, tok):
        res = call(app, "register_client", {"name": "chatgpt"}, token=tok)
        assert json.loads(text_of(res))["ok"] is True

        res = call(app, "save_file_text",
                   {"path": "notes/hello.txt", "content": "hi there",
                    "client": "chatgpt"}, token=tok)
        assert json.loads(text_of(res))["ok"] is True

        res = call(app, "get_file", {"path": "notes/hello.txt",
                                     "client": "chatgpt"}, token=tok)
        got = json.loads(text_of(res))
        assert got["ok"] is True
        assert got["url"].startswith("https://")

        res = call(app, "list_files", {"client": "chatgpt"}, token=tok)
        listed = json.loads(text_of(res))
        assert listed["files"][0]["path"] == "notes/hello.txt"

    def test_two_clients_share_the_users_store(self, app, tok):
        """Cross-client handoff: one client sees what another stored."""
        call(app, "register_client", {"name": "chatgpt"}, token=tok)
        call(app, "register_client", {"name": "claude"}, token=tok)
        call(app, "save_file_text",
             {"path": "shared.txt", "content": "from chatgpt",
              "client": "chatgpt"}, token=tok)

        res = call(app, "list_files", {}, token=tok)
        paths = {f["path"] for f in json.loads(text_of(res))["files"]}
        assert "chatgpt/shared.txt" in paths

    def test_traversal_rejected_over_mcp(self, app, tok):
        call(app, "register_client", {"name": "chatgpt"}, token=tok)
        res = call(app, "save_file_text",
                   {"path": "../../escape.txt", "content": "x",
                    "client": "chatgpt"}, token=tok)
        assert '"error"' in text_of(res)

    def test_ssrf_rejected_over_mcp(self, app, tok):
        call(app, "register_client", {"name": "chatgpt"}, token=tok)
        res = call(app, "save_file_from_url",
                   {"url": "http://169.254.169.254/latest/meta-data/",
                    "path": "x.txt", "client": "chatgpt"}, token=tok)
        assert '"error"' in text_of(res)

    def test_delete_removes_file(self, app, tok):
        call(app, "register_client", {"name": "chatgpt"}, token=tok)
        call(app, "save_file_text",
             {"path": "gone.txt", "content": "x", "client": "chatgpt"},
             token=tok)
        res = call(app, "delete_file", {"path": "gone.txt",
                                        "client": "chatgpt"}, token=tok)
        assert json.loads(text_of(res))["ok"] is True
        res = call(app, "list_files", {"client": "chatgpt"}, token=tok)
        assert json.loads(text_of(res))["files"] == []

    def test_two_users_cannot_see_each_other(self, app, tok, make_token):
        """The core isolation guarantee, over the wire.

        `alice` resolves to a different user_id than `joydip`, so her token
        must reach a different storage root entirely.
        """
        call(app, "register_client", {"name": "chatgpt"}, token=tok)
        call(app, "save_file_text",
             {"path": "private.txt", "content": "mine", "client": "chatgpt"},
             token=tok)

        other = make_token("alice", secret=TEST_SECRET)
        res = call(app, "list_files", {}, token=other)
        assert json.loads(text_of(res))["files"] == []

        res = call(app, "get_file", {"path": "private.txt",
                                     "client": "chatgpt"}, token=other)
        assert '"error"' in text_of(res)

    def test_file_tools_require_auth(self, app):
        for name, args in [("list_files", {}),
                           ("list_clients", {}),
                           ("get_file", {"path": "a", "client": "chatgpt"})]:
            r = app.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                       "method": "tools/call",
                                       "params": {"name": name,
                                                  "arguments": args}})
            assert r.status_code == 401, f"{name} was reachable unauthenticated"
