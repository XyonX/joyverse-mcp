from mcppro import MCPServer, any_auth, discovery_routes

from joyverse.config import (
    OAUTH_ENABLED, AUTH0_DOMAIN, AUTH0_AUDIENCE, PUBLIC_BASE_URL,
    READ_SCOPE, WRITE_SCOPE, SUPPORTED_SCOPES, resource_url,
)
from joyverse import config as jv_config
from joyverse.profile import get_profile, update_profile, delete_profile_field
from joyverse.bio import get_bio, update_bio, delete_bio_section
from joyverse.memory import (
    get_memory, add_memory_trait, update_focus, add_observed_pattern,
    update_preference, update_context, delete_memory_item,
)
from joyverse.data import (
    get_data, list_topics, edit_data, replace_data, restore_data, delete_data,
)
from joyverse.search import search
from joyverse.account import whoami
from joyverse.history import list_history, restore_version
from joyverse.storage import (
    register_client, list_clients, save_file_from_url, save_file_text,
    get_file, list_files, delete_file,
)
from joyverse.uploads import save_file_base64
from joyverse.logs import (
    add_to_log, get_log, list_log_days, add_to_agent_log, get_agent_log,
    delete_log_entry,
)
from joyverse.prompts import build_instructions
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
    #
    # with_client adds which agent is calling, read from the connection, so
    # tools do not depend on the model knowing its own name.
    return jv_auth.with_client(any_auth(*strategies))


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
    extra_routes=build_discovery_routes(),
    resource_url=_RESOURCE_URL,
    max_body_bytes=jv_config.MAX_REQUEST_BYTES,
)

# Orientation
server.tool(description=(
    "Who is calling: the account, whether you may write, the client name the "
    "server knows you by (`you_are`), the user's timezone and today's date "
    "for them, and a count of what is stored. Call it once at the start of a "
    "session instead of guessing your own name or the date."),
    scopes=[READ_SCOPE])(whoami)

server.tool(description=(
    "Find something by content when you do not know where or when it was "
    "stored. Searches the user's log, data topics, profile, bio, memory and "
    "file names for the words in `query` (every word must appear; if nothing "
    "has them all, entries with any of them are returned and the reply says "
    "so). Use this BEFORE guessing a date for get_log or a topic for "
    "get_data. Narrow with where (log, data, profile, bio, memory, files, "
    "journals) and, for the log, since/until dates. Agents' journals are "
    "searched only when where includes \"journals\" or is \"all\"."),
    scopes=[READ_SCOPE],
    param_docs={
        "query": "A word or short phrase, e.g. 'pricing page' or 'graphs'.",
        "where": "Areas to search. Leave out for everything except journals.",
        "since": "Only log entries on or after this date (YYYY-MM-DD).",
        "until": "Only log entries on or before this date (YYYY-MM-DD).",
    })(search)

# Profile
server.tool(description="Get the user's personal profile",
            scopes=[READ_SCOPE])(get_profile)
server.tool(description="Update a field in the user profile",
            scopes=[WRITE_SCOPE])(update_profile)

server.tool(description=(
    "DESTRUCTIVE: remove one `key: value` line or one whole `## Section` "
    "from the profile. Only when the user asks for it to be removed. The "
    "version before the removal is kept; restore_version brings it back."),
    scopes=[WRITE_SCOPE])(delete_profile_field)

# Bio
server.tool(description="Get the user's detailed life narrative",
            scopes=[READ_SCOPE])(get_bio)
server.tool(description="Write or replace a section of the user's bio",
            scopes=[WRITE_SCOPE])(update_bio)

server.tool(description=(
    "DESTRUCTIVE: remove one `## Section` of the bio, heading and body. Only "
    "when the user asks. The version before the removal is kept; "
    "restore_version brings it back."),
    scopes=[WRITE_SCOPE])(delete_bio_section)

# History for the three whole-document stores. Data topics have restore_data.
server.tool(description=(
    "List the earlier versions kept for a document: profile, bio or memory. "
    "One is kept every time the document changes, the newest ten."),
    scopes=[READ_SCOPE])(list_history)
server.tool(description=(
    "Undo a change to the profile, bio or memory by bringing back an earlier "
    "version -- the most recent one unless you pass a `version` from "
    "list_history. The version it replaces is kept too, so this can itself "
    "be undone. For a data topic use restore_data instead."),
    scopes=[WRITE_SCOPE])(restore_version)

# Memory
server.tool(description="Get the user's LLM memory model",
            scopes=[READ_SCOPE])(get_memory)
server.tool(description="Add a personality trait to memory",
            scopes=[WRITE_SCOPE])(add_memory_trait)
server.tool(description="Update the current main focus",
            scopes=[WRITE_SCOPE])(update_focus)
