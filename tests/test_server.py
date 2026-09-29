"""Unit tests for mcppro/server.py -- the MCPServer facade and HTTP wiring."""
import json
import pytest
from fastapi import HTTPException
from mcppro import MCPServer
from mcppro.auth import no_auth, api_key_auth


def sample_tool(a: str) -> str:
    """Sample tool."""
    return a


class TestConstruction:
    def test_stores_identity(self):
        s = MCPServer(name="n", version="2.0")
        assert s.name == "n" and s.version == "2.0"

    def test_default_version(self):
        assert MCPServer(name="n").version == "1.0.0"

    def test_instructions_default_to_empty(self):
        assert MCPServer(name="n").instructions == ""

    def test_instructions_are_stored(self):
        s = MCPServer(name="n", instructions="be helpful")
        assert s.instructions == "be helpful"

    def test_none_instructions_become_empty_string(self):
        assert MCPServer(name="n", instructions=None).instructions == ""

    def test_registries_start_empty(self):
        s = MCPServer(name="n")
        assert s._tool_schemas == [] and s._tool_functions == {}

    def test_creates_fastapi_app(self):
        assert MCPServer(name="n")._app is not None

    def test_app_title_contains_server_name(self):
        assert MCPServer(name="myserver")._app.title == "MCP Server: myserver"

    def test_instances_do_not_share_registries(self):
        a, b = MCPServer(name="a"), MCPServer(name="b")
        a.tool()(sample_tool)
        assert b._tool_schemas == []


class TestToolRegistration:
    def test_registers_via_decorator(self):
        s = MCPServer(name="n")
        s.tool(description="d")(sample_tool)
        assert "sample_tool" in s._tool_functions

    def test_decorator_returns_function(self):
        s = MCPServer(name="n")
        assert s.tool()(sample_tool) is sample_tool

    def test_app_is_built_after_registration_reflects_tools(self):
        s = MCPServer(name="n")
        s.tool(description="d")(sample_tool)
        assert len(s._tool_schemas) == 1


class TestAuthWiring:
    def test_defaults_to_no_auth(self):
        s = MCPServer(name="n")
        assert s._auth_dependency is no_auth

    def test_custom_auth_is_stored(self):
        def my_auth(request): ...
        assert MCPServer(name="n", auth=my_auth)._auth_dependency is my_auth

    def test_falsy_auth_falls_back_to_no_auth(self):
        assert MCPServer(name="n", auth=None)._auth_dependency is no_auth


class TestRunSignature:
    def test_accepts_host_and_port(self):
        import inspect
        sig = inspect.signature(MCPServer.run)
        assert sig.parameters["host"].default == "0.0.0.0"
        assert sig.parameters["port"].default == 8000
        assert sig.parameters["reload"].default is False

    def test_run_does_not_block_on_import(self):
        # merely referencing the bound method must not start uvicorn
        s = MCPServer(name="n")
        assert callable(s.run)


class TestHttpEndpoint:
    def test_mcp_route_exists(self, client_factory):
        client, _ = client_factory()
        paths = [r.path for r in client.app.routes]
        assert "/mcp" in paths

    def test_health_endpoint_exists(self, client_factory):
        client, _ = client_factory()
        paths = [r.path for r in client.app.routes]
        assert "/health" in paths

    def test_health_endpoint_returns_ok(self, client_factory):
        client, _ = client_factory()
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "server" in data
        assert "version" in data

    def test_response_is_an_sse_stream(self, client_factory, sse_rpc):
        client, _ = client_factory()
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r.headers["content-type"].startswith("text/event-stream")

    def test_response_is_not_cached(self, client_factory):
        client, _ = client_factory()
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert r.headers.get("cache-control") == "no-cache"

    def test_malformed_json_returns_parse_error(self, client_factory):
        client, _ = client_factory()
        r = client.post("/mcp", content=b"{{{not json",
                        headers={"Content-Type": "application/json"})
        assert r.json()["error"]["code"] == -32700

    def test_parse_error_uses_null_id(self, client_factory):
        client, _ = client_factory()
        r = client.post("/mcp", content=b"nope",
                        headers={"Content-Type": "application/json"})
        assert r.json()["id"] is None

    def test_server_name_and_version_are_reported(self, client_factory, sse_rpc):
        client, _ = client_factory()
        res = sse_rpc(client, "initialize")
        assert res["result"]["serverInfo"] == {"name": "test-server",
                                               "version": "9.9.9"}


class TestInstructionsOverHttp:
    def test_instructions_are_sent_to_the_client(self, client_factory, sse_rpc):
        client, _ = client_factory(instructions="be terse")
        assert sse_rpc(client, "initialize")["result"]["instructions"] == "be terse"

    def test_absent_instructions_key_when_unset(self, client_factory, sse_rpc):
        client, _ = client_factory()
        assert "instructions" not in sse_rpc(client, "initialize")["result"]


class TestAuthEnforcementOverHttp:
    def test_missing_token_is_401(self, client_factory):
        client, _ = client_factory(auth=api_key_auth(["k"]))
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401

    def test_valid_token_passes(self, client_factory, sse_rpc):
        client, _ = client_factory(auth=api_key_auth(["k"]))
        res = sse_rpc(client, "tools/list", token="k")
        assert "tools" in res["result"]

    def test_no_auth_allows_anonymous(self, client_factory, sse_rpc):
        client, _ = client_factory(auth=no_auth)
        assert "tools" in sse_rpc(client, "tools/list")["result"]

    def test_auth_runs_before_tool_dispatch(self, client_factory):
        # a protected server must not even list tools to an anonymous caller
        client, srv = client_factory(auth=api_key_auth(["k"]))
        srv.tool(description="d")(sample_tool)
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
