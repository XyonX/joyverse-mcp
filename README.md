# joyverse-mcp

A remote [MCP](https://modelcontextprotocol.io) server that gives your AI
assistants one shared, private memory of you: who you are, what you have been
doing, the things you track, and the files they produce. ChatGPT, Claude,
Cursor and your own agents all read and write the same account.

The repository holds two packages:

| Package | What it is |
|---|---|
| `joyverse/` | The application: the tools, storage layout and auth. |
| `mcppro/` | A small MCP server framework (Streamable HTTP, JSON-RPC, SSE) the application is built on. |

Data lives in a Cloudflare R2 bucket. The server keeps no state of its own.

## What it stores

| Store | For | Main tools |
|---|---|---|
| Profile | Facts: name, location, timezone, occupation | `get_profile`, `update_profile` |
| Bio | Your story, in sections | `get_bio`, `update_bio` |
| Memory | Traits, patterns, preferences, current focus | `get_memory`, `add_memory_trait`, `update_context` |
| Data topics | Structured logs by subject (dsa, projects, reading) | `list_topics`, `get_data`, `edit_data` |
| User log | What **you** did, one entry per meaningful thing | `add_to_log`, `get_log` |
| Agent journals | Each agent's own working notes | `add_to_agent_log`, `get_agent_log` |
| Files | Anything, per agent folder, shared across agents | `save_file_*`, `request_upload`, `get_file` |

Across all of it: `search` finds things by content, `whoami` tells an agent
which account and name it has, `export_account` produces a zip of everything,
and `get_audit_log` shows which agent changed what. Profile, bio and memory
keep their last ten versions (`restore_version`); data topics keep one
(`restore_data`).

The full tool list, with the rules for using it, is sent to every client as
the server's instructions; see `joyverse/prompts.py`.

## Run it

Requires Python 3.10+.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # then fill in the R2 values and JWT_SECRET
.venv/bin/python run.py     # serves http://localhost:8001/mcp
```

Or with Docker:

```bash
docker build -t joyverse-mcp .
docker run --env-file .env -p 8001:8001 joyverse-mcp
```

`GET /health` answers without auth. Every setting is documented in
`.env.example`.

## Connect a client

There are two ways to authenticate. Both resolve to the same kind of account.

**Bearer token** -- for your own agents and scripts. Edit the handle at the
top of `scripts/generate_token.py`, run it, and send the token as
`Authorization: Bearer <token>`.

**OAuth** -- for apps that sign you in (Claude, ChatGPT). Set
`OAUTH_ENABLED=true` and the Auth0 values in `.env`; the server then
advertises itself through RFC 9728 discovery and the app walks you through
login. To make a bearer account and an OAuth login the same account, run
`scripts/link_account.py`.

### Tell the server which agent is calling

Each agent gets its own folder and journal, so the server needs a stable name
for it. Do not rely on the model naming itself. Set it on the connection, in
order of preference:

1. Header `X-Joyverse-Agent: <name>` in the agent's MCP server config.
2. `?agent=<name>` on the MCP URL.
3. `AGENT` in `scripts/generate_token.py`, for a token minted for one agent.

Chat apps that cannot be configured are recognised from their User-Agent.
`whoami` shows the name the server settled on.

## Operator scripts

All of these read `.env` and act on the real bucket.

| Script | Does |
|---|---|
| `scripts/generate_token.py` | Mint a bearer token for a handle. |
| `scripts/revoke_token.py` | Retire one token, or every token issued so far for a handle. |
| `scripts/link_account.py` | Attach an OAuth login to a bearer account. |
| `scripts/migrate_agents.py` | Merge duplicate agent names; move agents' entries out of the user log. Dry run unless `--apply`. |
| `scripts/delete_account.py` | Delete an account. Dry run unless `--apply --confirm <user id>`. |

## Storage layout

```text
users/_registry/identity.json        handles and OAuth identities -> user ids
users/_registry/revoked.json         revoked bearer tokens
users/<user_id>/profile.md
users/<user_id>/bio.md
users/<user_id>/memory.json
users/<user_id>/data/<topic>/...     one JSON log per topic
users/<user_id>/logs/<date>.jsonl    the user's log, one object per UTC day
users/<user_id>/storage/<agent>/...  that agent's files
users/<user_id>/storage/<agent>/journal/<date>.jsonl
users/<user_id>/history/<doc>/...    earlier versions of profile, bio, memory
users/<user_id>/audit/<date>.jsonl   server-written record of changes
```

A user id (`u_` + 12 hex characters) is minted by the server and is the only
thing that ever becomes a path. Every tool takes the account from the auth
token, never from an argument.

## Tests

```bash
.venv/bin/python -m pytest
```

Storage is faked in memory, so the suite needs no credentials and never
touches a real bucket. CI runs it on every push to `main`.

## Adding a tool

Write a plain function that takes `user: dict` (injected from auth) and
returns a string, then register it in `run.py`:

```python
server.tool(description="...", scopes=[READ_SCOPE])(my_tool)
```

The input schema is inferred from the signature. Add the tool's name to one
of the annotation sets in `run.py` (read-only, additive, replacing or
destructive) -- the server refuses to start with an unclassified tool -- and
to the pinned tool lists in `tests/test_integration.py` and
`mcp_client/test_client_core.py`.
