from mcppro import MCPServer, api_key_auth

from joyverse.profile import get_profile, update_profile
from joyverse.memory import get_memory, add_memory_trait, update_focus
from joyverse.data import get_data, update_data

from joyverse.auth import jwt_auth  # NEW

server = MCPServer(
    name="joyverse-mcp",
    version="1.0.0",
    auth=jwt_auth,              # NEW: JWT auth
    instructions=USER_DATA
)

# Profile
server.tool(description="Get the user's personal profile")(get_profile)
server.tool(description="Update a field in the user profile")(update_profile)

# Memory
server.tool(description="Get the user's LLM memory model")(get_memory)
server.tool(description="Add a personality trait to memory")(add_memory_trait)
server.tool(description="Update the current main focus")(update_focus)

# Data Logs
server.tool(description="Get structured data logs by topic (e.g., dsa, projects)")(get_data)
server.tool(description="Update structured data logs for a topic")(update_data)

if __name__ == "__main__":
    server.run(port=8001)