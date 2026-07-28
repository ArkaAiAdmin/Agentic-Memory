#!/usr/bin/env python3
"""
LiteLLM Bridge Server

A JSON-RPC server that wraps LiteLLM for unified multi-provider LLM access.
The TypeScript IDE communicates with this via stdio (JSON-RPC over newlines).

Architecture:
  TypeScript (IDE) <--JSON-RPC/stdio--> This script <--HTTP--> LiteLLM <--HTTP--> LLM APIs
"""

import json
import sys
import traceback
from typing import Any

# LiteLLM import — graceful degradation if not installed
try:
    import litellm
    HAS_LITELLM = True
except ImportError:
    HAS_LITELLM = False


def log(msg: str) -> None:
    """Log to stderr (stdout is reserved for JSON-RPC)."""
    print(f"[LiteLLM Bridge] {msg}", file=sys.stderr, flush=True)


def send_response(id: int, result: Any) -> None:
    """Send a JSON-RPC response."""
    response = {"jsonrpc": "2.0", "id": id, "result": result}
    print(json.dumps(response), flush=True)


def send_error(id: int, code: int, message: str) -> None:
    """Send a JSON-RPC error response."""
    response = {
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": code, "message": message},
    }
    print(json.dumps(response), flush=True)


def handle_initialize(id: int, params: dict) -> None:
    """Handle MCP initialize handshake."""
    send_response(id, {
        "protocolVersion": "2024-11-05",
        "capabilities": {
            "tools": {"listChanged": False},
        },
        "serverInfo": {
            "name": "litellm-bridge",
            "version": "0.1.0",
        },
    })


def handle_chat(id: int, params: dict) -> None:
    """Handle a chat request using LiteLLM."""
    if not HAS_LITELLM:
        send_error(id, -32000, "LiteLLM is not installed. Run: pip install litellm")
        return

    model = params.get("model", "gpt-4o")
    messages = params.get("messages", [])
    tools = params.get("tools", [])
    system_prompt = params.get("system_prompt", "")
    temperature = params.get("temperature", 0.7)
    max_tokens = params.get("max_tokens", 4096)
    stream = params.get("stream", False)

    # Prepend system prompt
    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}] + messages

    # Normalize tool definitions for LiteLLM
    litellm_tools = None
    if tools:
        litellm_tools = []
        for tool in tools:
            litellm_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool.get("input_schema", {}),
                },
            })

    try:
        if stream:
            # Streaming response
            response = litellm.completion(
                model=model,
                messages=messages,
                tools=litellm_tools,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )

            chunks = []
            for chunk in response:
                delta = chunk.choices[0].delta
                finish_reason = chunk.choices[0].finish_reason

                if hasattr(delta, "content") and delta.content:
                    chunks.append({"type": "text", "text": delta.content})

                if hasattr(delta, "tool_calls") and delta.tool_calls:
                    for tc in delta.tool_calls:
                        chunks.append({
                            "type": "tool_call",
                            "id": tc.id or "",
                            "name": tc.function.name if tc.function else "",
                            "arguments": tc.function.arguments if tc.function else "",
                        })

                if finish_reason:
                    chunks.append({
                        "type": "done",
                        "reason": finish_reason,
                    })

            send_response(id, chunks)
        else:
            # Non-streaming response
            response = litellm.completion(
                model=model,
                messages=messages,
                tools=litellm_tools,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            result = {
                "content": response.choices[0].message.content or "",
                "tool_calls": [],
                "finish_reason": response.choices[0].finish_reason,
            }

            if hasattr(response.choices[0].message, "tool_calls"):
                for tc in response.choices[0].message.tool_calls or []:
                    result["tool_calls"].append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": json.loads(tc.function.arguments),
                    })

            send_response(id, result)

    except Exception as e:
        log(f"LiteLLM error: {e}\n{traceback.format_exc()}")
        send_error(id, -32000, f"LiteLLM error: {str(e)}")


def handle_request(line: str) -> None:
    """Parse and route a JSON-RPC request."""
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        log(f"Invalid JSON: {line[:200]}")
        return

    method = request.get("method", "")
    id = request.get("id")
    params = request.get("params", {})

    if id is None:
        # Notification — no response needed
        return

    if method == "initialize":
        handle_initialize(id, params)
    elif method == "chat":
        handle_chat(id, params)
    else:
        send_error(id, -32601, f"Method not found: {method}")


def main() -> None:
    """Main loop — read JSON-RPC requests from stdin."""
    log("LiteLLM Bridge started")

    if not HAS_LITELLM:
        log("WARNING: LiteLLM not installed. Install with: pip install litellm")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        handle_request(line)


if __name__ == "__main__":
    main()
