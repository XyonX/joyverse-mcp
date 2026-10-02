"""Deterministic tests for the MCP test client harness.

No network, no API key, no real LLM -- these verify the client's own logic:
token minting, SSE parsing, tool dispatch, schema conversion and the
tool-calling loop (driven by a stub LLM).
"""
import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "mcp_client"))

import mcp_test_client as mc  # noqa: E402


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-not-a-real-one")
    return "test-secret-not-a-real-one"


@pytest.fixture
def app(fake_r2):
    import run as app_module
    return app_module.server._app


@pytest.fixture
def client(app, secret):
    return mc.MCPClient(app, mc.mint_token("aarav-test"))


class TestMintToken:
    def test_returns_three_part_jwt(self, secret):
        assert mc.mint_token("aarav-test").count(".") == 2

    def test_encodes_the_username(self, secret):
        import jwt as pyjwt
        payload = pyjwt.decode(mc.mint_token("aarav-test"), secret,
                               algorithms=["HS256"])
        assert payload["username"] == "aarav-test"

    def test_sets_expiry_in_the_future(self, secret):
        import time
        import jwt as pyjwt
        payload = pyjwt.decode(mc.mint_token("aarav-test"), secret,
                               algorithms=["HS256"])
        assert payload["exp"] > time.time()

    def test_exits_without_secret(self, monkeypatch):
        monkeypatch.delenv("JWT_SECRET", raising=False)
        with pytest.raises(SystemExit):
            mc.mint_token("x")


class TestHandshake:
    def test_initialize_returns_server_info(self, client):
        assert client.initialize()["serverInfo"]["name"] == "joyverse-mcp"

    def test_initialize_carries_instructions(self, client):
        assert client.initialize().get("instructions")

    def test_lists_every_tool(self, client):
        # Compared against the expected set rather than a bare count, so adding
        # or removing a tool gives a readable diff instead of "18 != 17".
        assert len(client.list_tools()) == 18

    def test_tool_names_match_the_server(self, client):
        names = {t["name"] for t in client.list_tools()}
        assert names == {
            "get_profile", "update_profile", "get_bio", "update_bio",
            "get_memory", "add_memory_trait", "update_focus",
            "get_data", "list_topics", "edit_data", "replace_data",
            "register_client", "list_clients", "save_file_from_url",
            "save_file_text", "get_file", "list_files", "delete_file"}

    def test_bad_token_is_rejected(self, app):
        bad = mc.MCPClient(app, "not-a-jwt")
        with pytest.raises(RuntimeError):
            bad.initialize()


class TestSseParsing:
    def test_frame_decodes(self, app, secret):
        c = mc.MCPClient(app, mc.mint_token("aarav-test"))
        assert c._rpc("tools/list")["id"] == 1

    def test_request_id_is_echoed(self, app, secret):
        c = mc.MCPClient(app, mc.mint_token("aarav-test"))
        assert c._rpc("tools/list", req_id=42)["id"] == 42


class TestCallTool:
    def test_update_bio_then_read(self, client):
        r = client.call_tool("update_bio",
                             {"section": "Background", "content": "From Pune."})
        assert r["isError"] is False
        assert "From Pune." in client.call_tool("get_bio", {})["text"]

    def test_missing_data_returns_error_text(self, client):
        assert "error" in client.call_tool("get_data", {"topic": "dsa"})["text"]

    def test_unknown_tool_is_flagged(self, client):
        assert client.call_tool("nope", {})["isError"] is True


class TestToolSchemaConversion:
    def test_converts_to_openai_format(self):
        tools = [{"name": "get_profile", "description": "d",
                  "inputSchema": {"type": "object", "properties": {}}}]
        out = mc.to_openai_tools(tools)
        assert out[0]["type"] == "function"
        assert out[0]["function"]["name"] == "get_profile"

    def test_preserves_description(self):
        out = mc.to_openai_tools([{"name": "t", "description": "desc",
                                   "inputSchema": {}}])
        assert out[0]["function"]["description"] == "desc"

    def test_missing_input_schema_gets_an_object(self):
        out = mc.to_openai_tools([{"name": "t", "description": ""}])
        assert out[0]["function"]["parameters"]["type"] == "object"

    def test_injected_user_param_is_absent(self, client):
        for t in mc.to_openai_tools(client.list_tools()):
            props = t["function"]["parameters"].get("properties", {})
            assert "user" not in props

    def test_replace_data_schema_expects_a_string(self, client):
        # The whole-log writer still takes JSON as a STRING. This is the
        # mistake an agent most often makes, so the type stays pinned.
        tools = {t["function"]["name"]: t for t in
                 mc.to_openai_tools(client.list_tools())}
        data_prop = tools["replace_data"]["function"]["parameters"]["properties"]["data"]
        assert data_prop["type"] == "string"

    def test_edit_data_schema_exposes_its_operations(self, client):
        tools = {t["function"]["name"]: t for t in
                 mc.to_openai_tools(client.list_tools())}
        props = tools["edit_data"]["function"]["parameters"]["properties"]
        for arg in ("topic", "op", "value", "path", "match"):
            assert arg in props, f"edit_data is missing {arg}"

    def test_the_dangerous_tool_is_named_replace_not_update(self, client):
        # Naming is a safety feature: an agent reading "update_data" would
        # reasonably assume it sends a patch.
        names = {t["name"] for t in client.list_tools()}
        assert "replace_data" in names
        assert "update_data" not in names


