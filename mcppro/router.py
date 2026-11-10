import inspect
from mcppro import transport, types
from mcppro.types import (
    MCPToolResult, MCPContent, MCPCapabilities, MCPServerInfo,
    MCPTextContent,
)
from mcppro.scopes import require_scopes

# Protocol revisions this server can speak, newest first. The transport is
# stateless request/response in all of them, so the differences that matter
# here are only what the client is told it negotiated.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
DEFAULT_PROTOCOL_VERSION = "2025-03-26"


def negotiate_version(requested) -> str:
    """Echo the client's version when we support it, else offer our own.

    The spec leaves it to the client to disconnect if it cannot use what it
    is offered. Answering with a fixed version regardless told a newer client
    it had negotiated something it never asked for.
    """
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return DEFAULT_PROTOCOL_VERSION


def check_arguments(schema: dict, arguments: dict):
    """Validate tool arguments against the advertised schema.

    Returns an error string, or None when the call can go ahead. Only what a
    caller can act on is enforced -- a missing required argument, or a name
    the tool does not have -- because those previously surfaced as a Python
    TypeError the model could not interpret. A number sent where a string is
    expected is converted rather than refused: it is unambiguous, and tools
    are written against strings.
    """
    properties = (schema or {}).get("properties", {})
    required = (schema or {}).get("required", [])

    missing = [name for name in required if name not in arguments]
    if missing:
        return ("Missing required argument(s): " + ", ".join(missing)
                + ". Expected: " + (", ".join(properties) or "none") + ".")

    unknown = [name for name in arguments if name not in properties]
    if unknown:
        return ("Unknown argument(s): " + ", ".join(unknown)
                + ". This tool accepts: "
                + (", ".join(properties) or "no arguments") + ".")

    for name, value in arguments.items():
        if properties[name].get("type") == "string" \
                and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            arguments[name] = str(value)
    return None


# JSON-RPC codes the MCP spec assigns to resource operations.
RESOURCE_NOT_FOUND = -32002
FORBIDDEN = -32003
from typing import Any, Dict, AsyncGenerator

from starlette.concurrency import run_in_threadpool


async def _run(fn, *args, **kwargs):
    """Call a tool or resource function without blocking the event loop.

    Tools are ordinary blocking functions -- they talk to object storage and
    fetch URLs. Called directly from this coroutine, one slow tool froze every
    other request, the health check included, until it returned. A worker
    thread keeps the loop free; a tool written as a coroutine is awaited as is.
    """
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await run_in_threadpool(fn, *args, **kwargs)

