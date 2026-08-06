#!/usr/bin/env python3
"""
Fetch messages from Slack #research channel via unified gateway.
"""
import json
import os
import sys

# The unified gateway dispatches to MCP servers
# We need to call the Slack tool via the gateway

def main():
    channel_id = "C0AC9HSQNSY"  # #research channel
    limit = 20
    
    # Use the dispatch tool via the unified gateway
    # We'll call it via the MCP dispatch tool
    import subprocess
    import json
    
    # Use the dispatch tool via the Hermes MCP unified gateway
    # The dispatch tool is available via mcp_unified_gateway_dispatch
    result = subprocess.run([
        "python3", "-c", """
import json
import asyncio
import sys
sys.path.insert(0, '/Users/danizhaky/.hermes/hermes-agent')

# Import the mcp client
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client("http://127.0.0.1:8081/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            # Call slack:conversations_history
            result = await session.call_tool(
                "slack:conversations_history",
                {
                    "channel": "C0AC9HSQNSY",
                    "limit": 20
                }
            )
            print(json.dumps(result.model_dump(), default=str))

asyncio.run(main())
"""
    ], capture_output=True, text=True, cwd="/Users/danizhaky/.hermes/hermes-agent")
    
    print("STDOUT:", result.stdout)
    print("STDERR:", result.stderr)
    print("Return code:", result.returncode)

if __name__ == "__main__":
    main()