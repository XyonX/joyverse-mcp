from fastapi import FastAPI, Request,Depends
from fastapi.responses import StreamingResponse
from typing import Callable, Dict, Any
import uvicorn

from mcppro.auth import no_auth
from mcppro.decorators import create_tool_decorator
from mcppro.router import route_request

class MCPServer:
    """
    The main MCP Server class.
    Wraps FastAPI and provides a simple decorator-based API.
    """
    
    def __init__(self, name: str, version: str = "1.0.0", auth: Callable = None):
        self.name = name
        self.version = version
        
        # Internal registries
        self._tool_functions: Dict[str, Callable] = {}
        self._tool_schemas: list = []
        
        # Auth strategy (default: no auth)
        self._auth_dependency = auth or no_auth
        
        # Create the @server.tool decorator bound to this instance
        self.tool = create_tool_decorator(self)
        
        # Create the FastAPI app
        self._app = self._create_app()

    def _create_app(self) -> FastAPI:
        """Builds the FastAPI application with MCP endpoint."""
        app = FastAPI(title=f"MCP Server: {self.name}")
        
        @app.post("/mcp")
        async def handle_mcp(request: Request, user: dict = Depends(self._auth_dependency)):
            """The single MCP V2 endpoint."""
            
            # 1. Parse JSON-RPC request
            try:
                body = await request.json()
            except Exception:
                return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            
            method = body.get("method")
            req_id = body.get("id")
            params = body.get("params", {})
            
            # 2. Route the request through our logic
            async def generate_response():
                async for sse_chunk in route_request(
                    method=method,
                    req_id=req_id,
                    params=params,
                    server_name=self.name,
                    server_version=self.version,
                    tool_schemas=self._tool_schemas,
                    tool_functions=self._tool_functions
                ):
                    yield sse_chunk
            
            # 3. Return as SSE Stream
            return StreamingResponse(
                generate_response(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"}
            )
        
        return app

    def run(self, host: str = "127.0.0.1", port: int = 8000, reload: bool = False):
        """Starts the MCP server using Uvicorn."""
        print(f"🚀 Starting MCP Server: {self.name} v{self.version}")
        print(f"   Tools registered: {len(self._tool_schemas)}")
        for schema in self._tool_schemas:
            print(f"   ↳ {schema.name}")
        print(f"   Listening on: http://{host}:{port}/mcp")
        
        uvicorn.run(self._app, host=host, port=port, reload=reload)