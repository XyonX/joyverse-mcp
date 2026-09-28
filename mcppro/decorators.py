import inspect
from typing import get_type_hints, Dict, Any, List
from mcppro.types import MCPToolDefinition

# Python Type -> JSON Schema Type Mapping
TYPE_MAP = {
    int: "integer",
    float: "number",
    str: "string",
    bool: "boolean",
    list: "array",
    dict: "object",
}

def infer_input_schema(func) -> Dict[str, Any]:
    """
    Inspects a Python function and generates a JSON Schema
    matching MCP's inputSchema format.
    """
    try:
        sig = inspect.signature(func)
        hints = get_type_hints(func)
    except Exception:
        return {"type": "object", "properties": {}, "required": []}

    properties = {}
    required = []

    for param_name, param in sig.parameters.items():
        # Skip 'self' or injected context params
        if param_name in ("self", "cls", "user"):
            continue

        # Get JSON type from Python type hint
        python_type = hints.get(param_name, str)
        json_type = TYPE_MAP.get(python_type, "string")

        properties[param_name] = {"type": json_type}

        # If parameter has no default value, it's required
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required
    }

def create_tool_decorator(server_instance):
    """
    Factory that creates the @server.tool decorator
    bound to a specific MCPServer instance.
    """
    def tool(description: str = ""):
        def decorator(func):
            # 1. Infer schema from function signature
            input_schema = infer_input_schema(func)
            
            # 2. Create MCP Tool Definition
            tool_def = MCPToolDefinition(
                name=func.__name__,
                description=description or func.__doc__ or "",
                inputSchema=input_schema
            )
            
            # 3. Register in server's internal registries
            server_instance._tool_functions[func.__name__] = func
            server_instance._tool_schemas.append(tool_def)
            
            # 4. Return original function (so it can still be called normally)
            return func
        return decorator
    return tool