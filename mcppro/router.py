import inspect
from mcppro import transport, types
from mcppro.types import MCPToolResult, MCPContent, MCPCapabilities, MCPServerInfo
from typing import Any, Dict, AsyncGenerator

async def route_request(
    method: str, 
    req_id: int, 
    params: Dict, 
    server_name: str,
    server_version: str,
    tool_schemas: list,
    tool_functions: dict
) -> AsyncGenerator[str, None]:
    """
    Routes a JSON-RPC method to the appropriate handler.
    Yields SSE-formatted strings.
    """

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