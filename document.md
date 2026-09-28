# Project Documentation: `joyverse-mcp`

> Place this file as `README.md` or `ARCHITECTURE.md` in the project root.

---

# Joyverse MCP — Architecture & Specification

## Overview

**Joyverse MCP** is a remote Model Context Protocol (MCP) server that stores and exposes personal user data (profile, memory, activity logs) to any AI agent. It allows any LLM-powered application to securely access personalized context about the user ("Joy") to provide tailored, context-aware responses.

### Project Components

| Component | Role |
| :--- | :--- |
| **`joyverse-mcp`** | The application. Exposes personal data tools via MCP. |
| **`mcppro`** | The framework. A minimal, decorator-based MCP V2 server library built from scratch. |
| **`flexygent`** | The consumer. A custom agentic AI framework that connects to `joyverse-mcp` as a client. |

### Relationship

```text
┌──────────────┐         MCP V2 (HTTP+SSE)         ┌──────────────────┐
│              │  ───────────────────────────────►   │                  │
│  Flexygent   │      JSON-RPC over HTTP POST        │  joyverse-mcp    │
│  (MCP Client)│                                      │  (MCP Server)    │
│              │  ◄────────────────────────────────   │                  │
│              │         SSE Stream Response           │  Uses mcppro     │
└──────────────┘                                       └──────────────────┘
                                                          │
                                                          ▼
                                                   ┌──────────────────┐
                                                   │  ~/.flexygent/   │
                                                   │  user/           │
                                                   │  ├── profile.md  │
                                                   │  ├── memory.json │
                                                   │  └── data/       │
                                                   └──────────────────┘
```

---

## MCP V2 Protocol Specification (Streamable HTTP)

This server implements the **Streamable HTTP** transport (colloquially "MCP V2"), as defined by the 2025-03-26 specification.

### Transport Rules

1. **Single Endpoint:** All communication goes to `POST /mcp`.
2. **Client Headers:** Must include `Accept: text/event-stream`.
3. **Server Headers:** Must respond with `Content-Type: text/event-stream`.
4. **SSE Wrapping:** All server responses (even instant ones) are wrapped as `data: {JSON}\n\n`.
5. **Stateless:** No sessions, no background connections. Request comes in, response streams out, connection closes.

### JSON-RPC 2.0 Format

All messages follow JSON-RPC 2.0:

```json
// Request
{ "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {...} }

// Response (inside SSE stream)
data: {"jsonrpc":"2.0","id":1,"result":{...}}\n\n
```

### Lifecycle Flow

1. Client sends `initialize` → Server responds with capabilities.
2. Client sends `notifications/initialized` → Server yields nothing.
3. Client sends `tools/list` → Server returns tool schemas.
4. Client sends `tools/call` → Server executes tool, streams result.

---

## Framework: `mcppro/`

A minimal, Python-native MCP V2 server framework. It provides decorator-based tool registration, automatic JSON Schema inference from type hints, and pluggable auth.

### File Structure & Responsibilities

| File | Purpose |
| :--- | :--- |
| `__init__.py` | Public API. Exposes `MCPServer`, `api_key_auth`. |
| `server.py` | Main class. Wraps FastAPI, sets up POST /mcp, starts Uvicorn. |
| `decorators.py` | `@server.tool` decorator. Infers `inputSchema` from Python type hints. |
| `router.py` | Routes JSON-RPC methods (`initialize`, `tools/list`, `tools/call`). |
| `transport.py` | Builds JSON-RPC payloads and formats SSE streams. |
| `types.py` | Pydantic models for MCP schemas (ToolDefinition, ToolResult, etc.). |
| `auth.py` | Pluggable auth strategies (`no_auth`, `api_key_auth`). |

### How Schema Inference Works

```python
@server.tool(description="Add numbers")
def add(a: int, b: int) -> str:
    return str(a + b)
```

The framework automatically generates:
```json
{
  "name": "add",
  "description": "Add numbers",
  "inputSchema": {
    "type": "object",
    "properties": {
      "a": {"type": "integer"},
      "b": {"type": "integer"}
    },
    "required": ["a", "b"]
  }
}
```

Type mapping: `int`→`integer`, `float`→`number`, `str`→`string`, `bool`→`boolean`.

### Authentication Design

