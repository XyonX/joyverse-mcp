from fastapi import Request, HTTPException
import jwt
import os

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-in-production")

def jwt_auth(request: Request) -> dict:
    """
    Validates JWT token and extracts username.
    Returns user context: {"username": "joydip"}
    """
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "")
    
    if not token:
        raise HTTPException(status_code=40=401, detail="Missing authentication token")
    
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        username = payload.get("username")
        
        if not username:
            raise HTTPException(status_code=401, detail="Invalid token: missing username")
        
        # Security: sanitize username to prevent path traversal in R2 keys
        if "/" in username or ".." in username or username.strip() == "":
            raise HTTPException(status_code=401, detail="Invalid username format")
        
        return {"username": username}
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")