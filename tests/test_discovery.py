"""Unit tests for mcppro/discovery.py -- RFC 9728 protected resource metadata.

Covers both the pure URL/document builders and the routes mounted on a real
FastAPI app.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcppro.discovery import (
    build_metadata,
    challenge_header,
    metadata_urls,
    discovery_routes,
    register_resource_metadata,
)
from mcppro import MCPServer

RESOURCE = "https://joyverse.example.com/mcp"
ROOT = "/.well-known/oauth-protected-resource"
PATH_INSERTED = "/.well-known/oauth-protected-resource/mcp"


class TestMetadataUrls:
    def test_root_form_is_served(self):
        assert f"https://joyverse.example.com{ROOT}" in metadata_urls(RESOURCE)

    def test_path_inserted_form_is_served(self):
        # RFC 9728 s3: the resource path is inserted after the well-known
        # suffix. Clients probe for this one specifically.
        assert f"https://joyverse.example.com{PATH_INSERTED}" in metadata_urls(RESOURCE)

    def test_both_forms_are_returned(self):
        assert len(metadata_urls(RESOURCE)) == 2

    def test_root_only_resource_yields_one_url(self):
        assert metadata_urls("https://joyverse.example.com") == [
            f"https://joyverse.example.com{ROOT}"]

    def test_query_string_is_stripped(self):
        assert not any("?" in u for u in metadata_urls("https://h.example/mcp?x=1"))

    def test_fragment_is_stripped(self):
        assert not any("#" in u for u in metadata_urls("https://h.example/mcp#f"))

    def test_trailing_slash_does_not_duplicate(self):
        urls = metadata_urls("https://h.example/mcp/")
        assert len(urls) == 2
        assert urls[-1].endswith(PATH_INSERTED)

    def test_no_duplicates_when_path_is_empty(self):
        assert len(set(metadata_urls("https://h.example"))) == 1


class TestBuildMetadata:
    def test_resource_is_present(self):
        assert build_metadata(RESOURCE)["resource"] == RESOURCE

    def test_authorization_servers_listed(self):
        doc = build_metadata(RESOURCE, authorization_servers=["https://as.example"])
        assert doc["authorization_servers"] == ["https://as.example"]

    def test_multiple_authorization_servers(self):
        doc = build_metadata(RESOURCE,
                             authorization_servers=["https://a", "https://b"])
        assert doc["authorization_servers"] == ["https://a", "https://b"]

    def test_self_hosted_as_omits_the_field(self):
        # RFC 9728 s2: when the RS is also the AS, authorization_servers must
        # be absent -- not null, and not self-referential.
        assert "authorization_servers" not in build_metadata(
            RESOURCE, authorization_servers=[])

    def test_absent_fields_are_omitted_not_null(self):
        # a null scopes_supported reads as malformed to strict clients
        doc = build_metadata(RESOURCE, bearer_methods_supported=None)
        assert "scopes_supported" not in doc
        assert "bearer_methods_supported" not in doc

    def test_omitted_fields_are_never_null(self):
        # exclude_none means absent, never None
        doc = build_metadata(RESOURCE, bearer_methods_supported=None)
        assert all(v is not None for v in doc.values())

    def test_scopes_are_listed(self):
        assert build_metadata(
            RESOURCE, scopes_supported=["read", "write"]
        )["scopes_supported"] == ["read", "write"]

    def test_bearer_methods_default_to_header(self):
        assert build_metadata(RESOURCE)["bearer_methods_supported"] == ["header"]

    def test_documentation_is_included_when_given(self):
        assert build_metadata(
            RESOURCE, resource_documentation="https://docs"
        )["resource_documentation"] == "https://docs"

    def test_tuple_input_is_normalised_to_list(self):
        assert build_metadata(
            RESOURCE, authorization_servers=("https://a",)
        )["authorization_servers"] == ["https://a"]


class TestChallengeHeader:
    def test_is_a_bearer_challenge(self):
        assert challenge_header(RESOURCE).startswith("Bearer ")

    def test_points_at_the_metadata_url(self):
        assert "resource_metadata=" in challenge_header(RESOURCE)

    def test_references_the_path_inserted_form(self):
        # a client must be able to GET the URL we hand it
        assert "oauth-protected-resource/mcp" in challenge_header(RESOURCE)

    def test_default_error_is_invalid_token(self):
        assert 'error="invalid_token"' in challenge_header(RESOURCE)

    def test_error_can_be_overridden(self):
        assert 'error="invalid_request"' in challenge_header(
            RESOURCE, error="invalid_request")

    def test_description_is_included_when_given(self):
        assert 'error_description="token expired"' in challenge_header(
            RESOURCE, error_description="token expired")


def _app_with(resource_url, **kwargs):
    """A bare FastAPI app with discovery mounted directly."""
    app = FastAPI()
    register_resource_metadata(app, resource_url, **kwargs)
    return app


class TestDiscoveryOverHttp:
    """The metadata must be reachable over real HTTP, not just as a dict."""

    def _client(self, **kwargs):
        router = discovery_routes(RESOURCE, **kwargs)
        return TestClient(MCPServer(name="t", extra_routes=[router])._app)

    def test_root_url_is_served(self):
        assert self._client().get(ROOT).status_code == 200

    def test_path_inserted_url_is_served(self):
        assert self._client().get(PATH_INSERTED).status_code == 200

    def test_both_urls_return_the_same_document(self):
        c = self._client()
        assert c.get(ROOT).json() == c.get(PATH_INSERTED).json()

    def test_document_is_json(self):
        assert self._client().get(ROOT).json()["resource"] == RESOURCE

    def test_cors_allows_any_origin(self):
        # browser-based clients (Claude web, ChatGPT) read this cross-origin
        r = self._client().get(ROOT)
        assert r.headers["access-control-allow-origin"] == "*"

    def test_options_preflight_is_answered(self):
        r = self._client().options(ROOT)
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "*"

    def test_metadata_is_cacheable(self):
        assert "max-age" in self._client().get(ROOT).headers["cache-control"]

    def test_unknown_sub_path_is_404(self):
        assert self._client().get(f"{ROOT}/nope").status_code == 404

    def test_scopes_are_advertised_over_http(self):
        r = self._client(scopes_supported=["read", "write"]).get(ROOT)
        assert r.json()["scopes_supported"] == ["read", "write"]

    def test_authorization_servers_are_advertised_over_http(self):
        r = self._client(authorization_servers=["https://as.example"]).get(ROOT)
        assert r.json()["authorization_servers"] == ["https://as.example"]

    def test_metadata_needs_no_authentication(self):
        # it names no user and grants no access, so it must stay unprotected
        def forbidden(request):
            raise AssertionError("metadata must not be authenticated")

        srv = MCPServer(name="t", auth=forbidden,
                        extra_routes=[discovery_routes(RESOURCE)])
        assert TestClient(srv._app).get(ROOT).status_code == 200

    def test_router_and_direct_mount_agree(self):
        via_router = self._client().get(ROOT).json()
        via_app = TestClient(_app_with(RESOURCE)).get(ROOT).json()
        assert via_router == via_app

    def test_direct_mount_returns_mounted_urls(self):
        urls = register_resource_metadata(FastAPI(), RESOURCE)
        assert f"https://joyverse.example.com{PATH_INSERTED}" in urls

    def test_direct_mount_serves_the_document(self):
        assert TestClient(_app_with(RESOURCE)).get(PATH_INSERTED).status_code == 200


def _route_paths(app):
    """Paths served by an app.

    Filtering on `path` skips the _IncludedRouter marker starlette keeps for
    mounted sub-routers, which has no path of its own.
    """
    return [r.path for r in app.routes if hasattr(r, "path")]


class TestDiscoveryIsOptIn:
    def test_no_routes_when_not_configured(self):
        assert ROOT not in _route_paths(MCPServer(name="t")._app)

    def test_framework_exposes_no_app_specific_names(self):
        # the framework must stay provider- and app-agnostic
        import mcppro
        assert not any("joyverse" in n.lower() for n in dir(mcppro))

    def test_health_still_works_with_discovery_mounted(self):
        srv = MCPServer(name="t", extra_routes=[discovery_routes(RESOURCE)])
        r = TestClient(srv._app).get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_mcp_route_survives_discovery_mount(self):
        srv = MCPServer(name="t", extra_routes=[discovery_routes(RESOURCE)])
        assert "/mcp" in _route_paths(srv._app)

    def test_two_servers_do_not_share_routes(self):
        # Asserted over HTTP rather than via app.routes: starlette keeps
        # included routers behind a marker instead of flattening them, so
        # route introspection cannot see mounted routes at all.
        a = TestClient(MCPServer(name="a", extra_routes=[discovery_routes(RESOURCE)])._app)
        b = TestClient(MCPServer(name="b")._app)
        assert a.get(ROOT).status_code == 200
        assert b.get(ROOT).status_code == 404

    def test_extra_routes_cannot_shadow_mcp(self):
        # /mcp is registered after the mounts, so a duplicate wins
        srv = MCPServer(name="t", extra_routes=[discovery_routes(RESOURCE)])
        r = TestClient(srv._app).post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                                    "method": "tools/list"})
        assert r.status_code == 200