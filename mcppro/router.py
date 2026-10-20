import inspect
from mcppro import transport, types
from mcppro.types import (
    MCPToolResult, MCPContent, MCPCapabilities, MCPServerInfo,
    MCPTextContent,
)
from mcppro.scopes import require_scopes

# JSON-RPC codes the MCP spec assigns to resource operations.
RESOURCE_NOT_FOUND = -32002
FORBIDDEN = -32003
from typing import Any, Dict, AsyncGenerator

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
            "protocolVersion": "2025-03-26",
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
    elif method == "notifications/initialized":
        # Must not reply. Just exit generator.
        return

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
            arguments = params.get("arguments", {})
            
            func = tool_functions.get(tool_name)
            
            if not func:
                result = MCPToolResult(
                    content=[MCPContent(type="text", text=f"Unknown tool: {tool_name}")],
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
                    
                    output = func(**arguments)
                    
                    result = MCPToolResult(
                        content=[MCPContent(type="text", text=str(output))],
                        isError=False
                    )
                except Exception as e:
                    result = MCPToolResult(
                        content=[MCPContent(type="text", text=f"Server Error: {str(e)}")],
                        isError=True
                    )

            payload = transport.build_jsonrpc_response(req_id, result.model_dump())
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
        result = {
            "resources": [r.model_dump(exclude_none=True)
                          for r in resource_registry.list_resources(user_context)]
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
            content = _call(reader, user_context, uri)
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