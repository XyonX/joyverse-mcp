from mcppro.server import MCPServer
from mcppro.auth import (
    api_key_auth,
    no_auth,
    oauth_bearer_auth,
    any_auth,
    extract_bearer_token,
)
from mcppro.discovery import (
    discovery_routes,
    register_resource_metadata,
    build_metadata,
    metadata_urls,
    challenge_header,
)
from mcppro.scopes import (
    parse_scopes,
    has_scopes,
    missing_scopes,
    require_scopes,
)

__all__ = [
    "MCPServer",
    # auth strategies
    "api_key_auth",
    "no_auth",
    "oauth_bearer_auth",
    "any_auth",
    "extract_bearer_token",
    # RFC 9728 discovery
    "discovery_routes",
    "register_resource_metadata",
    "build_metadata",
    "metadata_urls",
    "challenge_header",
    # scopes
    "parse_scopes",
    "has_scopes",
    "missing_scopes",
    "require_scopes",
]