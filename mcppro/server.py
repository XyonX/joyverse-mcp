from fastapi import FastAPI, Request, Depends
from fastapi.responses import StreamingResponse, JSONResponse, Response
from typing import Callable, Dict, Any, Iterable, Optional
import json
import uvicorn

from mcppro.auth import no_auth
from mcppro.decorators import create_tool_decorator
from mcppro.discovery import challenge_header
from mcppro.resources import ResourceRegistry
from mcppro.router import route_request

class MCPServer:
    """
    The main MCP Server class.
    Wraps FastAPI and provides a simple decorator-based API.
    """
    
    def __init__(self, name: str, version: str = "1.0.0", auth: Callable = None,
                 instructions: str = "", extra_routes: Iterable = None,
                 resource_url: Optional[str] = None,
                 max_body_bytes: Optional[int] = 32 * 1024 * 1024,
                 tool_observer: Optional[Callable] = None):
        self.name = name
        # Called after every tool call as
        # observer(tool_name, user_context, arguments, output_text, is_error).
        self.tool_observer = tool_observer
        # name -> {func, description, title, arguments}. See prompt().
        self._prompts: Dict[str, dict] = {}
        # Largest request body /mcp will read. Without a cap a single request
        # can make the server buffer an arbitrary amount of memory before any
        # tool gets a chance to refuse it. None disables the check.
        self._max_body_bytes = max_body_bytes
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

        # Create the @server.tool decorator bound to this instance
        self.tool = create_tool_decorator(self)

        # Resource decorators, mirroring server.tool.
        self.resource = self._make_resource_decorator()
        self.resource_template = self._make_resource_template_decorator()
        
        
        # Create the FastAPI app
        self._app = self._create_app()

    def prompt(self, name: str = None, description: str = "",
               title: str = None, arguments: list = None):
        """Register a prompt: a reusable request the user picks by name.

        The function returns the text of the message; its keyword arguments
        are the prompt's arguments, each described in `arguments` as
        {"name", "description", "required"}.
        """
        def decorator(func):
            self._prompts[name or func.__name__] = {
                "func": func, "description": description, "title": title,
                "arguments": list(arguments or [])}
            return func
        return decorator

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
            
            # 1. Read the body with a hard cap. The declared length is checked
            #    first so an honest oversized request is refused before a byte
            #    is read; the running count catches a chunked body that
            #    declares nothing.
            limit = self._max_body_bytes
            too_large = JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32600, "message": "Request body too large"}},
                status_code=413)
            declared = request.headers.get("content-length", "")
            if limit and declared.isdigit() and int(declared) > limit:
                return too_large
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if limit and size > limit:
                    return too_large
                chunks.append(chunk)

            # 2. Parse JSON-RPC request
            try:
                body = json.loads(b"".join(chunks))
            except Exception:
                return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}

            # A body is one message or a batch of them (an array). Anything
            # else -- a bare string, a number, an empty array -- is not a
            # JSON-RPC request, and used to surface as an HTTP 500.
            messages = body if isinstance(body, list) else [body]
            if not messages or not all(isinstance(m, dict) for m in messages):
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "Invalid Request"}},
                    status_code=400)

            # A message without an id is a notification (or a response) and
            # expects no reply. When nothing in the body does, the answer is
            # 202 Accepted with no body rather than an empty event stream.
            if not any("id" in m for m in messages):
                return Response(status_code=202)

            # 3. Route each request through our logic
            async def generate_response():
                for message in messages:
                    if "id" not in message:
                        continue
                    async for sse_chunk in route_request(
                        method=message.get("method"),
                        req_id=message.get("id"),
                        params=message.get("params") or {},
                        server_name=self.name,
                        server_version=self.version,
                        tool_schemas=self._tool_schemas,
                        tool_functions=self._tool_functions,
                        instructions=self.instructions,
                        user_context=user,
                        tool_scopes=self._tool_scopes,
                        resource_registry=self.resources,
                        tool_observer=self.tool_observer,
                        prompts=self._prompts,
                    ):
                        yield sse_chunk

            # 4. Return as SSE Stream
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