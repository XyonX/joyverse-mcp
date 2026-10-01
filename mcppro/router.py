import inspect
from mcppro import transport, types
from mcppro.types import MCPToolResult, MCPContent, MCPCapabilities, MCPServerInfo
from mcppro.scopes import require_scopes
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
    tool_scopes: Dict[str, list] = None
) -> AsyncGenerator[str, None]:
    """
    Routes a JSON-RPC method to the appropriate handler.
    Yields SSE-formatted strings.

    user_context is the dict returned by the auth strategy, injected into any
    tool function that declares a 'user' parameter.

    tool_scopes maps a tool name to the scopes required to call it. A tool
    absent from the mapping has no scope requirement, so servers that do not
    use OAuth behave exactly as before.
    """
    if user_context is None:
        user_context = {}
    if tool_scopes is None:
        tool_scopes = {}

    # ==========================================
    # A. LIFECYCLE: Initialize
    # ==========================================
    if method == "initialize":
        result = {
            "protocolVersion": "2025-03-26",
            "capabilities": MCPCapabilities().model_dump(),
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
    # E. FALLBACK: Method Not Found
    # ==========================================
    else:
        payload = transport.build_jsonrpc_error(
            req_id, 
            transport.JSONRPCError.METHOD_NOT_FOUND, 
            f"Method '{method}' not found"
        )
        yield transport.format_sse(payload)