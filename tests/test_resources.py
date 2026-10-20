"""Tests for mcppro/resources.py and the resources/* router methods.

Covers the shapes the MCP spec fixes: the capability declaration, the
resources/list and resources/read payloads, and -32002 for an unknown URI.
"""
import json

import pytest

from mcppro import MCPServer
from mcppro.resources import ResourceRegistry, _matches_template


# ==========================================
# TEMPLATE MATCHING
# ==========================================

class TestTemplateMatching:
    def test_single_placeholder(self):
        assert _matches_template("joyverse://data/dsa",
                                 "joyverse://data/{topic}")

    def test_rejects_extra_path_segment(self):
        # {topic} must not swallow a slash, or joyverse://data/a/b would
        # resolve to joyverse://data/{topic} and read the wrong record.
        assert not _matches_template("joyverse://data/a/b",
                                     "joyverse://data/{topic}")

    def test_exact_literal_does_not_match_a_template(self):
        assert not _matches_template("joyverse://profile",
                                     "joyverse://data/{topic}")

    def test_braces_in_uri_do_not_raise(self):
        assert _matches_template("joyverse://data/{weird}",
                                 "joyverse://data/{topic}")

    def test_no_false_match_on_partial(self):
        assert not _matches_template("joyverse://data",
                                     "joyverse://data/{topic}")


# ==========================================
# REGISTRY
# ==========================================

class TestRegistry:
    def _registry(self):
        r = ResourceRegistry()
        r.add_resource("joyverse://profile", lambda user: "body",
                       name="Profile", mime_type="text/markdown")
        return r

    def test_empty_registry_has_nothing(self):
        assert not ResourceRegistry().has_any()

    def test_add_resource_makes_it_non_empty(self):
        assert self._registry().has_any()

    def test_list_resources(self):
        assert [x.uri for x in self._registry().list_resources({})] == [
            "joyverse://profile"]

    def test_resolve_exact(self):
        reader, mime = self._registry().resolve("joyverse://profile", {})
        assert mime == "text/markdown"

    def test_resolve_unknown_raises(self):
        with pytest.raises(KeyError):
            self._registry().resolve("joyverse://nope", {})

    def test_user_is_injected_when_declared(self):
        r = ResourceRegistry()
        r.add_resource("x://a", lambda user: user["id"], name="a")
        reader, _ = r.resolve("x://a", {})
        assert reader(user={"id": "u1"}) == "u1"

    def test_reader_taking_no_params_works(self):
        # Readers are invoked through _call, which injects only what the
        # signature declares. A zero-arg reader must be called bare.
        from mcppro.resources import _call

        r = ResourceRegistry()
        r.add_resource("x://a", lambda: "ok", name="a")
        reader, _ = r.resolve("x://a", {"id": "ignored"})
        assert _call(reader, {"id": "ignored"}) == "ok"

    def test_call_injects_uri_only_when_declared(self):
        from mcppro.resources import _call

        assert _call(lambda uri: uri, {}, "x://y") == "x://y"
        assert _call(lambda: "none", {}, "x://y") == "none"

    def test_lister_contributes_live_resources(self):
        r = ResourceRegistry()
        r.add_resource("x://static", lambda: "s", name="s")
        r.set_lister(lambda user: [{"uri": "x://live", "name": "live"}])
        uris = {x.uri for x in r.list_resources({})}
        assert uris == {"x://static", "x://live"}

    def test_lister_receives_user(self):
        r = ResourceRegistry()
        seen = {}

        def lister(user):
            seen["id"] = user["id"]
            return []

        r.set_lister(lister)
        r.list_resources({"id": "u9"})
        assert seen["id"] == "u9"

    def test_failing_lister_does_not_break_listing(self):
        r = ResourceRegistry()
        r.add_resource("x://static", lambda: "s", name="s")

        def boom(user):
            raise RuntimeError("r2 down")

        r.set_lister(boom)
        assert [x.uri for x in r.list_resources({})] == ["x://static"]


# ==========================================
# OVER HTTP
# ==========================================

@pytest.fixture
def server():
    s = MCPServer(name="res-test")

    @s.resource("joyverse://profile", name="Profile", mime_type="text/markdown",
                priority=0.9, audience=["user", "assistant"])
    def profile(user: dict):
        return "# Profile\nname: " + user.get("name", "anon")

    @s.resource_template("joyverse://data/{topic}", name="Data log",
                         mime_type="application/json")
    def log(uri: str, user: dict):
        return json.dumps({"uri": uri, "owner": user.get("name")})

    return s