server.tool(description=(
    "Record a recurring behaviour you have noticed across conversations, "
    "e.g. 'asks for the reasoning before the answer'. Distinct from a "
    "personality trait: a pattern is something they DO."),
    scopes=[WRITE_SCOPE])(add_observed_pattern)
server.tool(description=(
    "Record how the user likes things delivered. name is one of "
    "explanation_style, code_style, feedback_style."),
    scopes=[WRITE_SCOPE])(update_preference)
server.tool(description=(
    "Update the user's current context. field is one of main_focus, "
    "immediate_next (the next concrete step), mood."),
    scopes=[WRITE_SCOPE])(update_context)

server.tool(description=(
    "DESTRUCTIVE: remove one personality trait or observed pattern from "
    "memory -- one that is wrong or no longer true. kind is 'trait' or "
    "'pattern'; text is the entry, or enough of it to identify exactly one. "
    "restore_version(document='memory') undoes it."),
    scopes=[WRITE_SCOPE])(delete_memory_item)

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

server.tool(description=(
    "Undo the last write to a data log by bringing back the version it "
    "replaced. Use this when an edit_data or replace_data call went wrong. "
    "One level of undo: calling it again redoes the change."),
    scopes=[WRITE_SCOPE])(restore_data)

server.tool(description=(
    "DESTRUCTIVE: delete a whole data topic. Only when the user asks for the "
    "topic to be removed -- to remove one item inside a topic use edit_data "
    "with op=remove. The deleted log is kept as the topic's previous "
    "version, so restore_data(topic) brings it back."),
    scopes=[WRITE_SCOPE])(delete_data)


# ==========================================
# FILES
#
# A private per-user store. Clients (chatgpt, claude, hermes) each get their
# own folder and can hand files to one another: one agent saves an image, a
# different one fetches it. Sub-folders inside a client are free-form.
# ==========================================

server.tool(description=(
    "Claim a client name. You rarely need this: the server usually knows "
    "which assistant is calling and registers it on first use -- call "
    "list_clients and check `you_are`. Register only when `you_are` is empty. "
    "If your name is already registered, reuse it instead of registering "
    "again -- a second name splits your files across folders. Use the plain "
    "product name (chatgpt, claude, hermes) and keep it stable across "
    "sessions."),
    scopes=[WRITE_SCOPE])(register_client)

server.tool(description=(
    "List the client names in use, with their storage folders. `you_are` is "
    "the one the server already knows you to be -- when it is set you can "
    "leave the client argument out of every other tool."),
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
    "Store a file you already hold -- an image or document attached to this "
    "conversation, or something you generated -- by sending its bytes as "
    "base64. This is the way to store a file that has no public URL: "
    "save_file_from_url needs one, and save_file_text only handles plain text. "
    "Keep it small (a few MB); the bytes are charged to your context window, so "
    "for anything large host it publicly and use save_file_from_url."),
    scopes=[WRITE_SCOPE])(save_file_base64)

# The user's log and the agents' journals are separate on purpose: the log
# answers "what have I been doing", and an autonomous agent ticking every few
# minutes used to write almost all of it. The descriptions draw the line, and
# add_to_log also enforces it by filing operational entries in the caller's
# journal.
#
# `client` used to be the argument a model guessed wrong -- it does not
# reliably know its own name, and in an agency-style conversation the word
# reads as "who the work is for". The server now works the name out from the
# connection, so the argument is optional and described as an override.
_CLIENT_DOC = (
    "Usually leave this out: the server already knows which assistant is "
    "calling. If you must set it, it is YOUR own name as the assistant "
    "(chatgpt, claude, atlas...), exactly as list_clients shows it -- NOT the "
    "project, brand or person the work is about; that goes in tags.")

server.tool(description=(
    "Record something the USER did, so a later conversation can recall it: "
    "'practised graph problems', 'shipped the FlexyGrid pricing page'. This "
    "is the user's own activity log, read back when they ask what they have "
    "been doing -- so one entry per meaningful thing, written about THEM. Do "
    "NOT use it for your own working notes, status checks, routing ticks or "
    "anything about what you as an agent did: those go in add_to_agent_log. "
    "Make the summary specific about what was done AND what it was about, "
    "and add tags so it can be found later. client is YOUR own name as an "
    "assistant, never the project or brand the work was about -- that goes "
    "in tags."),
    scopes=[WRITE_SCOPE],
    param_docs={"client": _CLIENT_DOC})(add_to_log)

server.tool(description=(
    "Write a note in YOUR OWN journal -- the agent's log book. Use this for "
    "anything about your work rather than the user's: a routing or "
    "monitoring tick, a status sweep, what you tried, a handoff for your "
    "next session. It is stored in your own storage folder under journal/ "
    "and never appears in the user's log. Summaries can be long (4000 "
    "characters)."),
    scopes=[WRITE_SCOPE],
    param_docs={"client": _CLIENT_DOC})(add_to_agent_log)

