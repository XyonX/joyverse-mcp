from mcppro import MCPServer, any_auth, discovery_routes

from joyverse.config import (
    OAUTH_ENABLED, AUTH0_DOMAIN, AUTH0_AUDIENCE, PUBLIC_BASE_URL,
    READ_SCOPE, WRITE_SCOPE, SUPPORTED_SCOPES, resource_url,
)
from joyverse import config as jv_config
from joyverse.profile import get_profile, update_profile
from joyverse.bio import get_bio, update_bio
from joyverse.memory import get_memory, add_memory_trait, update_focus
from joyverse.data import get_data, update_data, list_topics
from joyverse.prompts import USER_DATA
from joyverse import auth as jv_auth


def build_auth():
    """Assemble the auth chain from whatever is actually configured.

    OAuth is tried first so a genuine provider token is never mistaken for a
    self-issued one. Bearer stays in the chain regardless, so an operator can
    always mint a local token even once OAuth is live -- that is the recovery
    path when the provider is misconfigured or unreachable.
    """
    strategies = []
    if OAUTH_ENABLED and AUTH0_DOMAIN and AUTH0_AUDIENCE:
        strategies.append(jv_auth.oauth_auth)
    strategies.append(jv_auth.jwt_auth)

    # One strategy is still wrapped, so the auth_method tagging and the
    # "first error wins" behaviour are identical in both configurations.
    return any_auth(*strategies)


def build_discovery_routes():
    """Mount RFC 9728 metadata so real MCP clients can find the login page.

    Returns an empty list when OAuth is off, which is what keeps a
    bearer-only deployment serving exactly what it did before.
    """
    if not (OAUTH_ENABLED and PUBLIC_BASE_URL and AUTH0_DOMAIN):
        return []

    # The issuer comes from config.authorization_servers() rather than being
    # rebuilt here. It used to be issuer_url().rstrip("/") inline, which
    # dropped the trailing slash Auth0's issuer carries -- and every MCP
    # client then refused to connect with an "issuer mismatch" error. One
    # definition, used by both discovery and token validation.
    return [discovery_routes(
        resource_url=resource_url(),
        authorization_servers=jv_config.authorization_servers(),
        scopes_supported=SUPPORTED_SCOPES,
    )]


# resource_url is only advertised when OAuth is on; otherwise a plain bearer
# server keeps its original, unchallenged 401 responses.
_RESOURCE_URL = None
if OAUTH_ENABLED and PUBLIC_BASE_URL:
    try:
        _RESOURCE_URL = resource_url()
    except RuntimeError:
        _RESOURCE_URL = None


server = MCPServer(
    name="joyverse-mcp",
    version="1.0.0",
    auth=build_auth(),                    # OAuth first, then bearer
    instructions=USER_DATA,
    extra_routes=build_discovery_routes(),
    resource_url=_RESOURCE_URL,
)

# Profile
server.tool(description="Get the user's personal profile",
            scopes=[READ_SCOPE])(get_profile)
server.tool(description="Update a field in the user profile",
            scopes=[WRITE_SCOPE])(update_profile)

# Bio
server.tool(description="Get the user's detailed life narrative",
            scopes=[READ_SCOPE])(get_bio)
server.tool(description="Write or replace a section of the user's bio",
            scopes=[WRITE_SCOPE])(update_bio)

# Memory
server.tool(description="Get the user's LLM memory model",
            scopes=[READ_SCOPE])(get_memory)
server.tool(description="Add a personality trait to memory",
            scopes=[WRITE_SCOPE])(add_memory_trait)
server.tool(description="Update the current main focus",
            scopes=[WRITE_SCOPE])(update_focus)

# Data Logs
server.tool(description="Get structured data logs by topic (e.g., dsa, projects)",
            scopes=[READ_SCOPE])(get_data)
# Listed first in the description set: an agent must be able to discover which
# topics exist before it can ask for one. Without it, callers guess names and
# a wrong guess returns nothing they can recover from.
server.tool(description=(
    "List every stored data topic with its description. Call this FIRST when "
    "you do not know which topics exist, rather than guessing a topic name."),
    scopes=[READ_SCOPE])(list_topics)
server.tool(description="Update structured data logs for a topic",
            scopes=[WRITE_SCOPE])(update_data)


if __name__ == "__main__":
    server.run(port=8001)