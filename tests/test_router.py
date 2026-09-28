"""Unit tests for mcppro/router.py -- JSON-RPC dispatch logic.

Drives the async generator directly, so these are protocol tests without
going through HTTP.
"""
import json
import pytest
from mcppro import router as rt


async def collect(**kwargs):
    """Drain route_request and return the decoded SSE frames."""
    out = []
    async for chunk in rt.route_request(**kwargs):
        assert chunk.startswith("data: ")
        out.append(json.loads(chunk[6:]))
    return out


BASE = dict(
    method="initialize", req_id=1, params={},
    server_name="s", server_version="1.0.0",
    tool_schemas=[], tool_functions={},
)


@pytest.mark.anyio
class TestInitialize:
    async def test_returns_one_frame(self):
        assert len(await collect(**BASE)) == 1

    async def test_protocol_version(self):
        r = (await collect(**BASE))[0]
        assert r["result"]["protocolVersion"] == "2025-03-26"

    async def test_server_info(self):
        r = (await collect(**BASE))[0]
        assert r["result"]["serverInfo"] == {"name": "s", "version": "1.0.0"}

    async def test_declares_tool_capability(self):
        r = (await collect(**BASE))[0]
        assert "tools" in r["result"]["capabilities"]

    async def test_envelope_shape(self):
        r = (await collect(**BASE))[0]
        assert r["jsonrpc"] == "2.0" and r["id"] == 1

    async def test_instructions_included_when_given(self):
        r = (await collect(**BASE, instructions="hello"))[0]
        assert r["result"]["instructions"] == "hello"

    async def test_instructions_omitted_when_empty(self):
        r = (await collect(**BASE, instructions=""))[0]
        assert "instructions" not in r["result"]


@pytest.mark.anyio
class TestNotifications:
    async def test_initialized_notification_gets_no_reply(self):
        assert await collect(**{**BASE, "method": "notifications/initialized"}) == []

    async def test_notification_yields_nothing_even_with_id(self):
        frames = await collect(**{**BASE, "method": "notifications/initialized",
                                  "req_id": 99})
        assert frames == []


@pytest.mark.anyio
class TestToolsList:
    async def test_empty_registry_returns_empty_list(self):
        r = (await collect(**{**BASE, "method": "tools/list"}))[0]
        assert r["result"]["tools"] == []

    async def test_serialises_schemas(self):
        from mcppro.types import MCPToolDefinition
        td = MCPToolDefinition(name="t", description="d",
                               inputSchema={"type": "object"})
        r = (await collect(**{**BASE, "method": "tools/list",
                              "tool_schemas": [td]}))[0]
        assert r["result"]["tools"][0]["name"] == "t"
        assert r["result"]["tools"][0]["inputSchema"] == {"type": "object"}


@pytest.mark.anyio
class TestToolsCall:
    async def test_calls_the_function(self):
        r = (await collect(**{**BASE, "method": "tools/call",
                              "params": {"name": "t", "arguments": {}},
                              "tool_functions": {"t": lambda: "hi"}}))[0]
        assert r["result"]["content"][0]["text"] == "hi"
        assert r["result"]["isError"] is False

    async def test_arguments_are_forwarded(self):
        def add(a: int, b: int):
            return a + b
        r = (await collect(**{**BASE, "method": "tools/call",
                              "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
                              "tool_functions": {"add": add}}))[0]
        assert r["result"]["content"][0]["text"] == "5"

    async def test_unknown_tool_is_flagged(self):
        r = (await collect(**{**BASE, "method": "tools/call",
                              "params": {"name": "nope", "arguments": {}}}))[0]
        assert r["result"]["isError"] is True
        assert "Unknown tool" in r["result"]["content"][0]["text"]

    async def test_exception_becomes_error_result(self):
        def boom():
            raise RuntimeError("kaboom")
        r = (await collect(**{**BASE, "method": "tools/call",
                              "params": {"name": "b", "arguments": {}},
                              "tool_functions": {"b": boom}}))[0]
        assert r["result"]["isError"] is True
        assert "kaboom" in r["result"]["content"][0]["text"]

    async def test_result_is_a_proper_jsonrpc_frame(self):
        # regression: the pydantic model must be dumped, not passed raw
        r = (await collect(**{**BASE, "method": "tools/call",
                              "params": {"name": "t", "arguments": {}},
                              "tool_functions": {"t": lambda: "hi"}}))[0]
        assert set(r) == {"jsonrpc", "id", "result"}
        assert isinstance(r["result"], dict)

    async def test_missing_arguments_defaults_to_empty(self):
        r = (await collect(**{**BASE, "method": "tools/call",
                              "params": {"name": "t"},
                              "tool_functions": {"t": lambda: "ok"}}))[0]
        assert r["result"]["isError"] is False


@pytest.mark.anyio
class TestContextInjection:
    """The router injects the auth context into any tool declaring `user`."""

    async def test_user_is_injected(self):
        def who(user: dict):
            return user["username"]
        r = (await collect(**{**BASE, "method": "tools/call",
                              "params": {"name": "w", "arguments": {}},
                              "tool_functions": {"w": who},
                              "user_context": {"username": "alice"}}))[0]
        assert r["result"]["content"][0]["text"] == "alice"

    async def test_injection_does_not_require_client_to_send_user(self):
        def who(user: dict):
            return user["username"]
        r = (await collect(**{**BASE, "method": "tools/call",
                              "params": {"name": "w", "arguments": {}},
                              "tool_functions": {"w": who},
                              "user_context": {"username": "bob"}}))[0]
        assert r["result"]["isError"] is False

    async def test_tool_without_user_param_is_unaffected(self):
        r = (await collect(**{**BASE, "method": "tools/call",
                              "params": {"name": "t", "arguments": {}},
                              "tool_functions": {"t": lambda: "plain"},
                              "user_context": {"username": "alice"}}))[0]
        assert r["result"]["content"][0]["text"] == "plain"

    async def test_missing_user_context_is_tolerated(self):
        # regression: `user_context` was once an undefined name (NameError)
        def who(user: dict):
            return "got empty"
        r = (await collect(**{**BASE, "method": "tools/call",
                              "params": {"name": "w", "arguments": {}},
                              "tool_functions": {"w": who}}))[0]
        assert r["result"]["isError"] is False


@pytest.mark.anyio
class TestMethodNotFound:
    async def test_unknown_method_returns_error_frame(self):
        frames = await collect(**{**BASE, "method": "bogus/method"})
        assert len(frames) == 1 and frames[0]["error"]["code"] == -32601

    async def test_error_frame_has_no_result(self):
        f = (await collect(**{**BASE, "method": "bogus/method"}))[0]
        assert "result" not in f and f["jsonrpc"] == "2.0"