Auth happens at the transport layer before JSON-RPC routing.

```python
# No auth (public server)
server = MCPServer(name="calc")

# API Key auth (personal data server)
server = MCPServer(name="joyverse", auth=api_key_auth(valid_keys=["sk-123"]))
```

Invalid auth returns `401 Unauthorized` before any MCP logic runs.

---

## Application: `joyverse/`

Business logic for reading/writing personal user data.

### File Structure

| File | Purpose |
| :--- | :--- |
| `config.py` | Defines base paths (`~/.flexygent/user/`). |
| `profile.py` | Read/write `profile.md`. |
| `memory.py` | Read/write `memory.json` (strict schema). |
| `data.py` | Read/write `data/**/*.json` (topic-based logs). |

### Data Storage Location

All data is stored in: `~/.flexygent/user/`

```text
~/.flexygent/user/
├── profile.md          ← Identity snapshot (key:value markdown)
├── bio.md              ← Detailed life narrative (markdown)
├── memory.json         ← LLM-inferred personality model (strict JSON)
└── data/
    ├── dsa/
    │   └── progress.json
    ├── projects/
    │   └── active.json
    └── skills/
        └── stack.json
```

### Tool Reference

| Tool Name | Description | Parameters |
| :--- | :--- | :--- |
| `get_profile` | Returns the user profile markdown. | None |
| `update_profile` | Updates a specific key in profile. | `field` (str), `value` (str) |
| `get_memory` | Returns the full memory JSON. | None |
| `add_memory_trait` | Appends a personality trait. | `trait` (str) |
| `update_focus` | Updates current main focus. | `focus` (str) |
| `get_data` | Fetches a data log by topic. | `topic` (str) |
| `update_data` | Updates a data log. | `topic` (str), `data` (str) |

---

## Data Schemas

### `memory.json` Schema

```json
{
  "personality": ["string (max 20 items)"],
  "observed_patterns": ["string (max 20 items)"],
  "preferences": {
    "explanation_style": "string",
    "code_style": "string",
    "feedback_style": "string"
  },
  "current_context": {
    "main_focus": "string",
    "immediate_next": "string",
    "mood": "string"
  },
  "last_updated": "YYYY-MM-DD"
}
```

### `profile.md` Format

```markdown
# User Profile

## Identity
name:       [Full Name]
age:        [number]
location:   [City, Country]
occupation: [Current role]

## Background
[3-5 sentence narrative paragraph]

## [Domain-specific sections]
key: value
```

---

## Integration with Flexygent

### 1. Configure the MCP Server

In `flexygent/mcp/config.py`:

```python
from flexygent.mcp.config import MCPServerConfig

joyverse_config = MCPServerConfig(
    name="joyverse-mcp",
    transport="sse",
    url="http://localhost:8001/mcp",
    headers={"X-API-Key": "sk-joy-local-dev"}
)
```

### 2. Register in Agent Setup

In your agent application:

```python
from flexygent.mcp.manager import register_mcp_server

register_mcp_server(tool_registry, joyverse_config)
```

### 3. Agent Loop

No changes needed. When the LLM calls `get_memory`, the `ToolRegistry` automatically routes it through the MCP client → HTTP POST → `joyverse-mcp` → file read → SSE stream back.

---

## Running & Testing

### Start the Server

```bash
cd joyverse-mcp
python run.py
```

### Test with Curl

```bash
# Initialize
curl -X POST http://localhost:8001/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "X-API-Key: sk-joy-local-dev" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'

# List Tools
curl -X POST http://localhost:8001/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "X-API-Key: sk-joy-local-dev" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

# Call Tool
curl -X POST http://localhost:8001/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "X-API-Key: sk-joy-local-dev" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_memory"}}'
```

---

## Adding New Tools

1. Write the function in `joyverse/` (e.g., `joyverse/bio.py`).
2. Import it in `run.py`.
3. Register it: `server.tool(description="Get user bio")(get_bio)`
4. The framework handles schema generation, routing, and MCP formatting automatically.

---

## Future Improvements

- [ ] Database backend (replace file I/O with SQLite/Postgres)
- [ ] Elicitation support (human confirmation for destructive operations)
- [ ] Resource exposure (MCP Resources for passive data injection)
- [ ] Multi-user support (API key → user_id mapping)
- [ ] Docker deployment