from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional


class MCPContent(BaseModel):
    """A single content block inside a tool result."""
    type: str = "text"
    text: Optional[str] = None
    data: Optional[str] = None      # Base64 for images/audio
    mimeType: Optional[str] = None


class MCPToolResult(BaseModel):
    """The result object for tools/call."""
    content: List[MCPContent] = Field(default_factory=list)
    isError: bool = False


class MCPToolDefinition(BaseModel):
    """A tool definition returned by tools/list."""
    name: str
    description: str = ""
    inputSchema: Dict[str, Any] = Field(default_factory=dict)


class MCPServerInfo(BaseModel):
    """Server identity returned during initialize."""
    name: str
    version: str = "1.0.0"


class MCPCapabilities(BaseModel):
    """Capabilities declared during initialize.

    `resources` and `prompts` are Optional-with-None so a server that does not
    implement them leaves the key out entirely. That matters: a client that
    sees `"resources": {}` will call resources/list, while a missing key means
    it will not.
    """
    tools: Dict[str, Any] = Field(default_factory=lambda: {})
    resources: Optional[Dict[str, Any]] = None


# ==========================================
# RESOURCES
# ==========================================

class MCPResourceAnnotations(BaseModel):
    """Optional hints for how a client should use a resource.

    `audience` and `priority` are what let a client include the important
    context and skip the rest, which is the difference between a useful server
    and one that dumps everything into the window.
    """
    audience: Optional[List[str]] = None   # "user" and/or "assistant"
    priority: Optional[float] = None       # 0.0 least .. 1.0 most
    lastModified: Optional[str] = None     # ISO 8601


class MCPResource(BaseModel):
    """A concrete, addressable resource returned by resources/list."""
    uri: str
    name: str
    title: Optional[str] = None
    description: Optional[str] = None
    mimeType: Optional[str] = None
    size: Optional[int] = None
    annotations: Optional[MCPResourceAnnotations] = None


class MCPResourceTemplate(BaseModel):
    """A parameterised URI pattern returned by resources/templates/list."""
    uriTemplate: str
    name: str
    title: Optional[str] = None
    description: Optional[str] = None
    mimeType: Optional[str] = None
    annotations: Optional[MCPResourceAnnotations] = None


class MCPTextContent(BaseModel):
    """One entry in a resources/read response."""
    uri: str
    mimeType: Optional[str] = None
    text: Optional[str] = None
    blob: Optional[str] = None            # base64, for binary
    annotations: Optional[MCPResourceAnnotations] = None


class MCPOAuthMetadata(BaseModel):
    """RFC 9728 OAuth 2.0 Protected Resource Metadata.

    `authorization_servers` is omitted when this server is also its own
    authorization server, which the RFC requires: the field is only present
    when the AS lives elsewhere.
    """
    resource: str
    authorization_servers: Optional[List[str]] = None
    scopes_supported: Optional[List[str]] = None
    bearer_methods_supported: Optional[List[str]] = None
    resource_documentation: Optional[str] = None
