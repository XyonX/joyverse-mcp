#!/usr/bin/env python3
"""
Single-file MCP test client for joyverse-mcp.

Boots the server in-process, mints a JWT for a test user, and drives an
OpenAI-compatible LLM in a tool-calling loop so the LLM itself decides which
MCP tools to invoke.

Usage:
    python mcp_client/mcp_test_client.py --dry-run   # no LLM, no writes
    python mcp_client/mcp_test_client.py --auto      # unattended live run
    python mcp_client/mcp_test_client.py             # interactive
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402
# Load the real .env BEFORE falling back. setdefault() only fills a missing key,
# so doing it first would pin the test secret and load_dotenv would then leave it
# in place -- every run would mint tokens the hosted server rejects with 401.
load_dotenv(REPO_ROOT / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env")
os.environ.setdefault("JWT_SECRET", "test-secret-not-a-real-one")

import jwt as pyjwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ============================================================
# CONFIG -- edit these
# ============================================================

TEST_USERNAME = "aarav-test"
TOKEN_TTL_HOURS = 24
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_TOOL_ROUNDS = 25

# ============================================================

GREEN, RED, YELLOW, CYAN, DIM, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m")


def log(msg, colour=DIM):
    print(f"{colour}{msg}{RESET}", flush=True)


def mint_token(username: str) -> str:
    """Same logic as scripts/generate_token.py."""
    secret = os.getenv("JWT_SECRET")
    if not secret:
        sys.exit(f"{RED}JWT_SECRET is not set.{RESET}")
    return pyjwt.encode(
        {"username": username, "exp": int(time.time()) + TOKEN_TTL_HOURS * 3600},
        secret, algorithm="HS256")


class MCPClient:
    """Thin JSON-RPC-over-SSE client for the joyverse MCP server."""

    def __init__(self, app, token: str):
        self.http = TestClient(app)
        self.token = token
        self.written_keys = []

    def _rpc(self, method, params=None, req_id=1):
        body = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            body["params"] = params
        resp = self.http.post(
            "/mcp", json=body,
            headers={"Authorization": f"Bearer {self.token}"})
        if resp.status_code != 200:
            raise RuntimeError(f"{method} failed: HTTP {resp.status_code} "
                               f"{resp.text[:200]}")
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        return None

    def initialize(self):
        return self._rpc("initialize")["result"]

    def list_tools(self):
        return self._rpc("tools/list")["result"]["tools"]

    def call_tool(self, name, arguments):
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        payload = result.get("result", {})
        text = "".join(b.get("text", "") for b in payload.get("content", []))
        return {"text": text, "isError": payload.get("isError", False)}


class RemoteMCPClient(MCPClient):
    """Same JSON-RPC-over-SSE client, pointed at a hosted server over HTTP.

    Used to exercise a deployed instance (e.g. https://host/mcp) instead of the
    in-process ASGI app. Only __init__ differs from MCPClient -- the SSE frame
    parsing, tool dispatch and conversation loop are inherited unchanged, so
    local and remote runs exercise identical client code.
    """

    def __init__(self, url: str, token: str, timeout: float = 60.0):
        import httpx

        # /mcp is the full endpoint path; we post to it verbatim and keep the
        # base bare so httpx does not try to resolve a relative "/mcp" itself.
        self.url = url
        self.token = token
        self.written_keys = []
        self.http = httpx.Client(timeout=timeout)

    def _rpc(self, method, params=None, req_id=1):
        body = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            body["params"] = params
        resp = self.http.post(
            self.url, json=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                # The server ignores this today, but the MCP spec requires the
                # client to ask for an event stream -- send it so the request
                # matches what a real spec-compliant client would send.
                "Accept": "text/event-stream",
            })
        if resp.status_code != 200:
            raise RuntimeError(f"{method} failed: HTTP {resp.status_code} "
                               f"{resp.text[:200]}")
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        return None


def to_openai_tools(tools):
    """Convert MCP tool definitions to OpenAI function-calling format."""
    return [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("inputSchema") or {
                "type": "object", "properties": {}},
        },
    } for t in tools]


def _complete(llm, messages, openai_tools, attempts=3):
    """Call the LLM, retrying transient empty/errored responses.

    Some gateways occasionally return a completion with no choices, or fail
    mid-run with a rate-limit or upstream error. Treat those as transient and
    back off rather than crashing the whole run.
    """
    last_error = None
    for attempt in range(attempts):
        try:
            resp = llm.chat.completions.create(
                model=MODEL, messages=messages, tools=openai_tools,
                tool_choice="auto")
            if resp.choices and resp.choices[0].message is not None:
                return resp.choices[0].message
            last_error = "empty response (no choices returned)"
        except Exception as e:  # noqa: BLE001 - upstream can fail many ways
            last_error = str(e)[:200]
        if attempt < attempts - 1:
            wait = 2 ** attempt
            log(f"     (LLM call failed: {last_error} -- retrying in {wait}s)",
                YELLOW)
            time.sleep(wait)
    raise RuntimeError(f"LLM call failed after {attempts} attempts: {last_error}")


def run_conversation(mcp: MCPClient, llm, tools, messages, dry_run=False):
    """The tool-calling loop. Returns the number of tool calls made."""
    openai_tools = to_openai_tools(tools)
    tool_calls_made = 0

    for turn in range(MAX_TOOL_ROUNDS):
        log(f"\n--- LLM turn {turn + 1} ---", CYAN)
        msg = _complete(llm, messages, openai_tools)
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            log("LLM finished (no more tool calls)", GREEN)
            if msg.content:
                log(f"\n{msg.content}", GREEN)
            return tool_calls_made

        for call in msg.tool_calls:
            fn = call.function
            log(f"  -> {fn.name}({fn.arguments})", YELLOW)
            try:
                args = json.loads(fn.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            if dry_run:
                log("     (dry-run: not executing)", DIM)
                result = {"text": "[dry-run] would call the tool",
                          "isError": False}
            else:
                result = mcp.call_tool(fn.name, args)
                flag = "OK " if not result["isError"] else "ERR"
                log(f"     {flag} {result['text'][:160]}", DIM)

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result),
            })
            tool_calls_made += 1

    log(f"\nReached MAX_TOOL_ROUNDS ({MAX_TOOL_ROUNDS})", RED)
    return tool_calls_made


def main():
    # Declared before any use of MAX_TOOL_ROUNDS below, including the --max-rounds
    # help string, which Python resolves as a read of the global.
    global MAX_TOOL_ROUNDS

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Execute the loop but never call real tools")
    ap.add_argument("--auto", action="store_true",
                    help="Run the full scripted conversation unattended")
    ap.add_argument("--message", help="Single message to send instead of --auto")
    ap.add_argument("--url", help="Target a hosted server (e.g. "
                                   "https://host/mcp) instead of the local app")
    ap.add_argument("--user", help=f"Username to authenticate as "
                                   f"(default: {TEST_USERNAME})")
    ap.add_argument("--token", help="Use this JWT verbatim instead of minting "
                                    "one from the local JWT_SECRET")
    ap.add_argument("--max-rounds", type=int,
                    help=f"Cap the tool-calling loop (default: {MAX_TOOL_ROUNDS})")
    args = ap.parse_args()

    if args.max_rounds is not None:
        MAX_TOOL_ROUNDS = args.max_rounds
    username = args.user or TEST_USERNAME

    log("=" * 62)
    log("joyverse-mcp :: LLM client test", CYAN)
    log("=" * 62)

    from joyverse.prompts import USER_DATA

    if args.url:
        # Remote mode: talk to a deployed server over real HTTP. Do NOT import
        # run.py here -- it builds a local boto3 client we have no use for.
        token = args.token or mint_token(username)
        client = RemoteMCPClient(args.url, token)
        log(f"target: {args.url} (remote)", CYAN)
    else:
        import run as app_module
        token = args.token or mint_token(username)
        client = MCPClient(app_module.server._app, token)
        log("target: in-process app (local)", CYAN)

    info = client.initialize()
    log(f"server: {info['serverInfo']['name']} "
        f"v{info['serverInfo']['version']}")
    log(f"instructions: {len(info.get('instructions', ''))} chars")

    tools = client.list_tools()
    log(f"tools ({len(tools)}): {', '.join(t['name'] for t in tools)}", GREEN)
    log(f"user: {username}")
    log(f"max tool rounds: {MAX_TOOL_ROUNDS}")

    if args.dry_run:
        log("\nDRY-RUN: tool calls will NOT be executed.", YELLOW)
        return 0

    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    if not api_key:
        log("\nNo OPENAI_API_KEY set. Add one to mcp_client/.env.", RED)
        log("Server, auth and tools all work -- see the output above.")
        return 1

    from openai import OpenAI
    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    llm = OpenAI(**kwargs)
    log(f"llm: {MODEL} via {base_url or 'api.openai.com'}")

    system = (f"{USER_DATA}\n\n---\n\nYou are helping {username}. "
              f"Use the available tools to read and write their data.")

    persona_path = Path(__file__).resolve().parent / "persona.md"
    persona = persona_path.read_text() if persona_path.exists() else ""
    messages = [{"role": "system", "content": system}]

    if args.message:
        messages.append({"role": "user", "content": args.message})
        run_conversation(client, llm, tools, messages, dry_run=args.dry_run)
    elif args.auto:  # noqa: E501
        log("\nauto mode: seeding persona from persona.md", CYAN)
        log("\nauto mode: seeding persona from persona.md", CYAN)
        messages.append({
            "role": "user",
            "content": (
                f"Here is background information about me, the user "
                f"{username}. Use your tools to store it properly: build "
                f"my profile with update_profile, write my bio with "
                f"update_bio, and create data logs for dsa, projects, skills "
                f"and reading with update_data.\n\n{persona}"
            ),
        })
        made = run_conversation(client, llm, tools, messages,
                                dry_run=args.dry_run)
        log(f"\nauto run made {made} tool calls", GREEN)
    else:
        log("\ninteractive: type a message, or 'quit'.", CYAN)
        while True:
            try:
                line = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if line.lower() in ("quit", "exit", "q"):
                break
            if not line:
                continue
            messages.append({"role": "user", "content": line})
            run_conversation(client, llm, tools, messages, dry_run=args.dry_run)

    log("\ndone.", GREEN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
