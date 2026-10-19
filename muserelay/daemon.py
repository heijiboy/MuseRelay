"""HTTP server exposing the Muse bridge (Anthropic + OpenAI + simple REST)."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple

from .browser import BrowserManager
from .config import (
    DEFAULT_TIMEOUT,
    MUSE_NEW_THREAD_URL,
)
from .parser import (
    _detect_intent_tool_call,
    CLARIFY_WAIT_NOTE,
    build_missing_files_note,
    build_unfulfilled_claim_report,
    clean_file_list,
    detect_delete_intent,
    detect_tool_calls,
    find_referenced_local_files,
    format_file_list,
    generate_local_session_title,
    guard_false_completion,
    is_clarifying_question,
    is_delete_all_files_query,
    is_dump_all_contents_query,
    is_file_list_query,
    is_folder_analysis_query,
    is_pure_listing_query,
    is_title_generation_query,
    is_workspace_promise,
    MEDIA_EXTS,
    missing_claimed_paths,
    _looks_textual,
    strip_system_tags,
)
from .prompts import (
    build_project_briefing,
    format_folder_contents,
    get_local_folder_contents,
    list_local_files,
)
from .protocol import AnthropicEmitter, new_msg_id, openai_chat_response


def _extract_cwd(payload: Dict[str, Any], messages: List[dict]) -> str:
    """Pull the agent's working directory out of the system prompt / messages."""
    cwd = os.getcwd()
    sys_val = payload.get("system") or ""
    if isinstance(sys_val, list):
        sys_val = "\n".join(b.get("text", "") for b in sys_val if isinstance(b, dict))
    sys_txts = [str(sys_val)]
    for m in messages:
        if m.get("role") == "system":
            c = m.get("content", "")
            if isinstance(c, list):
                sys_txts.append("\n".join(b.get("text", "") for b in c if isinstance(b, dict)))
            else:
                sys_txts.append(str(c))
    combined_sys = "\n".join(sys_txts)
    m_cwd = re.search(r"Primary working directory:\s*([^\n\r]+)", combined_sys)
    if m_cwd:
        cand_cwd = m_cwd.group(1).strip().strip("\"'")
        if os.path.isdir(cand_cwd):
            cwd = cand_cwd
    return cwd


def _parse_last_user(messages: List[dict]) -> Tuple[bool, List[str], List[str], List[dict], str, Any]:
    """Inspect the most recent user message.

    Returns: (is_tool_result, prev_tool_outputs, executed_tool_names,
              executed_tool_inputs, last_user_msg, last_user_msg_obj)
    """
    is_tool_result = False
    prev_tool_outputs: List[str] = []
    executed_tool_names: List[str] = []
    executed_tool_inputs: List[dict] = []
    last_user_msg = ""

    last_user_msg_obj = None
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_msg_obj = m
            break  # CRITICAL: Only inspect the MOST RECENT user message!

    if last_user_msg_obj:
        c = last_user_msg_obj.get("content", "")
        if isinstance(c, list):
            parts = []
            for block in c:
                if isinstance(block, dict):
                    if block.get("type") == "tool_result":
                        is_tool_result = True
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            rc = "\n".join(rb.get("text", "") for rb in rc if isinstance(rb, dict))
                        prev_tool_outputs.append(str(rc))
                    elif block.get("type") == "text":
                        parts.append(block.get("text", ""))
            if not is_tool_result:
                last_user_msg = "\n".join(parts)
        else:
            last_user_msg = str(c)

    if "[Request interrupted by user]" in last_user_msg:
        last_user_msg = last_user_msg.split("[Request interrupted by user]")[-1].strip()

    if is_tool_result:
        for m in reversed(messages):
            if m.get("role") == "assistant":
                c = m.get("content", [])
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            executed_tool_names.append(b.get("name", ""))
                            ti = b.get("input")
                            if isinstance(ti, dict):
                                executed_tool_inputs.append(ti)
                if executed_tool_names:
                    break

    return is_tool_result, prev_tool_outputs, executed_tool_names, executed_tool_inputs, last_user_msg, last_user_msg_obj


