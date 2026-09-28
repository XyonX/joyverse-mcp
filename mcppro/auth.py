from fastapi import Request, HTTPException
from typing import Callable, Dict

def no_auth(request: Request) -> Dict:
    """
    Strategy 1: No authentication.
    Always allows access. Returns empty user context.
    """
    return {}

def api_key_auth(valid_keys: list) -> Callable:
    """
    Strategy 2: API Key authentication.
    Checks X-API-Key header or Authorization: Bearer header.
    """
    def auth_dependency(request: Request) -> Dict:
        # Check X-API-Key header first
        api_key = request.headers.get("X-API-Key")
        
        # Fall back to Authorization: Bearer header
        if not api_key:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                api_key = auth_header[7:]
        
        # Validate
        if not api_key or api_key not in valid_keys:
            raise HTTPException(status_code=401, detail="Invalid API Key")
            
        # Return user context (usable for tool-level permissions later)
        return {"api_key": api_key, "role": "user"}
    
    return auth_dependency