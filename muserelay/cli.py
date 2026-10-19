"""CLI entry points: login / serve / send / read / inspect / mcp / launch-chrome."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from .browser import (
    BrowserManager,
    _click_send,
    _extract_messages,
    _fill_composer,
    _launch_browser_and_context,
    _wait_for_reply,
    detect_selectors,
    require_playwright,
)
from .config import (
    BASE_DIR,
    CONFIG_FILE,
    DAEMON_COMPOSER_SEL,
    DAEMON_MSG_SEL,
    DAEMON_SEND_SEL,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    STATE_FILE,
    load_config,
    save_config,
)
from .daemon import run_daemon_server


def _ensure_configured(cfg: Dict[str, Any]) -> str:
    if not STATE_FILE.exists():
        sys.exit(
            "No saved login session found.\n"
            "Please run: python muse_relay.py login"
        )
    chat_url = cfg.get("chat_url", "").strip()
    if not chat_url or chat_url.startswith("PASTE_"):
        sys.exit(
            "No chat_url configured.\n"
            "Please run: python muse_relay.py login"
        )
    return chat_url


def _query_daemon(endpoint: str, data: Optional[dict] = None, port: int = DEFAULT_PORT) -> Optional[dict]:
    """Check if the local background daemon is running and respond."""
    url = f"http://127.0.0.1:{port}{endpoint}"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8") if data is not None else None,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _oneshot_send(cfg: Dict[str, Any], text: str, timeout_s: int, headless: bool = True) -> str:
    """Launch a one-shot headless browser, send a prompt, and return the reply.

    Shared by `cmd_send` (daemon-down fallback) and `cmd_mcp` (no-daemon mode).
    """
    sync_playwright, PlaywrightTimeoutError = require_playwright()
    chat_url = _ensure_configured(cfg)
    selectors = cfg.get("selectors", {})
    composer_sel = selectors.get("composer") or DAEMON_COMPOSER_SEL
    send_sel = selectors.get("send_button") or DAEMON_SEND_SEL
    msg_sel = selectors.get("message") or DAEMON_MSG_SEL

    print(f"[*] Launching browser (headless={headless})...", file=sys.stderr, flush=True)
    with sync_playwright() as p:
        browser, ctx = _launch_browser_and_context(p, cfg, headless=headless, with_storage=True)
        page = ctx.new_page()
        print(f"[*] Navigating to {chat_url}...", file=sys.stderr, flush=True)
        page.goto(chat_url, timeout=45_000)

        print("[*] Waiting for composer...", file=sys.stderr, flush=True)
        try:
            page.wait_for_selector(composer_sel, timeout=30_000)
        except PlaywrightTimeoutError:
            detected = detect_selectors(page)
            composer_sel = detected.get("composer") or composer_sel
            send_sel = detected.get("send_button") or send_sel
            msg_sel = detected.get("message") or msg_sel

        # Ensure existing chat history is loaded before calculating n_before
        try:
            page.wait_for_selector(msg_sel, timeout=10_000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        msgs_before = _extract_messages(page, msg_sel)
        n_before = len(msgs_before)
        prev_texts = {
            m["text"].strip()
            for m in msgs_before
            if m.get("author") == "assistant" and m.get("text", "").strip()
        }
        print(f"[*] Found {n_before} messages in thread. Submitting prompt...", file=sys.stderr, flush=True)
        _fill_composer(page, composer_sel, text)
        _click_send(page, send_sel, composer_sel)
        print("[*] Message submitted. Waiting for Muse AI response...", file=sys.stderr, flush=True)

        reply = _wait_for_reply(page, msg_sel, n_before, timeout_s=timeout_s, prompt_text=text, prev_texts=prev_texts)
        browser.close()
        return reply


def cmd_login(args: argparse.Namespace) -> None:
    """Headed login flow: opens browser, logs in, auto-discovers selectors."""
    sync_playwright, _ = require_playwright()
    cfg = load_config()
    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "cdp", None):
        cfg["cdp_url"] = args.cdp

    target_url = args.url or cfg.get("chat_url") or "https://muse.ai"

    print("=" * 60)
    print("MUSE AI LOGIN & SETUP")
    print("=" * 60)
    print("Launching Chromium browser window...")
    print("  [✓] Location spoofing: US (New York) + America/New_York timezone")
    if cfg.get("proxy"):
        print(f"  [✓] Proxy: {cfg['proxy']}")
    if cfg.get("cdp_url"):
        print(f"  [✓] Attaching via CDP to: {cfg['cdp_url']}")
    print("1. Log in to your Muse account.")
    print("2. Open the chat you want to use with Claude Code.")
    print("3. Return here and press ENTER.")
    print("-" * 60)

    with sync_playwright() as p:
        browser, ctx = _launch_browser_and_context(p, cfg, headless=False, with_storage=False)
        page = ctx.new_page()
        page.goto(target_url)

        try:
            input("\n--> Press ENTER here after logging in and opening your chat: ")
        except (KeyboardInterrupt, EOFError):
            print("\nLogin cancelled.")
            browser.close()
            return

        # Auto-detect selectors from current page
        print("\nAnalyzing page DOM and discovering selectors...")
        detected = detect_selectors(page)

        # Save session state
        ctx.storage_state(path=str(STATE_FILE))
        try:
            STATE_FILE.chmod(0o600)
        except OSError:
            pass

        # Update config
        final_url = page.url
        cfg["chat_url"] = final_url
        cfg["selectors"]["composer"] = detected.get("composer", "")
        cfg["selectors"]["send_button"] = detected.get("send_button", "")
        cfg["selectors"]["message_list"] = detected.get("message_list", "")
        cfg["selectors"]["message"] = detected.get("message", "")
        save_config(cfg)

        print(f"\n[OK] Session saved to {STATE_FILE.name}")
        print(f"[OK] Chat URL saved: {final_url}")
        print("[OK] Discovered selectors:")
        print(f"     Composer:    {cfg['selectors']['composer']}")
        print(f"     Send Button: {cfg['selectors']['send_button']}")
        print(f"     Messages:    {cfg['selectors']['message']}")
        print(f"[OK] Configuration written to {CONFIG_FILE.name}")
        print("\nYou can now test sending messages with:")
        print('    python muse_relay.py send "hello"')
        browser.close()


def cmd_send(args: argparse.Namespace) -> None:
    cfg = load_config()
    port = args.port or cfg.get("port", DEFAULT_PORT)

    # 1. Try sending through running daemon (fast mode)
    daemon_health = _query_daemon("/health", port=port)
    if daemon_health and daemon_health.get("status") == "ok":
        resp = _query_daemon("/send", {"text": args.text, "timeout": args.timeout}, port=port)
        if resp and resp.get("status") == "ok":
            reply = resp.get("reply", "")
            if args.json:
                print(json.dumps({"reply": reply}, ensure_ascii=False))
            else:
                print(reply)
            return

    # 2. Fallback: launch one-shot headless browser
    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "cdp", None):
        cfg["cdp_url"] = args.cdp
    headless = not args.headful if args.headful else cfg.get("headless", True)

    reply = _oneshot_send(cfg, args.text, args.timeout, headless=headless)
    if args.json:
        print(json.dumps({"reply": reply}, ensure_ascii=False))
    else:
        print(reply)


def cmd_read(args: argparse.Namespace) -> None:
    sync_playwright, _ = require_playwright()
    cfg = load_config()
    port = args.port or cfg.get("port", DEFAULT_PORT)
    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "cdp", None):
        cfg["cdp_url"] = args.cdp

    # 1. Try daemon
    resp = _query_daemon(f"/read?limit={args.limit}", port=port)
    if resp and resp.get("status") == "ok":
        print(json.dumps(resp.get("messages", []), indent=2, ensure_ascii=False))
        return

    # 2. One-shot browser
    chat_url = args.chat_url or _ensure_configured(cfg)
    headless = not args.headful if args.headful else cfg.get("headless", True)
    msg_sel = cfg.get("selectors", {}).get("message") or ".hatch-chat-groupable-bubble, [data-hatch-assistant-message-body='true']"

    with sync_playwright() as p:
        browser, ctx = _launch_browser_and_context(p, cfg, headless=headless, with_storage=True)
        page = ctx.new_page()
        page.goto(chat_url, timeout=45_000)
        try:
            page.wait_for_selector(msg_sel, timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        msgs = _extract_messages(page, msg_sel)
        print(json.dumps(msgs[-args.limit:], indent=2, ensure_ascii=False))
        browser.close()


def cmd_inspect(args: argparse.Namespace) -> None:
    """Inspect the chat DOM and take a diagnostic screenshot."""
    sync_playwright, _ = require_playwright()
    cfg = load_config()
    chat_url = args.chat_url or _ensure_configured(cfg)
    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "cdp", None):
        cfg["cdp_url"] = args.cdp

    print("Launching browser to inspect DOM at:", chat_url)
    with sync_playwright() as p:
        browser, ctx = _launch_browser_and_context(p, cfg, headless=not args.headful, with_storage=True)
        page = ctx.new_page()
        page.goto(chat_url, timeout=45_000)
        try:
            page.wait_for_selector("textarea", timeout=20_000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        detected = detect_selectors(page)
        screenshot_path = BASE_DIR / "debug_screenshot.png"
        page.screenshot(path=str(screenshot_path))

        print("\n--- Detected Elements ---")
        print(f"Page Title:   {page.title()}")
        print(f"Page URL:     {page.url}")
        print(f"Composer:     {detected.get('composer')}")
        print(f"Send Button:  {detected.get('send_button')}")
        print(f"Message List: {detected.get('message_list')}")
        print(f"Message:      {detected.get('message')}")
        print(f"\nScreenshot saved to: {screenshot_path}")

        msg_sel = cfg.get("selectors", {}).get("message") or detected.get("message", "")
        msgs = _extract_messages(page, msg_sel)
        print(f"Extracted {len(msgs)} messages:")
        for m in msgs[-3:]:
            snippet = m["text"].replace("\n", " ")[:80]
            print(f"  - [{m.get('author') or 'unknown'}]: {snippet}...")

        browser.close()


def cmd_serve(args: argparse.Namespace) -> None:
    cfg = load_config()
    chat_url = args.chat_url or _ensure_configured(cfg)
    port = args.port or cfg.get("port", DEFAULT_PORT)
    headless = not args.headful if args.headful else cfg.get("headless", True)

    mgr = BrowserManager(chat_url=chat_url, headless=headless)
    try:
        mgr.start()
        run_daemon_server(mgr, port)
    finally:
        mgr.stop()


def cmd_launch_chrome(args: argparse.Namespace) -> None:
    """Launch Google Chrome (or Edge) with remote debugging enabled on Windows."""
    import subprocess
    port = args.port or 9222
    url = args.url or "https://muse.ai"
    user_data = Path(os.environ.get("LOCALAPPDATA", ".")) / "Google" / "Chrome" / "User Data Debug"

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    browser_exe = None
    for c in candidates:
        if os.path.exists(c):
            browser_exe = c
            break

    if not browser_exe:
        sys.exit("Error: Could not find Chrome or Edge executable on this machine.")

    cmd = [
        browser_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        url,
    ]
    print(f"Launching browser with remote debugging on port {port}...")
    subprocess.Popen(cmd)
    print(f"\n[OK] Browser launched!")
    print(f"1. Log in to Muse and open your chat in the opened Chrome window.")
    print(f"2. Then run:")
    print(f"    python muse_relay.py login --cdp http://127.0.0.1:{port}")


# ---------------------------------------------------------------------------
# MCP Server Mode (Model Context Protocol over stdio for Claude Code)
# ---------------------------------------------------------------------------

def cmd_mcp(args: argparse.Namespace) -> None:
    """Run an MCP server over stdio for Claude Code."""
    cfg = load_config()
    port = args.port or cfg.get("port", DEFAULT_PORT)

    def send_rpc(obj: dict):
        line = json.dumps(obj)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        req_id = req.get("id")
        method = req.get("method")

        if method == "initialize":
            send_rpc({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "muserelay", "version": "1.0.0"},
                },
            })
        elif method == "notifications/initialized":
            pass
        elif method == "ping":
            send_rpc({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "tools/list":
            send_rpc({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "ask_muse",
                            "description": "Send a question or task to Meta Muse AI (1 billion tokens) and receive its answer.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "prompt": {
                                        "type": "string",
                                        "description": "The message or prompt to send to Muse",
                                    }
                                },
                                "required": ["prompt"],
                            },
                        },
                        {
                            "name": "read_muse_chat",
                            "description": "Read recent chat messages from the Muse AI session.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "limit": {
                                        "type": "integer",
                                        "description": "Number of recent messages to return (default: 10)",
                                        "default": 10,
                                    }
                                },
                            },
                        },
                    ]
                },
            })
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            arguments = params.get("arguments", {})

            if name == "ask_muse":
                prompt = arguments.get("prompt", "")
                reply_text = ""
                daemon_res = _query_daemon("/send", {"text": prompt}, port=port)
                if daemon_res and daemon_res.get("status") == "ok":
                    reply_text = daemon_res.get("reply", "")
                else:
                    try:
                        reply_text = _oneshot_send(cfg, prompt, DEFAULT_TIMEOUT, headless=True)
                    except Exception as ex:
                        reply_text = f"Error communicating with Muse: {ex}"

                send_rpc({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": reply_text}],
                        "isError": False,
                    },
                })

            elif name == "read_muse_chat":
                limit = arguments.get("limit", 10)
                daemon_res = _query_daemon(f"/read?limit={limit}", port=port)
                if daemon_res and daemon_res.get("status") == "ok":
                    msgs = daemon_res.get("messages", [])
                else:
                    msgs = []
                send_rpc({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(msgs, indent=2)}],
                        "isError": False,
                    },
                })
            else:
                send_rpc({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {name}"},
                })


# ---------------------------------------------------------------------------
# CLI Argument Parser & Entry Point
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Unofficial Meta Muse AI bridge for Claude Code & local agents."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    # login
    p_login = sub.add_parser("login", help="Headed login & automatic selector discovery")
    p_login.add_argument("--url", default=None, help="Custom chat or login URL (default: https://muse.ai)")
    p_login.add_argument("--proxy", default=None, help="Proxy server e.g. http://127.0.0.1:1080 or socks5://...")
    p_login.add_argument("--cdp", default=None, help="Connect to running Chrome via CDP e.g. http://127.0.0.1:9222")
    p_login.set_defaults(fn=cmd_login)

    # send
    p_send = sub.add_parser("send", help="Send a prompt and return the assistant reply")
    p_send.add_argument("text", help="Message or prompt to send")
    p_send.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Seconds to wait for complete reply")
    p_send.add_argument("--json", action="store_true", help="Output reply as JSON")
    p_send.add_argument("--chat-url", default=None, help="Override chat URL")
    p_send.add_argument("--headful", action="store_true", help="Show browser window for debugging")
    p_send.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port of background daemon if active")
    p_send.add_argument("--proxy", default=None, help="Proxy server e.g. http://127.0.0.1:1080 or socks5://...")
    p_send.add_argument("--cdp", default=None, help="Connect to running Chrome via CDP e.g. http://127.0.0.1:9222")
    p_send.set_defaults(fn=cmd_send)

    # read
    p_read = sub.add_parser("read", help="Print recent chat messages as JSON")
    p_read.add_argument("--limit", type=int, default=10, help="Number of messages to fetch")
    p_read.add_argument("--chat-url", default=None, help="Override chat URL")
    p_read.add_argument("--headful", action="store_true", help="Show browser window")
    p_read.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port of background daemon if active")
    p_read.add_argument("--proxy", default=None, help="Proxy server")
    p_read.add_argument("--cdp", default=None, help="Connect via CDP")
    p_read.set_defaults(fn=cmd_read)

    # inspect
    p_inspect = sub.add_parser("inspect", help="Diagnose chat DOM selectors and capture a debug screenshot")
    p_inspect.add_argument("--chat-url", default=None, help="Override chat URL")
    p_inspect.add_argument("--headful", action="store_true", help="Show browser window")
    p_inspect.add_argument("--proxy", default=None, help="Proxy server")
    p_inspect.add_argument("--cdp", default=None, help="Connect via CDP")
    p_inspect.set_defaults(fn=cmd_inspect)

    # serve (background daemon)
    p_serve = sub.add_parser("serve", help="Run background daemon with fast API & OpenAI compatibility")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default: {DEFAULT_PORT})")
    p_serve.add_argument("--chat-url", default=None, help="Override chat URL")
    p_serve.add_argument("--headful", action="store_true", help="Show browser window while running")
    p_serve.add_argument("--proxy", default=None, help="Proxy server")
    p_serve.add_argument("--cdp", default=None, help="Connect via CDP")
    p_serve.set_defaults(fn=cmd_serve)

    # launch-chrome
    p_chrome = sub.add_parser("launch-chrome", help="Launch Chrome with remote debugging enabled")
    p_chrome.add_argument("--port", type=int, default=9222, help="Debugging port (default: 9222)")
    p_chrome.add_argument("--url", default="https://muse.ai", help="Initial URL")
    p_chrome.set_defaults(fn=cmd_launch_chrome)

    # mcp (Model Context Protocol)
    p_mcp = sub.add_parser("mcp", help="Run as Model Context Protocol (MCP) server for Claude Code")
    p_mcp.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to query daemon from")
    p_mcp.set_defaults(fn=cmd_mcp)

    args = ap.parse_args(argv)
    args.fn(args)
