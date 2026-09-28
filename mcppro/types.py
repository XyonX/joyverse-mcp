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