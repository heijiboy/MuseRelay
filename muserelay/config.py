"""Constants, config-file IO, and the per-project Muse thread registry."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

# Project root = the folder that holds muse_relay_config.json / muse_relay_state.json.
# This is the repo checkout, i.e. the parent of the muserelay/ package dir.
BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "muse_relay_state.json"
CONFIG_FILE = BASE_DIR / "muse_relay_config.json"
THREAD_REGISTRY_FILE = BASE_DIR / "muse_relay_threads.json"


def _adopt_legacy(new_path: Path, legacy_name: str) -> Path:
    """Keep working with pre-rebrand files (storage_state.json, etc.)."""
    if not new_path.exists():
        legacy = BASE_DIR / legacy_name
        if legacy.exists():
            return legacy
    return new_path


STATE_FILE = _adopt_legacy(STATE_FILE, "storage_state.json")
CONFIG_FILE = _adopt_legacy(CONFIG_FILE, "bridge_config.json")
THREAD_REGISTRY_FILE = _adopt_legacy(THREAD_REGISTRY_FILE, "thread_registry.json")

# Local-only files that must NEVER be sent to Muse (session cookies, private
# thread IDs, config, logs). Matched case-insensitively by basename.
IGNORED_LOCAL_FILES = {
    "storage_state.json", "muse_relay_state.json",
    "bridge_config.json", "muse_relay_config.json",
    "thread_registry.json", "muse_relay_threads.json",
    "claude_request_sample.json",
    "muse_relay.log", "bridge_run.log",
}
IGNORED_LOCAL_SUFFIXES = (".session.json", ".cookie", ".cookies", ".token", ".log")


def is_ignored_local_file(name: str) -> bool:
    n = os.path.basename(name).lower()
    if n in IGNORED_LOCAL_FILES:
        return True
    return any(n.endswith(s) for s in IGNORED_LOCAL_SUFFIXES)

DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 600
POLL_INTERVAL_S = 0.5
SETTLE_POLLS = 4
# Muse's SPA sometimes keeps newly sent messages out of the groupable-bubble DOM
# until a page reload. If no new bubble shows up for this long, reload the thread.
STALL_RELOAD_S = 30
MAX_STALL_RELOADS = 4

MUSE_HOME_URL = "https://muse.ai"
MUSE_NEW_THREAD_URL = "https://muse.ai/thread/new"

# Bubble separator Muse uses to split multi-part assistant replies.
BUBBLE_SEP = "\n\n<!-- bubble -->\n\n"

# Fallback DOM selectors (used when config has none / detection fails).
DEFAULT_SELECTORS: Dict[str, str] = {
    "composer": "textarea, div[contenteditable=\"true\"], [role=\"textbox\"]",
    "send_button": "button[type=\"submit\"], button[aria-label*=\"send\" i]",
    "message_list": "[role=\"log\"], main, div[class*=\"chat\" i]",
    "message": "[data-role=\"assistant\"], [data-role=\"user\"], div[class*=\"message\" i], article",
}

# Narrower defaults used by the long-lived daemon worker.
DAEMON_COMPOSER_SEL = "textarea[placeholder='Message'], textarea"
DAEMON_SEND_SEL = 'button[aria-label="Send"], button[aria-label*="Send" i]'
DAEMON_MSG_SEL = ".hatch-chat-groupable-bubble"

DEFAULT_CONFIG: Dict[str, Any] = {
    "chat_url": "",
    "port": DEFAULT_PORT,
    "headless": True,
    "timeout": DEFAULT_TIMEOUT,
    "proxy": "",  # e.g. "http://127.0.0.1:1080" or "socks5://127.0.0.1:1080"
    "cdp_url": "",  # e.g. "http://127.0.0.1:9222" to attach to your real browser
    "geolocation": {
        "latitude": 40.7128,
        "longitude": -74.0060,
    },
    "locale": "en-US",
    "timezone_id": "America/New_York",
    "selectors": {
        "composer": "",
        "send_button": "",
        "message_list": "",
        "message": "",
    },
}


def load_config() -> Dict[str, Any]:
    """Read muse_relay_config.json, merged over DEFAULT_CONFIG.

    Uses deepcopy so mutating the returned dict (e.g. writing discovered
    selectors) never corrupts the module-level DEFAULT_CONFIG.
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    cfg.update(loaded)
                    if "selectors" in loaded and isinstance(loaded["selectors"], dict):
                        cfg["selectors"] = {**DEFAULT_CONFIG["selectors"], **loaded["selectors"]}
        except Exception as e:
            print(f"Warning: could not read {CONFIG_FILE.name}: {e}", file=sys.stderr)
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Warning: failed to save {CONFIG_FILE.name}: {e}", file=sys.stderr)


def norm_key(path: str) -> str:
    """Normalized case/path key for the thread registry."""
    return os.path.normcase(os.path.normpath(path)) if path else ""


def load_thread_registry() -> Dict[str, str]:
    """Map project directory -> Muse thread URL, so each project keeps its own thread."""
    if THREAD_REGISTRY_FILE.exists():
        try:
            with open(THREAD_REGISTRY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            print(f"Warning: could not read {THREAD_REGISTRY_FILE.name}: {e}", file=sys.stderr)
    return {}


def save_thread_registry(reg: Dict[str, str]) -> None:
    try:
        with open(THREAD_REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(reg, f, indent=2)
    except Exception as e:
        print(f"Warning: failed to save {THREAD_REGISTRY_FILE.name}: {e}", file=sys.stderr)