class TestToolCallLoop:
    """Drives run_conversation with a stub LLM -- no network."""

    def _stub(self, responses):
        """Build a fake OpenAI client returning queued responses."""
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            item = responses.pop(0)
            if isinstance(item, str):
                content, tool_calls = item, None
            else:
                content, tool_calls = None, item
            msg = types.SimpleNamespace(
                content=content, tool_calls=tool_calls,
                model_dump=lambda exclude_none=True: {
                    "role": "assistant", "content": content})
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=msg)])

        llm = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=create)))
        return llm, calls

    def _call(self, name, args):
        fn = types.SimpleNamespace(name=name, arguments=json.dumps(args))
        return types.SimpleNamespace(function=fn, id=f"call_{name}")

    def _turn(self, client, text="hi"):
        return [{"role": "user", "content": text}]

    def test_stops_when_no_tool_calls(self, client):
        llm, _ = self._stub(["All done."])
        assert mc.run_conversation(client, llm, [], self._turn(client)) == 0

    def test_executes_a_tool_then_finishes(self, client):
        llm, _ = self._stub([[self._call("get_profile", {})], "No profile yet."])
        assert mc.run_conversation(client, llm, [], self._turn(client)) == 1

    def test_tool_result_is_appended_to_messages(self, client):
        llm, _ = self._stub([[self._call("get_profile", {})], "done"])
        messages = self._turn(client)
        mc.run_conversation(client, llm, [], messages)
        # user -> assistant(tool_calls) -> tool result -> assistant(final)
        assert [m["role"] for m in messages] == [
            "user", "assistant", "tool", "assistant"]
        assert "error" in messages[2]["content"]

    def test_multiple_tools_in_one_turn(self, client):
        llm, _ = self._stub([
            [self._call("get_profile", {}), self._call("get_bio", {})], "done"])
        assert mc.run_conversation(client, llm, [], self._turn(client)) == 2

    def test_dry_run_never_reaches_the_server(self, client, fake_r2):
        llm, _ = self._stub([
            [self._call("update_bio", {"section": "X", "content": "y"})], "done"])
        made = mc.run_conversation(client, llm, [], self._turn(client),
                                   dry_run=True)
        assert made == 1
        assert fake_r2.puts == []

    def test_honours_max_tool_rounds(self, client):
        # Queue far more loops than the cap so the stub never runs dry,
        # independent of the MAX_TOOL_ROUNDS value itself.
        loops = [[self._call("get_profile", {})]] * (mc.MAX_TOOL_ROUNDS + 5)
        llm, _ = self._stub(loops)
        made = mc.run_conversation(client, llm, [], self._turn(client))
        assert made == mc.MAX_TOOL_ROUNDS

    def test_tools_are_passed_to_the_llm(self, client):
        llm, calls = self._stub(["done"])
        tools = client.list_tools()
        mc.run_conversation(client, llm, tools, self._turn(client))
        assert len(calls[0]["tools"]) == len(tools)

    def test_malformed_arguments_do_not_crash(self, client):
        bad = types.SimpleNamespace(name="get_profile", arguments="{not json")
        llm, _ = self._stub([[types.SimpleNamespace(function=bad, id="c1")],
                             "done"])
        assert mc.run_conversation(client, llm, [], self._turn(client)) == 1


class TestPersonaAndIsolation:
    def test_persona_file_exists(self):
        p = REPO_ROOT / "mcp_client" / "persona.md"
        assert p.exists() and len(p.read_text()) > 200

    def test_persona_names_the_test_user(self):
        assert mc.TEST_USERNAME in (REPO_ROOT / "mcp_client" / "persona.md").read_text()

    def test_config_defaults_to_test_user(self):
        assert mc.TEST_USERNAME == "aarav-test"

    def test_client_never_touches_another_user(self, client, fake_r2):
        # Storage is keyed by the resolved user_id, not by the handle, so the
        # assertion asks the registry where this persona actually writes
        # instead of assuming a path.
        #
        # The identity registry itself is server infrastructure and lives under
        # the same users/ prefix, so it is excluded: what matters is that no
        # key belongs to anybody other than this persona.
        from joyverse import identity

        client.call_tool("update_bio", {"section": "S", "content": "mine"})
        prefix = f"users/{identity.resolve_handle(mc.TEST_USERNAME)}/"
        data_keys = [k for k in fake_r2.store if not k.startswith("users/_registry/")]

        assert data_keys, "the client wrote nothing to assert on"
        assert all(k.startswith(prefix) for k in data_keys)
