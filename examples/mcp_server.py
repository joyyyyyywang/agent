"""A tiny offline MCP stdio server for trying the connection end to end.

Only JSON-RPC belongs on stdout; put diagnostic logs on stderr.
"""

import json
import sys


def handle(request):
    method = request.get("method")
    if "id" not in request:
        return None
    result = {}
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "joyagent-demo", "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "返回输入文字，用于验证 MCP 连接。",
                             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                                             "required": ["text"], "additionalProperties": False}}]}
    elif method == "tools/call":
        params = request.get("params", {})
        arguments = params.get("arguments", {})
        if params.get("name") != "echo" or not isinstance(arguments.get("text"), str):
            result = {"isError": True, "content": [{"type": "text", "text": "echo requires text"}]}
        else:
            result = {"content": [{"type": "text", "text": arguments["text"]}]}
    elif method != "ping":
        return {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Unknown method"}}
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


if __name__ == "__main__":
    for line in sys.stdin:
        try:
            response = handle(json.loads(line))
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
        except (ValueError, TypeError, KeyError):
            print("invalid request", file=sys.stderr, flush=True)
