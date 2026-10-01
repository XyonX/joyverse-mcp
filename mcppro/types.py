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
    """Capabilities declared during initialize."""
    tools: Dict[str, Any] = Field(default_factory=lambda: {})


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