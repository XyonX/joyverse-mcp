import inspect
import types
import typing
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
    # Any means "whatever the caller sends". JSON Schema has no single type for
    # that, so an empty schema (meaning "no constraint") is the honest answer.
    # Publishing "string" here is what told agents to send a JSON string where
    # an object was meant, silently corrupting the data the tool stores.
    Any: "",
    # Parametrised aliases like Dict[str, Any] and List[str] are not `dict` or
    # `list` themselves, so they needed their origin type looked up.
    typing.Dict: "object",
    typing.List: "array",
}

def _unwrap_optional(python_type):
    """Reduce Optional[X] / X | None to X.

    `Optional[list]` is a Union, not a `list`, so a direct lookup in TYPE_MAP
    missed and fell through to the "string" default. That published list
    parameters as strings, and agents rejected the calls rather than guessing.

    A parameter that is Optional is not required, so `required` is already
    driven by the default value; unwrapping only affects the advertised type.
    """
    origin = typing.get_origin(python_type)
    if origin is not None and origin not in (typing.Union,
                                             getattr(types, "UnionType", None)):
        # A parametrised alias: Dict[str, Any] -> dict, List[str] -> list.
        return origin
    if origin is typing.Union or origin is getattr(types, "UnionType", ()):
        args = [a for a in typing.get_args(python_type)
                if a is not type(None)]
        if len(args) == 1:
            # Recurse: the inner type may itself be a parametrised alias,
            # e.g. Optional[Dict[str, Any]] -> Dict[str, Any] -> dict.
            return _unwrap_optional(args[0])
        # A real multi-type union (str | int) has no honest JSON Schema single
        # type; string is the safe thing to advertise.
        return str
    return python_type


def infer_input_schema(func, param_docs: Dict[str, str] = None) -> Dict[str, Any]:
    """
    Inspects a Python function and generates a JSON Schema
    matching MCP's inputSchema format.

    `param_docs` documents individual parameters inside the schema. The tool
    description explains the call; these explain each argument, which is
    what a model reads when choosing values. An optional parameter with no
    description has its meaning guessed from the surrounding conversation --
    a guess is exactly what per-parameter documentation prevents.
    """
    try:
        sig = inspect.signature(func)
        hints = get_type_hints(func)
    except Exception:
        return {"type": "object", "properties": {}, "required": []}

    properties = {}
    required = []
    param_docs = param_docs or {}

    for param_name, param in sig.parameters.items():
        # Skip 'self' or injected context params
        if param_name in ("self", "cls", "user"):
            continue

        # Get JSON type from Python type hint, seeing through Optional.
        python_type = _unwrap_optional(hints.get(param_name, str))
        json_type = TYPE_MAP.get(python_type, "string")

        # An empty JSON type means "accepts anything" -- publish an empty
        # schema rather than an empty string, which is not a valid type.
        prop = {"type": json_type} if json_type else {}

        # Per-parameter documentation, when the caller supplied any. Keys
        # that name parameters the function does not have are simply never
        # consulted, so a typo fails visibly as a missing description rather
        # than breaking registration.
        if param_name in param_docs:
            prop["description"] = param_docs[param_name]
        properties[param_name] = prop

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

    `scopes` declares the OAuth scopes a caller must hold to invoke the tool.
    Omitting it leaves the tool open to any authenticated caller, so existing
    registrations keep working unchanged.
    """
    def tool(description: str = "", scopes=None,
             param_docs: Dict[str, str] = None):
        def decorator(func):
            # 1. Infer schema from function signature, folding in any
            #    per-parameter documentation.
            input_schema = infer_input_schema(func, param_docs)
            
            # 2. Create MCP Tool Definition
            tool_def = MCPToolDefinition(
                name=func.__name__,
                description=description or func.__doc__ or "",
                inputSchema=input_schema
            )
            
            # 3. Register in server's internal registries
            server_instance._tool_functions[func.__name__] = func
            server_instance._tool_schemas.append(tool_def)

            # Scope requirement, stored separately from the advertised schema
            # so it never leaks into tools/list (RFC 9728 scopes are a
            # transport-level concept, not a tool parameter).
            #
            # getattr with a default keeps this working against any
            # server-like object that exposes the two original registries,
            # matching how the lookups above already duck-type.
            scopes_map = getattr(server_instance, "_tool_scopes", None)
            if scopes_map is not None:
                scopes_map[func.__name__] = list(scopes or [])
            
            # 4. Return original function (so it can still be called normally)
            return func
        return decorator
    return tool