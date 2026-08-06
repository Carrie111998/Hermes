#!/usr/bin/env python3
"""
Fetch messages from Slack #research channel via unified gateway dispatch.
"""
import json
import sys

# The dispatch tool is available as a function call
# Let me try using the mcp_unified_gateway_dispatch directly via the agent's tool calling

result = {
    "server": "nexus",
    "tool": "slack_conversations_history",
    "arguments": {
        "channel": "C0AC9HSQNSY",
        "limit": 20
    }
}

print(json.dumps(result))