"""Stdio MCP adapter. The capability is scoped to one task, never a bot token."""

import json
import os
import socket
import sys

TOOLS = [
    {
        "name": "channel_api_help",
        "description": "List this conversation's supported native channel methods.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "channel_api",
        "description": "Send, edit or control a message in the task's authenticated conversation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {"type": "string"},
                "params": {"type": "object"},
                "action_id": {"type": "string"},
            },
            "required": ["method", "params", "action_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_approval",
        "description": "Ask the owner to approve a native Claude Code tool operation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "input": {"type": "object"},
            },
            "required": ["tool_name", "input"],
        },
    },
]


def call(name, arguments):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(os.environ["TINYHAT_CHANNEL_SOCKET"])
        stream = client.makefile("rwb")
        stream.write(
            (
                json.dumps(
                    {
                        "capability": os.environ["TINYHAT_CHANNEL_CAPABILITY"],
                        "name": name,
                        "arguments": arguments,
                    }
                )
                + "\n"
            ).encode()
        )
        stream.flush()
        result = json.loads(stream.readline(2**20))
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["result"]


def main():
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        try:
            method = message.get("method")
            if method == "initialize":
                result = {
                    "protocolVersion": message["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "tinyhat_channel", "version": "1.0"},
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = message["params"]
                value = call(params["name"], params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value)}]}
            elif method == "ping":
                result = {}
            else:
                raise ValueError("Unsupported MCP method")
            response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except Exception:
            response = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32603, "message": "Channel operation unavailable"},
            }
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