def _original_user_text(messages: List[dict]) -> str:
    """Extract the earliest user's plain text (for matching tool_result follow-ups)."""
    orig = ""
    for m in messages:
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str):
                orig = c
            elif isinstance(c, list):
                txts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
                if txts:
                    orig = "\n".join(txts)
    return strip_system_tags(orig)


def _verify_written_files(tool_inputs: List[dict], tool_results: str, cwd: str) -> str:
    """Verify Write tool calls actually landed on disk before claiming success.

    The old code returned a hardcoded "I have successfully created and updated
    the requested file(s)" for every Write — which lied whenever the parser
    emitted a wrong target (e.g. a text body written into a .mp4 path).
    """
    ok: List[str] = []
    problems: List[str] = []
    checked = 0
    for ti in tool_inputs:
        fp = str(ti.get("file_path", "") or "")
        if not fp:
            continue
        checked += 1
        if not os.path.isfile(fp):
            problems.append(f"`{fp}` — NOT found on disk (the file was not created).")
            continue
        size = os.path.getsize(fp)
        if size == 0:
            problems.append(f"`{fp}` — exists but is empty (0 bytes).")
            continue
        ext = os.path.splitext(fp)[1].lower()
        if ext in MEDIA_EXTS and _looks_textual(fp):
            problems.append(
                f"`{fp}` — extension `{ext}` is a media/binary type but the file content is TEXT; "
                f"the content belongs in a code file (e.g. .html/.css/.js), not here."
            )
            continue
        ok.append(f"`{fp}` ({size:,} bytes)")

    if problems:
        ok_note = f" Verified OK: {'; '.join(ok)}." if ok else ""
        return (
            "The Write tool did NOT complete correctly on disk. Problems:\n"
            + "\n".join(f"- {p}" for p in problems)
            + f"{ok_note}\n"
            "Do NOT claim success for the failed items above. Suggest the user check the paths, "
            "and emit a corrected Write (with the right file_path) if you can determine the intended target."
        )
    if not ok:
        if checked == 0 and re.search(r"\berror\b|\bfailed\b|not found|denied|no such file", tool_results or "", re.I):
            return (
                "The Write tool reported an error — the file was NOT created. "
                f"Tool output:\n{(tool_results or '')[:2000]}\n"
                "Report this accurately; do NOT claim success."
            )
        return (
            "The Write tool result did not include a verifiable file path, so I cannot confirm "
            "the file was created. Please check the file exists before telling the user it is done."
        )
    return (
        "I have successfully created and updated the requested file(s) on your computer:\n"
        + "\n".join(f"- {line}" for line in ok)
    )