async def route_request(
    method: str, 
    req_id: int, 
    params: Dict, 
    server_name: str,
    server_version: str,
    tool_schemas: list,
    tool_functions: dict,
    instructions: str = "",
    user_context: dict = None,
    tool_scopes: Dict[str, list] = None,
    resource_registry=None,
) -> AsyncGenerator[str, None]:
    """
    Routes a JSON-RPC method to the appropriate handler.
    Yields SSE-formatted strings.

    user_context is the dict returned by the auth strategy, injected into any
    tool function that declares a 'user' parameter.

    tool_scopes maps a tool name to the scopes required to call it. A tool
    absent from the mapping has no scope requirement, so servers that do not
    use OAuth behave exactly as before.

    resource_registry is a mcppro.resources.ResourceRegistry, or None. When
    present the server advertises the resources capability and answers
    resources/list, resources/read and resources/templates/list.
    """
    if user_context is None:
        user_context = {}
    if tool_scopes is None:
        tool_scopes = {}

    # ==========================================
    # A. LIFECYCLE: Initialize
    # ==========================================
    if method == "initialize":
        capabilities = {"tools": {}}
        # A client that sees `"resources": null` will still call
        # resources/list; a missing key means it will not. So advertise
        # resources only when the server actually implements them.
        if resource_registry is not None and resource_registry.has_any():
            capabilities["resources"] = {}
        result = {
            "protocolVersion": negotiate_version(
                (params or {}).get("protocolVersion")),
            "capabilities": capabilities,
            "serverInfo": MCPServerInfo(
                name=server_name, 
                version=server_version
            ).model_dump()
        }
        if instructions:
            result["instructions"] = instructions
        payload = transport.build_jsonrpc_response(req_id, result)
        yield transport.format_sse(payload)

    # ==========================================
    # B. LIFECYCLE: Initialized Notification
    # ==========================================
    elif isinstance(method, str) and method.startswith("notifications/"):
        # A notification never gets a reply -- not the initialized one, and
        # not cancelled/progress/roots either. Answering those with "method
        # not found" sent the client an error for a message that has no id.
        return

    elif method == "ping":
        # Liveness check; the spec's answer is an empty result.
        yield transport.format_sse(transport.build_jsonrpc_response(req_id, {}))

    # ==========================================
    # C. DISCOVERY: List Tools
    # ==========================================
    elif method == "tools/list":
        result = {
            "tools": [t.model_dump() for t in tool_schemas]
        }
        payload = transport.build_jsonrpc_response(req_id, result)
        yield transport.format_sse(payload)

    # ==========================================
    # D. EXECUTION: Call Tool
    # ==========================================
    elif method == "tools/call":
            tool_name = params.get("name")
            # Copied, and tolerant of an explicit null: the dict is mutated
            # below when the auth context is injected.
            raw_arguments = params.get("arguments") or {}
            arguments = dict(raw_arguments) \
                if isinstance(raw_arguments, dict) else None
            
            func = tool_functions.get(tool_name)
            schema = next((t.inputSchema for t in tool_schemas
                           if t.name == tool_name), None)
            problem = None
            # `user` is the injected auth context, never a caller's to set.
            # A client that sends one is ignored rather than refused: the
            # value is overwritten below either way.
            if func and arguments and \
                    "user" in inspect.signature(func).parameters:
                arguments.pop("user", None)
            if func and arguments is None:
                problem = "arguments must be an object of named values."
            elif func and schema is not None:
                problem = check_arguments(schema, arguments)
            
            if not func:
                result = MCPToolResult(
                    content=[MCPContent(type="text", text=f"Unknown tool: {tool_name}")],
                    isError=True
                )
            elif problem:
                result = MCPToolResult(
                    content=[MCPContent(type="text", text=problem)],
                    isError=True
                )
            else:
                try:
                    # ==========================================
                    # SCOPE ENFORCEMENT
                    # Checked before the function is looked up and run, so
                    # an under-scoped caller never reaches the tool body.
                    # Fails closed: a 403 here becomes isError rather than a
                    # successful-looking result.
                    # ==========================================
                    require_scopes(user_context, tool_scopes.get(tool_name, []))
                    
                    # ==========================================
                    # CONTEXT INJECTION
                    # If the tool function has a 'user' parameter, inject it!
                    # ==========================================
                    if "user" in inspect.signature(func).parameters:
                        arguments["user"] = user_context  # Injected by auth!
                    
                    output = await _run(func, **arguments)
                    
                    result = MCPToolResult(
                        content=[MCPContent(type="text", text=str(output))],
                        isError=False
                    )
                except Exception as e:
                    # A refusal raised on purpose (a missing scope) carries
                    # its own wording; only an unexpected failure is labelled
                    # a server error.
                    detail = getattr(e, "detail", None)
                    text = str(detail) if detail else f"Server Error: {str(e)}"
                    result = MCPToolResult(
                        content=[MCPContent(type="text", text=text)],
                        isError=True
                    )

            # exclude_none: a text block must not carry `"data": null` and
            # `"mimeType": null`, which strict clients reject.
            payload = transport.build_jsonrpc_response(
                req_id, result.model_dump(exclude_none=True))
            yield transport.format_sse(payload)

    # ==========================================
    # E. RESOURCES
    # ==========================================

    elif method == "resources/list":
        if resource_registry is None:
            payload = transport.build_jsonrpc_error(
                req_id, transport.JSONRPCError.METHOD_NOT_FOUND,
                "This server does not expose resources")
            yield transport.format_sse(payload)
            return
        # The lister reads the caller's live data, so it blocks like a tool.
        listed = await _run(resource_registry.list_resources, user_context)
        result = {
            "resources": [r.model_dump(exclude_none=True) for r in listed]
        }
        payload = transport.build_jsonrpc_response(req_id, result)
        yield transport.format_sse(payload)

    elif method == "resources/templates/list":
        if resource_registry is None:
            payload = transport.build_jsonrpc_error(
                req_id, transport.JSONRPCError.METHOD_NOT_FOUND,
                "This server does not expose resources")
            yield transport.format_sse(payload)
            return
        result = {
            "resourceTemplates": [t.model_dump(exclude_none=True)
                                  for t in resource_registry.list_templates()]
        }
        payload = transport.build_jsonrpc_response(req_id, result)
        yield transport.format_sse(payload)

    elif method == "resources/read":
        if resource_registry is None:
            payload = transport.build_jsonrpc_error(
                req_id, transport.JSONRPCError.METHOD_NOT_FOUND,
                "This server does not expose resources")
            yield transport.format_sse(payload)
            return

        uri = params.get("uri", "")
        try:
            reader, mime = resource_registry.resolve(uri, user_context)
        except KeyError:
            # -32002 is the code the spec assigns to a missing resource.
            payload = transport.build_jsonrpc_error(
                req_id, RESOURCE_NOT_FOUND, "Resource not found",
                {"uri": uri})
            yield transport.format_sse(payload)
            return

        try:
            require_scopes(user_context, resource_registry.required_scopes(uri))
        except Exception as e:
            # Reported as a JSON-RPC error, not an HTTP 403: by this point the
            # request is inside the MCP stream and the envelope is the only
            # channel left.
            detail = getattr(e, "detail", str(e))
            payload = transport.build_jsonrpc_error(
                req_id, FORBIDDEN, detail)
            yield transport.format_sse(payload)
            return

        try:
            from mcppro.resources import _call
            content = await _run(_call, reader, user_context, uri)
        except Exception as e:
            payload = transport.build_jsonrpc_error(
                req_id, transport.JSONRPCError.INTERNAL_ERROR,
                f"Could not read resource: {e}", {"uri": uri})
            yield transport.format_sse(payload)
            return

        result = {
            "contents": [MCPTextContent(
                uri=uri, mimeType=mime, text=str(content)
            ).model_dump(exclude_none=True)]
        }
        payload = transport.build_jsonrpc_response(req_id, result)
        yield transport.format_sse(payload)

    # ==========================================
    # E. FALLBACK: Method Not Found
    # ==========================================
    else:
        payload = transport.build_jsonrpc_error(
            req_id, 
            transport.JSONRPCError.METHOD_NOT_FOUND, 
            f"Method '{method}' not found"
        )
        yield transport.format_sse(payload)