@pytest.fixture
def client(server):
    from fastapi.testclient import TestClient
    return TestClient(server._app)


def rpc(client, method, params=None, req_id=1):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    r = client.post("/mcp", json=body)
    for line in r.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return None


class TestCapabilityDeclaration:
    def test_resources_advertised_when_present(self, client):
        caps = rpc(client, "initialize")["result"]["capabilities"]
        assert caps["resources"] == {}

    def test_absent_when_server_has_no_resources(self, client_factory):
        client, _ = client_factory()
        caps = rpc(client, "initialize")["result"]["capabilities"]
        # Key must be missing, not null: a client seeing null would still call
        # resources/list on a server that cannot answer it.
        assert "resources" not in caps


class TestListResources:
    def test_returns_registered(self, client):
        uris = [r["uri"] for r in
                rpc(client, "resources/list")["result"]["resources"]]
        assert uris == ["joyverse://profile"]

    def test_carries_mime_type_and_priority(self, client):
        r = rpc(client, "resources/list")["result"]["resources"][0]
        assert r["mimeType"] == "text/markdown"
        assert r["annotations"]["priority"] == 0.9
        assert r["annotations"]["audience"] == ["user", "assistant"]

    def test_templates_listed_separately(self, client):
        res = rpc(client, "resources/templates/list")
        assert [t["uriTemplate"] for t in res["result"]["resourceTemplates"]] == [
            "joyverse://data/{topic}"]

    def test_template_is_not_in_resources_list(self, client):
        uris = [r["uri"] for r in
                rpc(client, "resources/list")["result"]["resources"]]
        assert not any("{" in u for u in uris)


class TestReadResource:
    def test_exact_uri(self, client):
        res = rpc(client, "resources/read", {"uri": "joyverse://profile"})
        c = res["result"]["contents"][0]
        assert c["uri"] == "joyverse://profile"
        assert c["mimeType"] == "text/markdown"
        assert "Profile" in c["text"]

    def test_template_uri_is_resolved(self, client):
        res = rpc(client, "resources/read", {"uri": "joyverse://data/dsa"})
        c = res["result"]["contents"][0]
        assert c["mimeType"] == "application/json"
        assert "joyverse://data/dsa" in c["text"]

    def test_unknown_uri_is_minus_32002(self, client):
        res = rpc(client, "resources/read", {"uri": "joyverse://nope"})
        assert res["error"]["code"] == -32002
        assert res["error"]["data"]["uri"] == "joyverse://nope"

    def test_no_result_key_on_error(self, client):
        res = rpc(client, "resources/read", {"uri": "joyverse://nope"})
        assert "result" not in res


class TestResourcesRequireAuth:
    def test_401_without_a_token(self):
        from fastapi.testclient import TestClient
        from mcppro.auth import api_key_auth

        # Auth must be wired before the app is built: Depends() captures the
        # strategy when the route is registered.
        s = MCPServer(name="x", auth=api_key_auth(["k"]))

        @s.resource("x://a", name="a")
        def read(user: dict):
            return "ok"

        c = TestClient(s._app)
        for method, params in [("resources/list", None),
                              ("resources/read", {"uri": "x://a"})]:
            body = {"jsonrpc": "2.0", "id": 1, "method": method}
            if params:
                body["params"] = params
            assert c.post("/mcp", json=body).status_code == 401

    def test_resource_reads_are_scoped(self):
        from fastapi import Request
        from fastapi.testclient import TestClient

        # The parameter must be annotated as Request, or FastAPI treats it as
        # a query parameter and rejects the call with 422 before any auth runs.
        def no_scopes(request: Request):
            return {"scopes": []}

        s = MCPServer(name="x", auth=no_scopes)

        @s.resource("x://a", name="a", scopes=["read"])
        def read(user: dict):
            return "ok"

        c = TestClient(s._app)
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                  "method": "resources/read",
                                  "params": {"uri": "x://a"}})
        frame = next(l for l in r.text.splitlines() if l.startswith("data: "))
        # -32003 is the JSON-RPC forbidden code, used here because the
        # request is already inside the MCP stream.
        assert json.loads(frame[6:])["error"]["code"] == -32003
