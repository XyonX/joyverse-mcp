from mcppro import MCPServer, any_auth, discovery_routes

from joyverse.config import (
    OAUTH_ENABLED, AUTH0_DOMAIN, AUTH0_AUDIENCE, PUBLIC_BASE_URL,
    READ_SCOPE, WRITE_SCOPE, SUPPORTED_SCOPES, resource_url,
)
from joyverse import config as jv_config
from joyverse.profile import get_profile, update_profile
from joyverse.bio import get_bio, update_bio
from joyverse.memory import get_memory, add_memory_trait, update_focus
from joyverse.data import get_data, list_topics, edit_data, replace_data
from joyverse.storage import (
    register_client, list_clients, save_file_from_url, save_file_text,
    get_file, list_files, delete_file,
)
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
# edit_data is the safe default for changing a data log. The description
# spells out the addressing rule, because an agent that reaches for an array
# index will edit the wrong item the moment anything is added or removed.
server.tool(description=(
    "Make a targeted change to one data log without disturbing anything else. "
    "Choose op: set = change fields; add = insert a new item into an array; "
    "remove = delete the item that matches `match`; append = add entries to a "
    "list inside the matched item. Address items by matching a field such as "
    "{\"name\": \"OmniHome\"}, NEVER by array index -- indices shift when "
    "items are added or removed. Always call get_data first so you use the "
    "exact existing names."),
    scopes=[WRITE_SCOPE])(edit_data)

server.tool(description=(
    "DESTRUCTIVE: overwrites a whole data log with what you send, so "
    "anything you omit is DELETED. Only for a deliberate full rewrite, such "
    "as a first write or a complete restructure. For any partial change use "
    "edit_data instead. Refuses the write if it would drop existing keys "
    "unless you pass allow_drop."),
    scopes=[WRITE_SCOPE])(replace_data)


# ==========================================
# FILES
#
# A private per-user store. Clients (chatgpt, claude, hermes) each get their
# own folder and can hand files to one another: one agent saves an image, a
# different one fetches it. Sub-folders inside a client are free-form.
# ==========================================

server.tool(description=(
    "Claim a client name so it can save and fetch files. Call this once per "
    "agent before any file tool; the name becomes your storage folder and is "
    "yours alone -- another user may hold the same name with no overlap. "
    "Pick a stable name you will keep using."),
    scopes=[WRITE_SCOPE])(register_client)

server.tool(description=(
    "List the client names you have registered, with their storage folders."),
    scopes=[READ_SCOPE])(list_clients)

server.tool(description=(
    "Download a public URL and store the file for later. Use this for any file "
    "type -- image, document, audio, video. Saves into your client's folder "
    "under `path`, where sub-folders like images/ or renders/ are yours to "
    "organise. Refuses private and internal addresses, oversized files, and "
    "links that resolve to a sign-in page rather than the file."),
    scopes=[WRITE_SCOPE])(save_file_from_url)

server.tool(description=(
    "Save text you produced yourself as a file -- notes, JSON, code, a summary "
    "-- without going through a URL. Use save_file_from_url instead when the "
    "content already exists somewhere fetchable."),
    scopes=[WRITE_SCOPE])(save_file_text)

server.tool(description=(
    "Get a temporary download link for a file you stored earlier. The link is "
    "signed and expires in 7 days; call again for a fresh one. Use this to "
    "read a file another client saved."),
    scopes=[READ_SCOPE])(get_file)

server.tool(description=(
    "List stored files and your storage usage. Pass client to list one "
    "client's files, or omit it to list everything you have stored."),
    scopes=[READ_SCOPE])(list_files)

server.tool(description=(
    "DESTRUCTIVE: permanently deletes a stored file and frees its space. There "
    "is no undo and no trash -- confirm with the user first."),
    scopes=[WRITE_SCOPE])(delete_file)


# ==========================================
# RESOURCES
#
# Tools are actions the caller invokes; resources are addressable things a
# client reads. Both matter here: tools are what an agent drives, while
# resources let a client BROWSE the data and decide what belongs in context
# using the priority and audience annotations.
#
# A custom scheme rather than https:// because the spec reserves https:// for
# resources a client can fetch itself -- ours require auth through the server.
# ==========================================

@server.resource(
    "joyverse://profile", name="Profile", title="Personal profile",
    description="Identity snapshot: name, location, occupation, and any "
                "other sections kept here.",
    mime_type="text/markdown", priority=0.9,
    audience=["user", "assistant"], scopes=[READ_SCOPE])
def read_profile_resource(user: dict) -> str:
    return get_profile(user)


@server.resource(
    "joyverse://bio", name="Biography", title="Life narrative",
    description="The user's story in their own words, grouped into sections.",
    mime_type="text/markdown", priority=0.8,
    audience=["user", "assistant"], scopes=[READ_SCOPE])
def read_bio_resource(user: dict) -> str:
    return get_bio(user)


@server.resource(
    "joyverse://memory", name="Memory", title="User memory model",
    description="Personality traits, observed patterns, stated preferences "
                "and current focus.",
    mime_type="application/json", priority=0.9,
    audience=["assistant"], scopes=[READ_SCOPE])
def read_memory_resource(user: dict) -> str:
    return get_memory(user)


@server.resource(
    "joyverse://topics", name="Topic catalogue", title="Available data topics",
    description="Every stored data topic with a description of what it holds. "
                "Read this before fetching a topic you were not given.",
    mime_type="application/json", priority=0.7,
    audience=["assistant"], scopes=[READ_SCOPE])
def read_topics_resource(user: dict) -> str:
    return list_topics(user)


@server.resource_template(
    "joyverse://data/{topic}", name="Data log", title="Structured data log",
    description="One stored topic, e.g. joyverse://data/dsa. Call "
                "joyverse://topics first to see which exist.",
    mime_type="application/json", priority=0.5,
    audience=["assistant"], scopes=[READ_SCOPE])
def read_data_resource(uri: str, user: dict) -> str:
    # The topic comes from the URI, but it is passed through get_data, which
    # sanitises it. The user_id in the storage key comes from the auth
    # context, never from the URI, so a crafted URI cannot reach another
    # account's data.
    topic = uri.rsplit("/", 1)[-1]
    return get_data(topic, user)


def _live_data_topics(user: dict) -> list:
    """Contribute one resource per topic the caller actually owns.

    Without this a browsing client sees only the template and has to guess
    URIs; with it the tree matches the caller's real data.
    """
    from joyverse.data import _topic_names, _describe, _topic_from_key, _data_prefix
    from joyverse.config import r2_client as _r2, BUCKET_NAME as _bucket

    try:
        user_id = user["user_id"]
        prefix = _data_prefix(user_id)
        listing = _r2.list_objects_v2(Bucket=_bucket, Prefix=prefix)
    except Exception:
        return []

    out = []
    for name in _topic_names(user["user_id"]):
        description = ""
        size = 0
        try:
            for obj in listing.get("Contents", []):
                if _topic_from_key(obj.get("Key", ""), prefix) == name:
                    body = _r2.get_object(Bucket=_bucket,
                                          Key=obj["Key"])["Body"].read().decode()
                    description = _describe(body)
                    size = obj.get("Size", 0)
                    break
        except Exception:
            pass
        out.append({
            "uri": f"joyverse://data/{name}",
            "name": name,
            "title": name.replace("_", " ").title(),
            "description": description or f"Structured data log: {name}",
            "mimeType": "application/json",
            "size": size,
            "annotations": {"audience": ["assistant"], "priority": 0.5},
        })
    return out


server.resources.set_lister(_live_data_topics)


if __name__ == "__main__":
    server.run(port=8001)