server.tool(description=(
    "Read an agent's journal -- yours by default. No arguments gives the "
    "last 24 hours; pass date for one day or since/until for a range. Pass "
    "client to read another agent's journal, which is how you pick up where "
    "another agent left off."),
    scopes=[READ_SCOPE],
    param_docs={"client": "Whose journal to read. Leave out for your own."})(
        get_agent_log)

server.tool(description=(
    "Read back the USER'S log -- what they did. With no arguments, returns the "
    "last 24 hours -- use that for 'what have I been doing'. Agents' own "
    "journals are separate; read those with get_agent_log. Pass date for one whole day, or "
    "since/until for a range such as 12pm-2pm; both accept a bare date "
    "(2026-10-01) or a full timestamp. Check has_more: if it is true you are "
    "seeing a truncated view, so widen the window or raise limit."),
    scopes=[READ_SCOPE])(get_log)

server.tool(description=(
    "See which days have a log recorded, newest first. Call this FIRST when you "
    "do not know whether something was logged -- it avoids guessing a date. "
    "Pass include_counts=true to also get how many entries each day holds."),
    scopes=[READ_SCOPE])(list_log_days)

server.tool(description=(
    "DESTRUCTIVE: remove one entry from the user's log -- a mistake, a "
    "duplicate, or something they want forgotten. Pass ts exactly as get_log "
    "returned it; if two entries share that timestamp, add match with a few "
    "words from the summary. Set journal=true to remove from your own "
    "journal instead. The removed entry is returned in the reply."),
    scopes=[WRITE_SCOPE],
    param_docs={
        "ts": "The entry's timestamp, exactly as get_log returned it.",
        "match": "Words from the summary, when several entries share ts.",
        "journal": "true to remove from an agent journal, not the user's log.",
        "client": "Whose journal. Leave out for your own.",
    })(delete_log_entry)

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
# TOOL ANNOTATIONS
#
# Hints a client uses to decide what to run freely and what to confirm with
# the user. Assigned from one table rather than at each registration, and
# every tool must appear in it: a new tool with no classification fails at
# start-up instead of being silently advertised as "might do anything".
#
# "Destructive" here means it removes or overwrites something the caller did
# not name piece by piece. The update_* and edit tools replace one value and
# keep the version they replaced, so they are not marked.
# ==========================================

_READ_ONLY = {
    "whoami", "search", "get_profile", "get_bio", "get_memory", "get_data",
    "list_topics", "list_history", "list_clients", "get_log", "get_agent_log",
    "list_log_days", "get_file", "list_files",
}
_ADDITIVE = {
    "add_memory_trait", "add_observed_pattern", "add_to_log",
    "add_to_agent_log", "register_client",
}
_REPLACING = {
    "update_profile", "update_bio", "update_focus", "update_preference",
    "update_context", "edit_data", "restore_data", "restore_version",
    "save_file_text", "save_file_base64", "save_file_from_url",
}
_DESTRUCTIVE = {
    "replace_data", "delete_data", "delete_file", "delete_profile_field",
    "delete_bio_section", "delete_memory_item", "delete_log_entry",
}
# Calling these twice with the same arguments leaves the same state.
_IDEMPOTENT = (_REPLACING | _DESTRUCTIVE | {"register_client"}) - {
    "edit_data", "restore_data", "restore_version"}
# The only tool that reaches outside this server.
_OPEN_WORLD = {"save_file_from_url"}


def _annotate(tool_server):
    unclassified = []
    for schema in tool_server._tool_schemas:
        name = schema.name
        if name in _READ_ONLY:
            hints = {"readOnlyHint": True}
        elif name in _ADDITIVE | _REPLACING | _DESTRUCTIVE:
            hints = {"readOnlyHint": False,
                     "destructiveHint": name in _DESTRUCTIVE,
                     "idempotentHint": name in _IDEMPOTENT}
        else:
            unclassified.append(name)
            continue
        hints["openWorldHint"] = name in _OPEN_WORLD
        hints["title"] = name.replace("_", " ").capitalize()
        schema.annotations = hints
    if unclassified:
        raise RuntimeError(
            "Tools with no annotation class in run.py: "
            + ", ".join(unclassified))


_annotate(server)


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
    from joyverse.data import (
        _topic_names, _describe, _topic_from_key, _data_prefix, _is_internal,
    )
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
                if _is_internal(obj.get("Key", "")):
                    continue
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


# Assigned here rather than in the constructor: build_instructions reads the
# tool registry, and the registry is only complete once every tool() call
# above has run. A tool registered without appearing in the instructions is
# the exact drift this ordering prevents.
server.instructions = build_instructions(server)


if __name__ == "__main__":
    server.run(port=8001)