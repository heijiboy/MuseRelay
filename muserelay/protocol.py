"""Wire-format shaping for the Anthropic Messages API and OpenAI chat API.

The bridge speaks two protocols to local agents:
  - Anthropic Messages API (native Claude Code endpoint) with SSE streaming
  - OpenAI chat.completions (for other tools)

This module centralizes the SSE envelope and message JSON construction that
was previously inlined (and duplicated) inside the daemon HTTP handler.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Tuple


def new_msg_id() -> str:
    return f"msg_muse_{int(time.time())}"


def new_tool_id(idx: int) -> str:
    return f"toolu_{int(time.time())}_{idx}"


def estimate_tokens(text: str) -> int:
    """Cheap word-count token estimate."""
    return len(text.split())


class AnthropicEmitter:
    """Builds Anthropic Messages API responses for the daemon handler.

    The handler sets `self.is_stream` (bool) before dispatching; this emitter
    reads it via the constructor `stream` flag to decide SSE vs JSON.

    Usage:
        em = AnthropicEmitter(handler, msg_id, model_name, stream=is_stream)
        em.begin(input_tokens=10)     # sends 200 + headers (SSE) or nothing (JSON)
        em.text_block("hello")        # stream only
        em.finish_text(text)          # end_turn; JSON emits the full text at once
    """

    def __init__(self, handler, msg_id: str, model_name: str, stream: bool):
        self.h = handler
        self.msg_id = msg_id
        self.model = model_name
        self.stream = stream
        self.stream_started = False
        self.msg_started = False

    # -- low-level SSE ------------------------------------------------------

    def _sse(self, event: str, data: dict) -> None:
        line = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
        try:
            self.h.wfile.write(line)
            self.h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _json(self, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.h.send_response(200)
            self.h.send_header("Content-Type", "application/json; charset=utf-8")
            self.h.send_header("Content-Length", str(len(body)))
            self.h.send_header("Access-Control-Allow-Origin", "*")
            self.h.end_headers()
            self.h.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    # -- envelope (SSE only) ------------------------------------------------

    def _start_stream(self) -> None:
        if self.stream_started:
            return
        self.h.send_response(200)
        self.h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.h.send_header("Cache-Control", "no-cache")
        self.h.send_header("Connection", "close")
        self.h.send_header("Access-Control-Allow-Origin", "*")
        self.h.end_headers()
        self.stream_started = True

    def _message_start(self, input_tokens: int, output_tokens: int = 1) -> None:
        self._sse("message_start", {
            "type": "message_start",
            "message": {
                "id": self.msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": self.model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            },
        })
        self.msg_started = True

    def begin(self, input_tokens: int = 10) -> None:
        """Send 200 + SSE headers + message_start. No-op for JSON mode."""
        if not self.stream:
            return
        self._start_stream()
        if not self.msg_started:
            self._message_start(input_tokens)

    # -- public emitters ----------------------------------------------------

    def emit_text(self, text: str, input_tokens: int = 10) -> None:
        """Emit a plain assistant text reply (stop_reason=end_turn)."""
        if self.stream:
            self.begin(input_tokens)
            self._sse("content_block_start", {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            })
            chunk = 40
            for i in range(0, len(text), chunk):
                self._sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text[i:i + chunk]},
                })
                time.sleep(0.01)
            self._sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            self._sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": estimate_tokens(text)},
            })
            self._sse("message_stop", {"type": "message_stop"})
            self.h.close_connection = True
        else:
            self._json({
                "id": self.msg_id,
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "model": self.model,
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": estimate_tokens(text)},
            })

    def emit_tool_use(self, thought: str, tools: List[Tuple[str, dict]], input_tokens: int = 10) -> None:
        """Emit a tool_use reply (stop_reason=tool_use) with an optional leading text block."""
        if self.stream:
            self.begin(input_tokens)
            idx = 0
            clean_thought = thought.strip()
            if clean_thought:
                self._sse("content_block_start", {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "text", "text": ""},
                })
                self._sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": clean_thought + "\n\n"},
                })
                self._sse("content_block_stop", {"type": "content_block_stop", "index": idx})
                idx += 1

            total_out = 0
            for i, (t_name, t_input) in enumerate(tools):
                tool_id = new_tool_id(i)
                self._sse("content_block_start", {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "tool_use", "id": tool_id, "name": t_name, "input": {}},
                })
                raw_in = json.dumps(t_input, ensure_ascii=False)
                total_out += len(raw_in) // 4 + 10
                self._sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "input_json_delta", "partial_json": raw_in},
                })
                self._sse("content_block_stop", {"type": "content_block_stop", "index": idx})
                idx += 1

            self._sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": total_out},
            })
            self._sse("message_stop", {"type": "message_stop"})
            self.h.close_connection = True
        else:
            content_blocks: List[dict] = []
            if thought.strip():
                content_blocks.append({"type": "text", "text": thought.strip()})
            total_out = 0
            for i, (t_name, t_input) in enumerate(tools):
                content_blocks.append({
                    "type": "tool_use",
                    "id": new_tool_id(i),
                    "name": t_name,
                    "input": t_input,
                })
                total_out += len(json.dumps(t_input)) // 4 + 10

            self._json({
                "id": self.msg_id,
                "type": "message",
                "role": "assistant",
                "content": content_blocks,
                "model": self.model,
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": total_out},
            })

    def emit_empty(self, input_tokens: int = 10) -> None:
        """Emit a no-op assistant reply (short-circuits suggestion-mode requests)."""
        self.emit_text("", input_tokens=input_tokens)

    def emit_title(self, title: str, input_tokens: int = 15) -> None:
        """Emit a locally-generated session title as a text reply."""
        self.emit_text(title, input_tokens=input_tokens)


def openai_chat_response(reply: str, model: str = "muse", prompt: str = "") -> Dict[str, Any]:
    """Shape an OpenAI chat.completion response object."""
    return {
        "id": f"chatcmpl-muse-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": estimate_tokens(prompt),
            "completion_tokens": estimate_tokens(reply),
            "total_tokens": estimate_tokens(prompt) + estimate_tokens(reply),
        },
    }
