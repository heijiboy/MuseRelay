"""muserelay -- Unofficial Meta Muse AI bridge for Claude Code & local agents.

Package layout:
  config    -- constants, config file IO, per-project Muse thread registry
  parser    -- reply parsing, tool-call detection, local intent handling
  prompts   -- project briefing / folder content builders
  browser   -- Playwright automation + BrowserManager daemon worker
  protocol  -- Anthropic & OpenAI wire-format (SSE + JSON) shaping
  daemon    -- HTTP server exposing the bridge
  cli       -- argparse entry points (login/serve/send/read/inspect/mcp/...)
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

__version__ = "1.1.0"
