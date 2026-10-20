"""RFC 9728 OAuth 2.0 Protected Resource Metadata discovery.

An MCP client that finds a 401 has no way to learn where to send the user to
log in unless the resource server advertises it. These routes are that
advertisement, and they are what make a plain `POST /mcp` URL usable from
Claude, Cursor and ChatGPT without the user hand-editing a bearer token.

The document is public by design -- it names no user and grants no access --
so it is served with permissive CORS, which browser-based clients need in
order to read it cross-origin. Nothing else in the server gets that header.
"""
from typing import Dict, List, Optional, Sequence

from fastapi.responses import JSONResponse

from mcppro.types import MCPOAuthMetadata

# RFC 9728 s3: the metadata path is the well-known suffix with the resource's
# path *inserted* before it. For https://host/mcp that is
# https://host/.well-known/oauth-protected-resource/mcp
WELL_KNOWN_SUFFIX = "/.well-known/oauth-protected-resource"

# Metadata is unauthenticated public information, so any origin may read it.
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Mcp-Session-Id",
    "Cache-Control": "public, max-age=3600",
}


def _split_resource_url(resource_url: str):
    """Break a resource URL into (origin, path).

    Returns origin="" when the input is not an absolute URL, which lets the
    path helpers degrade to plain paths for tests and local use.
    """
    scheme_split = resource_url.split("://", 1)
    if len(scheme_split) != 2:
        return "", resource_url.lstrip("/")

    scheme, rest = scheme_split
    # A resource URL may carry a query or fragment; they are not part of
    # the metadata path.
    rest = rest.split("?")[0].split("#")[0]
    authority, _, path = rest.partition("/")
    return f"{scheme}://{authority}", path.strip("/")


def metadata_urls(resource_url: str) -> List[str]:
    """Both URL forms clients may probe: root and path-inserted.

    The RFC defines the path-inserted form, but clients in the wild also probe
    the bare well-known root, so both are served from the same document.
    """
    origin, path = _split_resource_url(resource_url)
    urls = [f"{origin}{WELL_KNOWN_SUFFIX}"]
    if path:
        urls.append(f"{origin}{WELL_KNOWN_SUFFIX}/{path}")

    # Preserve order, drop duplicates (a root-only resource yields one URL).
    seen = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def _as_list(value: Optional[Sequence[str]]) -> Optional[List[str]]:
    """Normalise a sequence-or-None field into a list-or-None.

    An empty sequence becomes None so the key is omitted from the document
    entirely, rather than serialising as an empty list that a strict client
    would read as "this provider supports nothing".
    """
    if not value:
        return None
    return list(value)


def build_metadata(
    resource_url: str,
    authorization_servers: Optional[Sequence[str]] = None,
    scopes_supported: Optional[Sequence[str]] = None,
    bearer_methods_supported: Optional[Sequence[str]] = ("header",),
    resource_documentation: Optional[str] = None,
) -> Dict:
    """Build the protected resource metadata document.

    `authorization_servers` is omitted when this server is also its own
    authorization server; the RFC requires the field to be absent in that
    case rather than pointing back at ourselves.
    """
    metadata = MCPOAuthMetadata(
        resource=resource_url,
        authorization_servers=_as_list(authorization_servers),
        scopes_supported=_as_list(scopes_supported),
        bearer_methods_supported=_as_list(bearer_methods_supported),
        resource_documentation=resource_documentation,
    )
    # exclude_none so absent fields are genuinely absent rather than null --
    # clients treat a null AS list as malformed.
    return metadata.model_dump(exclude_none=True)


def challenge_header(resource_url: str, error: str = "invalid_token",
                     error_description: Optional[str] = None) -> str:
    """The WWW-Authenticate value to put on a 401.

    RFC 9728 s5.1: `resource_metadata` is how the client learns where to fetch
    the discovery document. Without it a 401 is a dead end.
    """
    target = metadata_urls(resource_url)[-1]
    parts = [f'Bearer resource_metadata="{target}"', f'error="{error}"']
    if error_description:
        parts.append(f'error_description="{error_description}"')
    return ", ".join(parts)


def _mount(router, resource_url, authorization_servers, scopes_supported,
           bearer_methods_supported, resource_documentation):
    """Register the metadata GET/OPTIONS handlers on an APIRouter.

    The document is built once at registration time: it is static config, and
    rebuilding it per request would mean re-serialising on every probe.
    """
    document = build_metadata(
        resource_url=resource_url,
        authorization_servers=authorization_servers,
        scopes_supported=scopes_supported,
        bearer_methods_supported=bearer_methods_supported,
        resource_documentation=resource_documentation,
    )

    for url in metadata_urls(resource_url):
        # _split_resource_url already returns the full path component of the
        # metadata URL, well-known suffix included. Prefixing it again here
        # would produce /.../oauth-protected-resource/oauth-protected-resource.
        _, path = _split_resource_url(url)
        path = "/" + path.strip("/")

        # The document is bound as a default argument so the closure keeps
        # returning this route's own metadata rather than the last one
        # registered.
        @router.get(path, include_in_schema=False)
        async def _metadata(_document=document):
            return JSONResponse(_document, headers=CORS_HEADERS)

        # CORS preflight: browser clients probe with OPTIONS before GET.
        @router.options(path, include_in_schema=False)
        async def _metadata_options():
            return JSONResponse({}, headers=CORS_HEADERS)

    return router


def discovery_routes(
    resource_url: str,
    authorization_servers: Optional[Sequence[str]] = None,
    scopes_supported: Optional[Sequence[str]] = None,
    bearer_methods_supported: Optional[Sequence[str]] = ("header",),
    resource_documentation: Optional[str] = None,
) -> "APIRouter":
    """Build an APIRouter carrying the metadata routes.

    Pass it to `MCPServer(extra_routes=[...])`.
    """
    from fastapi import APIRouter

    return _mount(APIRouter(), resource_url, authorization_servers,
                  scopes_supported, bearer_methods_supported,
                  resource_documentation)


def register_resource_metadata(
    app,
    resource_url: str,
    authorization_servers: Optional[Sequence[str]] = None,
    scopes_supported: Optional[Sequence[str]] = None,
    bearer_methods_supported: Optional[Sequence[str]] = ("header",),
    resource_documentation: Optional[str] = None,
) -> List[str]:
    """Mount the metadata routes directly on a FastAPI app.

    Returns the absolute URLs now being served.
    """
    _mount(app, resource_url, authorization_servers, scopes_supported,
           bearer_methods_supported, resource_documentation)
    return metadata_urls(resource_url)