def run_daemon_server(mgr: BrowserManager, port: int) -> None:
    class BridgeHTTPHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write(f"[HTTP] {self.address_string()} - {fmt % args}\n")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

        def _send_json(self, status: int, data: Any):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

        def do_GET(self):
            clean_path = self.path.split("?")[0].rstrip("/")
            if clean_path == "/health":
                self._send_json(200, {"status": "ok", "url": mgr.url or ""})
            elif clean_path == "/chat_url":
                self._send_json(200, {"url": mgr.url or mgr.chat_url})
            elif clean_path.startswith("/read"):
                limit = 10
                if "limit=" in self.path:
                    try:
                        limit = int(self.path.split("limit=")[1].split("&")[0])
                    except ValueError:
                        pass
                msgs = mgr.get_messages(limit=limit)
                self._send_json(200, {"status": "ok", "messages": msgs})
            elif clean_path in ("/v1/models", "/models", "/api/v1/models", "/api/models"):
                models_list = [
                    {"id": "muse", "type": "model", "object": "model", "display_name": "Muse AI"},
                    {"id": "stealth/union-alpha", "type": "model", "object": "model", "display_name": "Stealth Union Alpha"},
                    {"id": "claude-3-7-sonnet", "type": "model", "object": "model", "display_name": "Claude 3.7 Sonnet"},
                    {"id": "claude-3-5-sonnet-20241022", "type": "model", "object": "model", "display_name": "Claude 3.5 Sonnet"},
                    {"id": "claude-3-5-haiku-20241022", "type": "model", "object": "model", "display_name": "Claude 3.5 Haiku"},
                ]
                self._send_json(200, {"data": models_list})
            else:
                self._send_json(404, {"error": "Not found"})

        def do_POST(self):
            clean_path = self.path.split("?")[0].rstrip("/")
            content_len = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_len).decode("utf-8", errors="replace") if content_len > 0 else "{}"
            try:
                payload = json.loads(raw_body)
            except Exception:
                payload = {}

            if clean_path == "/chat_url":
                new_url = payload.get("url") or payload.get("chat_url", "")
                if not new_url:
                    self._send_json(400, {"status": "error", "error": "Missing 'url' parameter"})
                    return
                try:
                    final_url = mgr.navigate(new_url)
                    self._send_json(200, {"status": "ok", "url": final_url})
                except Exception as e:
                    self._send_json(500, {"status": "error", "error": str(e)})
                return
            elif clean_path == "/reset":
                try:
                    # Drop the current project's registered thread, then open a fresh one.
                    mgr.clear_active_thread()
                    final_url = mgr.navigate(MUSE_NEW_THREAD_URL)
                    self._send_json(200, {"status": "ok", "url": final_url})
                except Exception as e:
                    self._send_json(500, {"status": "error", "error": str(e)})
                return
            elif clean_path == "/send":
                text = payload.get("text") or payload.get("prompt", "")
                if not text:
                    self._send_json(400, {"status": "error", "error": "Missing 'text' parameter"})
                    return
                timeout_s = payload.get("timeout", DEFAULT_TIMEOUT)
                try:
                    reply, extracted = mgr.send_prompt(text, timeout_s=timeout_s, cwd=os.getcwd())
                    self._send_json(200, {"status": "ok", "reply": reply, "extracted_files": extracted})
                except Exception as e:
                    self._send_json(500, {"status": "error", "error": str(e)})

            elif clean_path in ("/v1/messages/count_tokens", "/messages/count_tokens", "/api/v1/messages/count_tokens"):
                self._send_json(200, {"input_tokens": 50})

            elif clean_path in ("/v1/messages", "/messages", "/api/v1/messages", "/api/messages"):
                self._handle_anthropic_messages(payload)

            elif self.path in ("/v1/chat/completions", "/chat/completions"):
                self._handle_openai_chat(payload)
            else:
                self._send_json(404, {"error": "Endpoint not found"})

        # ------------------------------------------------------------------
        # Anthropic Messages API
        # ------------------------------------------------------------------

        def _handle_anthropic_messages(self, payload: dict) -> None:
            messages = payload.get("messages", [])
            tools = payload.get("tools", [])
            tool_names = {t.get("name") for t in tools if isinstance(t, dict)}
            model_name = payload.get("model", "claude-3-5-sonnet-20241022")
            is_stream = payload.get("stream", False)
            msg_id = new_msg_id()

            cwd = _extract_cwd(payload, messages)
            is_tool_result, prev_tool_outputs, executed_tool_names, executed_tool_inputs, last_user_msg, _ = _parse_last_user(messages)

            if not last_user_msg and not is_tool_result:
                self._send_json(400, {"error": "No user message found"})
                return

            em = AnthropicEmitter(self, msg_id, model_name, stream=is_stream)

            # CRITICAL FIX: Claude Code sends background [SUGGESTION MODE: ...] requests
            # to predict the user's next CLI keystroke. If sent to Muse, Muse stays silent,
            # blocking the daemon for 240s and stalling Claude Code with 'Pontificating...'.
            if "[SUGGESTION MODE:" in last_user_msg:
                print("[*] Intercepted Claude Code SUGGESTION MODE request; returning empty response immediately.", file=sys.stderr, flush=True)
                em.emit_empty(input_tokens=10)
                return

            # Intercept Claude Code automated session title requests.
            if is_title_generation_query(last_user_msg):
                title_text = generate_local_session_title(last_user_msg)
                print(f"[*] Intercepted Claude Code session title request; returning '{title_text}' locally.", file=sys.stderr, flush=True)
                em.emit_title(title_text)
                return

            # send_with_keepalive: for streaming requests, send SSE headers immediately
            # and stream ping comments while Muse generates, so the client does not time out.
            stream_headers_sent = [False]

            def send_with_keepalive(prompt: str, cwd: str) -> Tuple[str, List[str]]:
                if not is_stream or stream_headers_sent[0]:
                    return mgr.send_prompt(prompt, cwd=cwd)

                em.begin(input_tokens=len(last_user_msg.split()))
                stream_headers_sent[0] = True

                res_box: Dict[str, Any] = {}

                def _call():
                    try:
                        r, ext = mgr.send_prompt(prompt, cwd=cwd)
                        res_box["reply"] = r
                        res_box["extracted"] = ext
                    except Exception as ex:
                        res_box["error"] = ex

                wt = threading.Thread(target=_call, daemon=True)
                wt.start()

                while wt.is_alive():
                    wt.join(timeout=2.0)
                    if wt.is_alive():
                        try:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                        except Exception:
                            break

                if "error" in res_box:
                    raise res_box["error"]
                return res_box.get("reply", ""), res_box.get("extracted", [])

            prev_tool_output = "\n\n".join(prev_tool_outputs)
            clean_last_user_msg = strip_system_tags(last_user_msg) if last_user_msg else ""
            tools_to_emit: List[Tuple[str, dict]] = []
            thought = ""
            reply = ""

            if is_tool_result:
                reply = self._handle_tool_result(
                    messages, executed_tool_names, executed_tool_inputs, prev_tool_output, cwd, send_with_keepalive
                )
            else:
                try:
                    reply, tools_to_emit, thought = self._handle_fresh_prompt(
                        clean_last_user_msg, tool_names, cwd, send_with_keepalive
                    )
                except Exception as e:
                    self._send_json(500, {"error": str(e)})
                    return

            if tools_to_emit:
                clean_thought = re.sub(r"<!-- bubble -->", "", thought).strip()
                print(f"[*] Emitting {len(tools_to_emit)} tool call(s) to Claude Code: {[t[0] for t in tools_to_emit]}", file=sys.stderr, flush=True)
                em.emit_tool_use(clean_thought, tools_to_emit, input_tokens=len(last_user_msg.split()))
            else:
                clean_reply = re.sub(r"<!-- bubble -->", "", reply).strip()
                em.emit_text(clean_reply, input_tokens=len(last_user_msg.split()))

        def _handle_tool_result(
            self,
            messages: List[dict],
            executed_tool_names: List[str],
            executed_tool_inputs: List[dict],
            prev_tool_output: str,
            cwd: str,
            send_with_keepalive,
        ) -> str:
            """Synthesize a reply after Claude Code executed a tool call we emitted."""
            if executed_tool_names and all(t in ("Write", "FileWrite") for t in executed_tool_names):
                return _verify_written_files(executed_tool_inputs, prev_tool_output, cwd)

            if "Glob" in executed_tool_names:
                cleaned_files = clean_file_list(prev_tool_output)
                orig_user_msg = _original_user_text(messages)
                if is_dump_all_contents_query(orig_user_msg):
                    return format_folder_contents(get_local_folder_contents(cwd), cwd)
                if is_pure_listing_query(orig_user_msg):
                    return format_file_list(cleaned_files, cwd)

                file_list_str = "\n".join(f"- {f}" for f in cleaned_files)
                prompt_to_muse = (
                    f"Here are the files in the project:\n"
                    f"{file_list_str}\n\n"
                    f"The user asks:\n{orig_user_msg}\n\n"
                    f"Please inspect and analyze which files match the user's question and explain clearly:"
                )
                try:
                    reply, _ = send_with_keepalive(prompt_to_muse, cwd=cwd)
                    return reply
                except Exception as e:
                    return f"Here are the files in the project:\n\n{file_list_str}\n\n(Error analyzing with model: {e})"

            if "Read" in executed_tool_names or "FileRead" in executed_tool_names:
                orig_user_msg = _original_user_text(messages)
                is_pure_dump = bool(re.search(r"^(?:read|cat|view|show(?:\s+me)?)\s+[`\"]?[a-zA-Z0-9_\-\./\\]+[`\"]?\s*$", orig_user_msg, flags=re.IGNORECASE))
                if is_pure_dump or not orig_user_msg:
                    return f"Contents of the file:\n\n```\n{prev_tool_output.strip()}\n```"
                prompt_to_muse = (
                    f"Here is the content of the file from the user's PC:\n\n"
                    f"```\n{prev_tool_output.strip()}\n```\n\n"
                    f"The user asked:\n{orig_user_msg}\n\n"
                    f"Please inspect the file content and provide a thorough, accurate answer:"
                )
                try:
                    reply, _ = send_with_keepalive(prompt_to_muse, cwd=cwd)
                    return reply
                except Exception as e:
                    return f"Contents of the file:\n\n```\n{prev_tool_output.strip()}\n```\n\n(Error analyzing with model: {e})"

            if executed_tool_names and all(t in ("PowerShell", "Bash") for t in executed_tool_names):
                prev_cmds = []
                for m in reversed(messages):
                    if m.get("role") == "assistant":
                        c = m.get("content", [])
                        if isinstance(c, list):
                            for b in c:
                                if isinstance(b, dict) and b.get("type") == "tool_use":
                                    prev_cmds.append(str(b.get("input", {}).get("command", "")).lower())
                        if prev_cmds:
                            break
                if any("remove-item" in cmd or "rm " in cmd for cmd in prev_cmds):
                    return "I have successfully deleted the requested file(s) from your computer."
                return f"Command output:\n\n```\n{prev_tool_output.strip()}\n```"

            return f"Completed action(s) on your computer:\n\n{prev_tool_output}"

        def _handle_fresh_prompt(
            self,
            clean_last_user_msg: str,
            tool_names: set,
            cwd: str,
            send_with_keepalive,
        ) -> Tuple[str, List[Tuple[str, dict]], str]:
            """Handle a fresh (non tool_result) user prompt. Returns (reply, tools_to_emit, thought)."""
            # 0. Direct check: Is this a request to delete all files in current folder?
            if is_delete_all_files_query(clean_last_user_msg):
                del_call = detect_delete_intent(clean_last_user_msg, cwd, tool_names)
                if del_call:
                    thought, t_name, t_input = del_call
                    return "", [(t_name, t_input)], thought
                return f"No files were found in your local folder (`{cwd}`) to delete.", [], ""

            # 1. Direct check: Is this a request to dump/output all file contents in current folder?
            if is_dump_all_contents_query(clean_last_user_msg):
                print(f"[*] Detected dump all files query; reading local folder {cwd}", file=sys.stderr, flush=True)
                local_contents = get_local_folder_contents(cwd)
                return format_folder_contents(local_contents, cwd), [], ""

            # 2. Direct check: Is this a request to list files in current folder?
            if is_file_list_query(clean_last_user_msg):
                print(f"[*] Detected list files query; listing local folder {cwd}", file=sys.stderr, flush=True)
                cleaned_files = list_local_files(cwd)
                return format_file_list(cleaned_files, cwd), [], ""

            # 3. Check if user prompt is directly asking for a local computer / filesystem action
            intent_call = _detect_intent_tool_call(clean_last_user_msg, tool_names, cwd)
            if intent_call:
                thought, t_name, t_input = intent_call
                return "", [(t_name, t_input)], thought

            # 4. Check if any local files on user's PC are referenced in the prompt!
            ref_files = find_referenced_local_files(clean_last_user_msg, cwd)
            is_edit_request = bool(
                re.search(
                    r"\b(?:add|insert|edit|modify|update|solve|change|replace|overwrite|fix|patch|write\s+(?:the\s+)?solution|th[eê]m|s[uử]a|ch[iỉ]nh|c[aậ]p\s*nh[aậ]t|[dđ][oổ]i|thay|vi[eế]t)\b",
                    clean_last_user_msg,
                    flags=re.IGNORECASE,
                )
            )

            prompt_to_send = clean_last_user_msg
            target_file_info = None
            if ref_files:
                file_context_parts = []
                for fname, fpath, fcontent in ref_files:
                    file_context_parts.append(f"[File: {fname} on user's PC]\n{fcontent.strip()}")
                file_context_str = "\n\n".join(file_context_parts)

                if is_edit_request and ("Write" in tool_names or "FileWrite" in tool_names or "Edit" in tool_names):
                    target_file_info = ref_files[0]
                    prompt_to_send = (
                        f"{clean_last_user_msg}\n\n"
                        f"Here is the current content of the file from the user's PC:\n"
                        f"{file_context_str}\n\n"
                        f"Output ONLY the full updated content for `{target_file_info[0]}` with the solution or edit applied (with no conversational filler, or wrap in a code block):"
                    )
                else:
                    prompt_to_send = (
                        f"{clean_last_user_msg}\n\n"
                        f"Here is the content of the referenced file from the user's PC:\n"
                        f"{file_context_str}"
                    )
            elif is_folder_analysis_query(clean_last_user_msg):
                local_contents = get_local_folder_contents(cwd)
                if local_contents:
                    file_context_parts = [f"[File: {fname} on user's PC]\n{fcontent.strip()}" for fname, fcontent in local_contents.items()]
                    file_context_str = "\n\n".join(file_context_parts)
                    prompt_to_send = (
                        f"{clean_last_user_msg}\n\n"
                        f"Here are the files and their contents from the user's local PC (`{cwd}`):\n\n"
                        f"{file_context_str}\n\n"
                        f"Please analyze the user's local files above to answer their question:"
                    )
            elif is_title_generation_query(clean_last_user_msg):
                prompt_to_send = clean_last_user_msg
            elif re.search(r"\b(?:create|make|write|generate|build|implement|develop|platform|architecture|code|deliverable|deliverables|spec|specification|transfer|save|disk|export)\b", clean_last_user_msg, flags=re.IGNORECASE):
                prompt_to_send = (
                    f"{clean_last_user_msg}\n\n"
                    f"[MANDATORY HARNESS INSTRUCTION: You are driving an automated developer harness on the user's local computer. "
                    f"You DO NOT have an internal ~/workspace container, background runner, or bash terminal. "
                    f"Do NOT say 'building it now', 'going into ~/workspace', or that you will report back later. "
                    f"You MUST output the complete, functional code for the requested files directly in this response right now. "
                    f"Format each file with a clear filename header and code block (e.g. ### `filename.ext` followed by ```language ... ```) "
                    f"so the harness can immediately create the files on the user's PC.]"
                )

            m_target = re.search(r"\b(?:create|make|write|named|file|add|save)\s+[`\"']?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]{1,6})[`\"']?", clean_last_user_msg, flags=re.IGNORECASE)
            default_fname = m_target.group(1) if m_target else ""

            # Always attach a compact project briefing (cwd + file tree +
            # CLAUDE.md/AGENTS.md) so Muse knows the project even on a
            # fresh thread. Skip tiny meta-requests like session titles.
            if not is_title_generation_query(clean_last_user_msg):
                briefing = build_project_briefing(cwd)
                if briefing:
                    prompt_to_send = f"{prompt_to_send}\n\n{briefing}"

            try:
                reply, extracted_files = send_with_keepalive(prompt_to_send, cwd=cwd)
            except Exception:
                raise  # propagate to caller -> HTTP 500

            # Check if Muse hallucinated putting files into ~/workspace or background execution
            if not is_title_generation_query(clean_last_user_msg):
                reply, extracted_files = self._retry_workspace_promise(
                    reply, extracted_files, clean_last_user_msg, default_fname, cwd, send_with_keepalive
                )

            # Keep the raw Muse reply for disk verification (before any rewrite below).
            muse_reply_raw = reply

            # Muse is asking the user to choose (option chips on its web UI, or a
            # clarifying question) — nothing was executed. Surface the question and
            # emit NO tools, so Claude Code cannot report the work as done.
            if (
                not extracted_files
                and not is_title_generation_query(clean_last_user_msg)
                and ("[MUSE OFFERED CHOICES" in reply or is_clarifying_question(muse_reply_raw))
            ):
                if "[MUSE OFFERED CHOICES" not in reply:
                    reply = f"{reply.strip()}\n\n{CLARIFY_WAIT_NOTE}"
                print(
                    "[*] Muse asked a clarifying question — returning it without tools.",
                    file=sys.stderr, flush=True,
                )
                thought = "Muse is waiting for the user to choose an option; nothing was executed."
                return reply, [], thought

            if extracted_files:
                file_list_preview = "\n".join(f"- `{f}`" for f in sorted(extracted_files[:25]))
                more_count = len(extracted_files) - 25
                more_suffix = f"\n- ... and {more_count} more files" if more_count > 0 else ""
                image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".svg"}
                if all(os.path.splitext(f)[1].lower() in image_exts for f in extracted_files):
                    # Media-only harvest: keep Muse's actual words and only append
                    # what was really saved to disk (never replace the reply).
                    thought = f"Saved {len(extracted_files)} image(s) from Muse's chat into your workspace (`{cwd}`)."
                    reply = (
                        f"{muse_reply_raw.strip()}\n\n"
                        f"[Harness verification — saved to disk] {len(extracted_files)} image(s) attached "
                        f"in Muse's chat were saved to your workspace (`{cwd}`):\n\n"
                        f"{file_list_preview}{more_suffix}"
                    )
                else:
                    thought = f"Extracted {len(extracted_files)} files directly into your workspace (`{cwd}`)."
                    reply = (
                        f"I have built the project and automatically downloaded and extracted all {len(extracted_files)} files directly into your local workspace (`{cwd}`):\n\n"
                        f"{file_list_preview}{more_suffix}\n\n"
                        f"All files are saved to disk and ready to use."
                    )

            thought, tools_to_emit = detect_tool_calls(reply, tool_names, cwd, default_fname=default_fname)
            if not tools_to_emit and is_edit_request and target_file_info and ("Write" in tool_names or "FileWrite" in tool_names):
                _tgt_ext = os.path.splitext(target_file_info[1])[1].lower()
                if _tgt_ext in MEDIA_EXTS:
                    print(
                        f"[!] Skipping edit-request fallback: target `{target_file_info[1]}` has a media extension "
                        f"({_tgt_ext}) — writing text into it would corrupt the file.",
                        file=sys.stderr, flush=True,
                    )
                else:
                    clean_sol = reply.strip()
                    m_fence = re.search(r"^```(?:[a-zA-Z0-9_\-]+)?\n+(.*?)\n+```$", clean_sol, flags=re.DOTALL)
                    if m_fence:
                        clean_sol = m_fence.group(1).strip()
                    t_name = "Write" if "Write" in tool_names else "FileWrite"
                    thought = f"Updating `{target_file_info[0]}` with the solution on your computer."
                    tools_to_emit = [(t_name, {"file_path": target_file_info[1], "content": clean_sol})]

            # Honesty guard: verify Muse's file-creation claims against the real disk.
            reply, tools_to_emit, thought, extracted_files = guard_false_completion(
                reply=reply,
                claim_source=muse_reply_raw,
                tools_to_emit=tools_to_emit,
                thought=thought,
                extracted_files=extracted_files,
                cwd=cwd,
                tool_names=tool_names,
                default_fname=default_fname,
                send_fn=send_with_keepalive,
            )

            return reply, tools_to_emit, thought

        def _retry_workspace_promise(
            self,
            reply: str,
            extracted_files: List[str],
            clean_last_user_msg: str,
            default_fname: str,
            cwd: str,
            send_with_keepalive,
        ) -> Tuple[str, List[str]]:
            """Re-prompt Muse (up to 2x) if it is stalling with a ~/workspace promise instead of emitting code."""
            for retry_attempt in range(2):
                if not is_workspace_promise(reply, default_fname=default_fname) or extracted_files:
                    break
                print(f"[*] Detected workspace promise from Muse (attempt {retry_attempt + 1}/2); automatically re-prompting for direct code output...", file=sys.stderr, flush=True)
                is_multi_file_project = bool(
                    re.search(
                        r"\b(?:platform|entire project|full project|multi-file|full stack|scaffold|repository|codebase|archive|tar\.gz|zip)\b",
                        clean_last_user_msg,
                        flags=re.IGNORECASE,
                    )
                )
                if is_multi_file_project:
                    retry_prompt = (
                        "I need the project on my local computer. Please package all files from your workspace as a downloadable archive (tar.gz or zip), "
                        "or output the files with their filenames and complete code in markdown code blocks, so the automated harness can extract or save them to disk immediately."
                    )
                else:
                    m_fn = re.search(r"\b([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]{1,6})\b", clean_last_user_msg)
                    fn_mention = f" for `{m_fn.group(1)}`" if m_fn else ""
                    ext_mention = m_fn.group(1).split(".")[-1] if m_fn else ""
                    retry_prompt = (
                        f"Please output the complete, raw content{fn_mention} directly in your chat response inside a markdown code block "
                        f"(```{ext_mention} ... ```). Do not package it as an archive or keep it in a private workspace."
                    )
                try:
                    retry_reply, retry_extracted = send_with_keepalive(retry_prompt, cwd=cwd)
                    if retry_reply:
                        reply = retry_reply
                    if retry_extracted:
                        extracted_files = retry_extracted
                except Exception as ex:
                    print(f"[!] Error on workspace promise retry: {ex}", file=sys.stderr, flush=True)
                    break
            return reply, extracted_files

        # ------------------------------------------------------------------
        # OpenAI chat.completions
        # ------------------------------------------------------------------

        def _handle_openai_chat(self, payload: dict) -> None:
            messages = payload.get("messages", [])
            last_user_msg = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    last_user_msg = m.get("content", "")
                    break
            if not last_user_msg and messages:
                last_user_msg = messages[-1].get("content", "")

            if not last_user_msg:
                self._send_json(400, {"error": "No user message found"})
                return

            try:
                reply, _ = mgr.send_prompt(last_user_msg)
                resp = openai_chat_response(reply, model=payload.get("model", "muse"), prompt=str(last_user_msg))
                self._send_json(200, resp)
            except Exception as e:
                self._send_json(500, {"error": str(e)})

    server = ThreadingHTTPServer(("127.0.0.1", port), BridgeHTTPHandler)
    print(f"[Daemon] HTTP Server listening at http://127.0.0.1:{port}")
    print(f"  - Health check:            http://127.0.0.1:{port}/health")
    print(f"  - Anthropic API (Claude):  POST http://127.0.0.1:{port}/v1/messages")
    print(f"  - OpenAI API:              POST http://127.0.0.1:{port}/v1/chat/completions")
    print(f"  - Send endpoint:           POST http://127.0.0.1:{port}/send")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()
