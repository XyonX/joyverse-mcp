from fastapi import FastAPI, Request, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from typing import Callable, Dict, Any, Iterable, Optional
import uvicorn

from mcppro.auth import no_auth
from mcppro.decorators import create_tool_decorator
from mcppro.discovery import challenge_header
from mcppro.resources import ResourceRegistry
from mcppro.resources import ResourceRegistry
from mcppro.router import route_request

class MCPServer:
    """
    The main MCP Server class.
    Wraps FastAPI and provides a simple decorator-based API.
    """
    
    def __init__(self, name: str, version: str = "1.0.0", auth: Callable = None,
                 instructions: str = "", extra_routes: Iterable = None,
                 resource_url: Optional[str] = None):
        self.name = name
        self.version = version
        self.instructions = instructions or ""
        
        # Internal registries
        self._tool_functions: Dict[str, Callable] = {}
        self._tool_schemas: list = []
        # tool name -> scopes required to call it. Empty list = no requirement.
        self._tool_scopes: Dict[str, list] = {}
        
        # Auth strategy (default: no auth)
        self._auth_dependency = auth or no_auth
        
        # Extra routers to mount (e.g. RFC 9728 discovery). Kept generic:
        # the framework does not know what an application needs to expose.
        self._extra_routes = list(extra_routes or [])
        
        # When set, 401 responses gain a WWW-Authenticate header pointing at
        # the resource metadata URL. This is what lets a client discover how
        # to authenticate instead of seeing a dead end. Left None, 401s are
        # plain and nothing about OAuth leaks into the framework's behaviour.
        self._resource_url = resource_url
        
        # Resources live in their own registry so the tool path stays untouched.
        self.resources = ResourceRegistry()

        # Resources live in their own registry so the tool path stays untouched.
        self.resources = ResourceRegistry()

        # Create the @server.tool decorator bound to this instance
        self.tool = create_tool_decorator(self)

        # Resource decorators, mirroring server.tool.
        self.resource = self._make_resource_decorator()
        self.resource_template = self._make_resource_template_decorator()

        # Resource decorators, mirroring server.tool.
        self.resource = self._make_resource_decorator()
        self.resource_template = self._make_resource_template_decorator()
        
        # Create the FastAPI app
        self._app = self._create_app()

    def _make_resource_decorator(self):
        """Build the @server.resource decorator."""
        def resource(uri: str, *, name: str = "", description: str = "",
                     mime_type: str = "text/plain", title=None,
                     size=None, priority=None, audience=None,
                     last_modified=None, scopes=None):
            def decorator(func):
                self.resources.add_resource(
                    uri, func, name=name, description=description,
                    mime_type=mime_type, title=title, size=size,
                    priority=priority, audience=audience,
                    last_modified=last_modified, scopes=scopes)
                return func
            return decorator
        return resource

    def _make_resource_template_decorator(self):
        """Build the @server.resource_template decorator."""
        def resource_template(uri_template: str, *, name: str = "",
                              description: str = "",
                              mime_type: str = "text/plain", title=None,
                              priority=None, audience=None,
                              last_modified=None, scopes=None):
            def decorator(func):
                self.resources.add_template(
                    uri_template, func, name=name, description=description,
                    mime_type=mime_type, title=title, priority=priority,
                    audience=audience, last_modified=last_modified,
                    scopes=scopes)
                return func
            return decorator
        return resource_template

    def _make_resource_decorator(self):
        """Build the @server.resource decorator."""
        def resource(uri: str, *, name: str = "", description: str = "",
                     mime_type: str = "text/plain", title=None,
                     size=None, priority=None, audience=None,
                     last_modified=None, scopes=None):
            def decorator(func):
                self.resources.add_resource(
                    uri, func, name=name, description=description,
                    mime_type=mime_type, title=title, size=size,
                    priority=priority, audience=audience,
                    last_modified=last_modified, scopes=scopes)
                return func
            return decorator
        return resource

    def _make_resource_template_decorator(self):
        """Build the @server.resource_template decorator."""
        def resource_template(uri_template: str, *, name: str = "",
                              description: str = "",
                              mime_type: str = "text/plain", title=None,
                              priority=None, audience=None,
                              last_modified=None, scopes=None):
            def decorator(func):
                self.resources.add_template(
                    uri_template, func, name=name, description=description,
                    mime_type=mime_type, title=title, priority=priority,
                    audience=audience, last_modified=last_modified,
                    scopes=scopes)
                return func
            return decorator
        return resource_template

    def _create_app(self) -> FastAPI:
        """Builds the FastAPI application with MCP endpoint."""
        app = FastAPI(title=f"MCP Server: {self.name}")

        # Mount application-supplied routers (e.g. RFC 9728 discovery). Done
        # before the MCP routes so an app can never shadow /mcp or /health.
        for router in self._extra_routes:
            app.include_router(router)

        # RFC 9728 s5.1: a 401 must tell the client where to find the resource
        # metadata, or it cannot discover how to authenticate. Registered only
        # when a resource_url is configured, so plain bearer servers keep their
        # exact previous 401 shape.
        if self._resource_url:
            challenge = challenge_header(self._resource_url)

            @app.exception_handler(401)
            async def _unauthorized(request: Request, exc):
                return JSONResponse(
                    {"detail": getattr(exc, "detail", "Unauthorized")},
                    status_code=401,
                    headers={"WWW-Authenticate": challenge},
                )
        
        @app.get("/health")
        async def health_check():
            """Health check endpoint for load balancers / Docker."""
            return {"status": "ok", "server": self.name, "version": self.version}
        
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
                    tool_functions=self._tool_functions,
                    instructions=self.instructions,
                    user_context=user,
                    tool_scopes=self._tool_scopes,
                    resource_registry=self.resources
                ):
                    yield sse_chunk
            
            # 3. Return as SSE Stream
            return StreamingResponse(
                generate_response(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"}
            )
        
        return app

    def run(self, host: str = "0.0.0.0", port: int = 8000, reload: bool = False):
        """Starts the MCP server using Uvicorn."""
        print(f"🚀 Starting MCP Server: {self.name} v{self.version}")
        print(f"   Tools registered: {len(self._tool_schemas)}")
        for schema in self._tool_schemas:
            print(f"   ↳ {schema.name}")
        print(f"   Listening on: http://{host}:{port}/mcp")
        
        uvicorn.run(self._app, host=host, port=port, reload=reload)