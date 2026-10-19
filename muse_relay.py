#!/usr/bin/env python3
"""
muse_relay.py -- Backward-compatible entry point for the MuseRelay package.

The real implementation lives in the `muserelay/` package:
  muserelay/config.py     constants, config IO, per-project thread registry
  muserelay/parser.py     reply parsing, tool-call detection, local intents
  muserelay/prompts.py    project briefing / folder content builders
  muserelay/browser.py    Playwright automation + BrowserManager
  muserelay/protocol.py   Anthropic + OpenAI wire-format shaping
  muserelay/daemon.py     HTTP server (Anthropic / OpenAI / REST)
  muserelay/cli.py        login / serve / send / read / inspect / mcp

This thin shim preserves the documented `python muse_relay.py <cmd>` interface
so start_muse_relay.bat, switch_provider.py, and README all keep working.
"""

from muserelay.cli import main

if __name__ == "__main__":
    main()
