"""Who is calling: working out the agent's name without asking the model.

The `client` name decides which storage folder an agent writes to and how its
log entries are attributed. It used to come only from a tool argument, which
means from the model -- and a model does not reliably know what it is called.
The same assistant turned up as `chatgpt`, `gpt` and `openai-gpt`; one agent
as `atlas`, `hermes-atlas` and `atlas-gate`; and sometimes as the name of the
project it was working on.

So the name is taken from the connection instead, where the software sets it
and it is the same on every request:

    1. `X-Joyverse-Agent` header, or `?agent=` on the MCP URL. This is how a
       named agent identifies itself -- several agents can run on one platform
       (five Hermes profiles, say), so the platform alone cannot tell them
       apart. It is one line in that agent's MCP server config.
    2. An `agent` claim in a self-issued bearer token.
    3. The User-Agent, which identifies the product for chat apps that cannot
       be configured (ChatGPT, Claude).

None of this is a security boundary: every source resolves inside the caller's
own account, which the auth token alone decides. A wrong or forged agent name
can only mislabel that user's own files.

A name the model passes explicitly is still honoured, but it goes through the
same alias folding, so `gpt` lands in the folder `chatgpt` already owns.
"""
from typing import Iterable, Optional, Tuple

AGENT_HEADER = "x-joyverse-agent"
AGENT_QUERY_PARAMS = ("agent", "client")

# Spellings of one product. The first name is the canonical one, used when the
# user has none of them registered yet; otherwise whichever member they
# already have wins, so existing folders keep their names.
ALIAS_GROUPS = (
    ("chatgpt", "gpt", "chat-gpt", "chatgpt-app", "openai", "openai-gpt",
     "openai-chatgpt", "openai-mcp", "gpt-4", "gpt-4o", "gpt-5", "gpt4",
     "gpt5"),
    ("claude", "claude-ai", "claude.ai", "claude-chat", "claude-desktop",
     "claude-app", "anthropic", "anthropic-claude"),
    ("claude-code", "claudecode", "claude-cli"),
    ("cursor", "cursor-vscode", "cursor-ide"),
    ("hermes", "hermes-agent"),
    ("gemini", "google-gemini", "bard"),
)
_GROUP_OF = {name: group for group in ALIAS_GROUPS for name in group}

# "hermes-atlas" is the agent atlas on the hermes platform. When the bare name
# is one the user already has, the prefixed form is the same agent.
PLATFORM_PREFIXES = ("hermes-", "hermes_", "hermes.")

# Substrings of a User-Agent that identify a product, most specific first.
USER_AGENT_HINTS = (
    ("claude-code", "claude-code"),
    ("openai-mcp", "chatgpt"),
    ("chatgpt", "chatgpt"),
    ("openai", "chatgpt"),
    ("claude", "claude"),
    ("anthropic", "claude"),
    ("cursor", "cursor"),
    ("hermes", "hermes"),
    ("gemini", "gemini"),
)


def clean(name) -> str:
    """Lower-case and tidy a name without judging whether it is valid."""
    if not isinstance(name, str):
        return ""
    return "-".join(name.strip().lower().split())


def canonical(name, registered: Iterable[str] = ()) -> str:
    """Fold a name onto the one this user already uses for the same agent.

    `registered` is the user's existing client names. Order of preference:
    the exact name, then a registered spelling of the same product, then the
    product's canonical spelling, then a registered agent the name is a
    platform-prefixed form of. A name that matches nothing is returned as is.
    """
    name = clean(name)
    registered = set(registered or ())
    if not name or name in registered:
        return name

    group = _GROUP_OF.get(name)
    if group:
        for member in group:
            if member in registered:
                return member
        return group[0]

    for prefix in PLATFORM_PREFIXES:
        if name.startswith(prefix) and name[len(prefix):] in registered:
            return name[len(prefix):]
    return name


def _valid(name: str) -> Optional[str]:
    from joyverse.paths import normalise_client_name

    try:
        return normalise_client_name(name)
    except ValueError:
        return None


def detect(request, context: Optional[dict] = None) -> Tuple[Optional[str],
                                                           Optional[str]]:
    """The agent name carried by the connection, and where it came from.

    Returns (name, source) with source one of "header", "url", "token",
    "user-agent"; or (None, None) when nothing identifies the caller. A value
    that is not a usable name is skipped rather than trusted.
    """
    headers = getattr(request, "headers", {}) or {}

    name = _valid(clean(headers.get(AGENT_HEADER, "")))
    if name:
        return name, "header"

    params = getattr(request, "query_params", {}) or {}
    for param in AGENT_QUERY_PARAMS:
        name = _valid(clean(params.get(param, "")))
        if name:
            return name, "url"

    name = _valid(clean((context or {}).get("agent", "")))
    if name:
        return name, "token"

    user_agent = (headers.get("user-agent", "") or "").lower()
    for needle, product in USER_AGENT_HINTS:
        if needle in user_agent:
            return product, "user-agent"
    return None, None
