import json
from typing import Any, Dict, Optional

def build_jsonrpc_response(req_id: int, result: Any) -> Dict:
    """Wraps a result in a JSON-RPC 2.0 success response."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": result
    }

def build_jsonrpc_error(req_id: int, code: int, message: str) -> Dict:
    """Wraps an error in a JSON-RPC 2.0 error response."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": code,
            "message": message
        }
    }

def format_sse(payload: Dict) -> str:
    """Converts a dictionary to an SSE formatted string."""
    return f"data: {json.dumps(payload)}\n\n"

# Standard JSON-RPC Error Codes
class JSONRPCError:
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603