"""Playwright automation and the BrowserManager daemon worker.

Playwright is imported lazily so that `config`, `parser`, `prompts`, and
`protocol` remain importable (and unit-testable) without Playwright installed.
"""

from __future__ import annotations

import base64
import hashlib
import os
import queue
import re
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import (
    BUBBLE_SEP,
    DAEMON_COMPOSER_SEL,
    DAEMON_MSG_SEL,
    DAEMON_SEND_SEL,
    DEFAULT_SELECTORS,
    DEFAULT_TIMEOUT,
    MUSE_NEW_THREAD_URL,
    MUSE_HOME_URL,
    MAX_STALL_RELOADS,
    POLL_INTERVAL_S,
    SETTLE_POLLS,
    STALL_RELOAD_S,
    STATE_FILE,
    load_config,
    load_thread_registry,
    norm_key,
    save_config,
    save_thread_registry,
)
from .parser import extract_claimed_paths

# Runtime-mutable set: archive filenames already downloaded this process, so we
# do not re-download/extract the same Muse archive card twice.
_DOWNLOADED_ARCHIVES: set = set()

# Records every URL.createObjectURL() call in the page so media that Muse
# renders as blob: URLs (generated images, attachments) can be harvested and
# written to the user's workspace after each turn.
_MEDIA_CAPTURE_INIT = """
(() => {
  try {
    if (!window.__museBlobPatched) {
      window.__museBlobPatched = true;
      window.__museBlobLog = window.__museBlobLog || [];
      const orig = URL.createObjectURL.bind(URL);
      URL.createObjectURL = function (obj) {
        const u = orig(obj);
        try {
          window.__museBlobLog.push({
            url: u,
            type: (obj && obj.type) || "",
            size: (obj && obj.size) || 0,
          });
        } catch (e) {}
        return u;
      };
    }
  } catch (e) {}
})();
"""

_MEDIA_TYPE_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
}
_MIN_MEDIA_BYTES = 5000
_IMG_HASH_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".svg"}
_HASH_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache"}


def _workspace_has_content(cwd: str, digest: str) -> bool:
    """True when some existing image in the workspace has these exact bytes."""
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in _HASH_SKIP_DIRS]
        for f in files:
            if os.path.splitext(f)[1].lower() not in _IMG_HASH_EXTS:
                continue
            p = os.path.join(root, f)
            try:
                with open(p, "rb") as fh:
                    if hashlib.sha256(fh.read()).hexdigest() == digest:
                        return True
            except Exception:
                continue
    return False


def require_playwright():
    """Import playwright lazily; exit with install instructions if missing."""
    try:
        from playwright.sync_api import (
            sync_playwright,
            TimeoutError as PlaywrightTimeoutError,  # noqa: F401
        )
        return sync_playwright, PlaywrightTimeoutError
    except ImportError:
        sys.exit(
            "playwright is not installed. "
            "Run: pip install playwright && playwright install chromium"
        )


# ---------------------------------------------------------------------------
# DOM Auto-Discovery Engine
# ---------------------------------------------------------------------------

AUTO_DETECT_JS = """
(() => {
  const pick = (sels) => {
    for (const s of sels) {
      try { const el = document.querySelector(s); if (el) return s; } catch (e) {}
    }
    return "";
  };
  const composer = pick([
    'textarea[placeholder*="Message" i]',
    'textarea',
    'div[contenteditable="true"]',
    '[role="textbox"]',
  ]);
  const send_button = pick([
    'button[aria-label*="send" i]',
    'button[type="submit"]',
    'button:has(svg)',
  ]);
  const message_list = pick([
    '[role="log"]',
    'main',
    'div[class*="chat" i]',
  ]);
  const message = pick([
    '[data-role="assistant"]',
    '[data-role="user"]',
    'div[class*="message" i]',
    'article',
  ]);
  return { url: location.href, composer, send_button, message_list, message };
})()
"""


def detect_selectors(page) -> Dict[str, Any]:
    try:
        data = page.evaluate(AUTO_DETECT_JS)
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"Warning: DOM auto-detection encountered: {e}", file=sys.stderr)
    return {
        "url": page.url,
        "composer": DEFAULT_SELECTORS["composer"],
        "send_button": DEFAULT_SELECTORS["send_button"],
        "message_list": DEFAULT_SELECTORS["message_list"],
        "message": DEFAULT_SELECTORS["message"],
    }


def _fill_composer(page, composer_selector: str, text: str) -> None:
    """Robustly fill the composer element, triggering React/Next.js state updates."""
    sel = composer_selector or "textarea[placeholder='Message'], textarea"
    loc = page.locator(sel).last
    loc.wait_for(state="visible", timeout=15_000)
    loc.click()
    page.wait_for_timeout(200)

    try:
        loc.evaluate("el => { el.focus(); el.select(); }")
    except Exception:
        pass

    # For shorter text without newlines, keyboard typing directly triggers full React input state
    if len(text) < 1200 and "\n" not in text:
        try:
            page.keyboard.type(text)
        except Exception:
            page.keyboard.insert_text(text)
    else:
        try:
            page.keyboard.insert_text(text)
        except Exception:
            page.keyboard.type(text)

    # Wait for the Send button to become visible/enabled (confirming React state caught up)
    try:
        page.wait_for_selector('button[aria-label="Send"], button[aria-label*="Send" i]', timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(300)


def _click_send(page, send_btn_selector: str, composer_selector: str) -> None:
    """Click send button and ensure prompt is submitted via Enter key."""
    clicked = False
    send_candidates = [
        'button[aria-label="Send"]',
        'button[aria-label*="Send" i]',
        send_btn_selector,
    ]
    for s in send_candidates:
        if not s:
            continue
        try:
            btn = page.locator(s).last
            if btn.is_visible() and btn.is_enabled():
                print(f"[*] Clicking send button: {s}", file=sys.stderr, flush=True)
                btn.click(timeout=2000)
                clicked = True
                break
        except Exception as ex:
            print(f"[*] Send button candidate {s} failed: {ex}", file=sys.stderr, flush=True)

    # If send button wasn't clicked, or if composer still holds text, press Enter to submit
    try:
        page.wait_for_timeout(400)
        composer_loc = page.locator(composer_selector or "textarea").last
        val = composer_loc.input_value() if composer_loc.count() > 0 else ""
        if val or not clicked:
            print("[*] Submitting via Enter key...", file=sys.stderr, flush=True)
            composer_loc.press("Enter")
    except Exception:
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass

    page.wait_for_timeout(600)


def _extract_messages(page, message_selector: str) -> List[Dict[str, str]]:
    """Extract message items as list of {'author': str, 'text': str}."""
    sel = message_selector or ".hatch-chat-groupable-bubble"
    try:
        loc = page.locator(sel)
        count = loc.count()
    except Exception:
        return []

    out = []
    for i in range(count):
        el = loc.nth(i)
        try:
            # Note: Do NOT filter with is_visible() here because long chat containers
            # clip off-screen bubbles, but inner_text() remains accessible in the DOM.
            text = el.inner_text().strip()
            if not text:
                continue
            classes = el.get_attribute("class") or ""
            is_user = (
                "bg-chat-user-bubble" in classes
                or "user" in classes.lower()
                or text.lstrip().startswith("You:")
                or text.lstrip().startswith("You\n")
            )
            role = "user" if is_user else "assistant"
            clean_text = text
            if is_user:
                clean_text = re.sub(r"^You\s*:\s*", "", clean_text, flags=re.IGNORECASE).strip()
            out.append({"author": role, "text": clean_text})
        except Exception:
            continue
    return out


def _is_generating(page) -> bool:
    """Check if Muse is currently generating, thinking, or streaming."""
    try:
        stop_btn = page.locator("button[aria-label*='Stop' i], button:has-text('Stop'), [data-testid*='stop' i]")
        for i in range(stop_btn.count()):
            if stop_btn.nth(i).is_visible():
                return True
    except Exception:
        pass

    try:
        active_indicators = page.locator(
            "[role='log'] [class*='typing'], [role='log'] [class*='thinking'], "
            "[role='log'] [aria-label*='thinking' i], [role='log'] [aria-label*='loading' i], "
            ".hatch-chat-groupable-bubble [class*='animate-pulse']"
        )
        for i in range(min(active_indicators.count(), 5)):
            if active_indicators.nth(i).is_visible():
                return True
    except Exception:
        pass

    return False


def _wait_for_reply(
    page,
    message_selector: str,
    n_before: int = 0,
    timeout_s: int = DEFAULT_TIMEOUT,
    prompt_text: str = "",
    prev_texts: Optional[Set[str]] = None,
) -> str:
    """Wait for assistant response to stream and settle."""
    deadline = time.time() + timeout_s
    last_text = None
    stable_count = 0
    captured_texts: List[str] = []
    seen_generating = False

    # Assistant texts that already existed BEFORE this send. Long ones (>=300 chars)
    # are never a fresh reply for this prompt — accepting them means returning the
    # previous turn's answer (stale) and acting on it. Short acks may legitimately
    # repeat, so only long texts are treated as stale.
    prev_stale = {
        t.strip() for t in (prev_texts or set())
        if t and len(t.strip()) >= 300
    }

    # Clean target snippet from prompt_text to match in user message bubble
    target_snippet = prompt_text.strip()[:40].strip().lower() if prompt_text else ""

    print(f"[*] Waiting for Muse AI response (timeout={timeout_s}s)...", file=sys.stderr, flush=True)
    poll_count = 0
    stall_since = time.time()
    reload_count = 0
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        poll_count += 1

        # 1. Scroll window and scrollable containers to bottom so new elements render
        try:
            page.evaluate("""() => {
                const scroller = document.getElementById('hatch-chat-scroll') || document.querySelector('[class*="overflow-y-auto"]');
                if (scroller) {
                    scroller.scrollTop = scroller.scrollHeight;
                }
                window.scrollTo(0, document.body.scrollHeight);
                const scrollables = document.querySelectorAll('[role="log"], main, div[class*="chat" i], div[class*="overflow" i], div[class*="scroll" i]');
                for (const el of scrollables) {
                    if (el.scrollHeight > el.clientHeight) {
                        el.scrollTop = el.scrollHeight;
                    }
                }
            }""")
        except Exception:
            pass

        # 2. Extract current messages in the DOM
        msgs = _extract_messages(page, message_selector)
        if not msgs:
            continue

        # 3. Locate the user message for THIS prompt.
        # Tier 1: newest user bubble at/after n_before matching the prompt snippet
        #         (normal case — the just-sent bubble).
        # Tier 2: newest user bubble anywhere matching the snippet (DOM re-synced
        #         after a reload, so n_before may be too high).
        # Tier 3: newest user bubble anywhere.
        matched_user_idx = None
        user_indices = [i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("author") == "user"]
        for idx in user_indices:
            if n_before and idx < n_before:
                continue
            u_txt = msgs[idx].get("text", "").strip().lower()
            if not target_snippet or target_snippet in u_txt or u_txt[:40] in target_snippet:
                matched_user_idx = idx
                break
        if matched_user_idx is None and target_snippet:
            for idx in user_indices:
                u_txt = msgs[idx].get("text", "").strip().lower()
                if target_snippet in u_txt or u_txt[:40] in target_snippet:
                    matched_user_idx = idx
                    break
        if matched_user_idx is None and user_indices:
            matched_user_idx = user_indices[0]

        # 4. Extract assistant messages that appear AFTER the matched user message
        assistant_msgs = []
        if matched_user_idx is not None and matched_user_idx < len(msgs) - 1:
            assistant_msgs = [
                m["text"] for m in msgs[matched_user_idx + 1:]
                if m.get("author") == "assistant" or not m.get("text", "").lstrip().startswith("You:")
            ]
        elif not assistant_msgs and n_before and len(msgs) > n_before:
            new_msgs = msgs[n_before:]
            assistant_msgs = [m["text"] for m in new_msgs if m.get("author") == "assistant"]
            if not assistant_msgs and len(new_msgs) >= 1:
                # Possibly single long bubble right after n_before? treat last new as assistant
                last_new = new_msgs[-1]
                if last_new.get("author") != "user":
                    assistant_msgs = [last_new["text"]]
            if not assistant_msgs and len(new_msgs) >= 2:
                assistant_msgs = [new_msgs[-1]["text"]]

        # Drop texts that are just a replay of a previous turn's answer
        # (Muse sometimes re-renders an old bubble into the new reply slot).
        if prev_stale:
            assistant_msgs = [m for m in assistant_msgs if m.strip() not in prev_stale]

        # Stall detection: Muse's SPA sometimes keeps newly sent messages out of
        # the groupable-bubble DOM until a reload. If nothing new appears for a
        # while, reload the thread page to re-sync (bounded number of times).
        if assistant_msgs:
            stall_since = time.time()
        elif (
            time.time() - stall_since > STALL_RELOAD_S
            and reload_count < MAX_STALL_RELOADS
            and "/thread/" in (page.url or "")
        ):
            reload_count += 1
            print(
                f"[*] No new bubble for {STALL_RELOAD_S}s — reloading thread to re-sync DOM "
                f"(reload {reload_count}/{MAX_STALL_RELOADS})...",
                file=sys.stderr, flush=True,
            )
            try:
                page.reload(timeout=60_000)
                try:
                    page.wait_for_selector(message_selector or DAEMON_MSG_SEL, timeout=30_000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)
            except Exception as ex:
                print(f"[!] Thread reload failed: {ex}", file=sys.stderr, flush=True)
            # The pre-send count may be inflated by SPA ghost bubbles; after a
            # reload the DOM re-syncs to server truth, so clamp n_before to keep
            # the just-sent pair reachable.
            n_before = max(0, len(msgs) - 2)
            stall_since = time.time()

        if not assistant_msgs:
            if poll_count % 8 == 0:
                print(f"[*] Waiting for assistant bubble... (DOM bubbles={len(msgs)}, n_before={n_before}, matched_user={matched_user_idx})", file=sys.stderr, flush=True)
                try:
                    conn_elem = page.locator("text='Still sending', text='Connecting...'")
                    if conn_elem.count() > 0 and conn_elem.first.is_visible():
                        print(
                            "\n[!] ALERT: Muse session expired or disconnected ('Connecting...' / 'Still sending' detected).\n"
                            "    Your login session in muse_relay_state.json has expired.\n"
                            "    Please refresh it by running: python muse_relay.py login\n",
                            file=sys.stderr,
                            flush=True,
                        )
                except Exception:
                    pass
            continue

        cur_text = BUBBLE_SEP.join(m.strip() for m in assistant_msgs if m.strip())
        captured_texts = [cur_text]

        is_gen = _is_generating(page)
        if is_gen:
            seen_generating = True

        if poll_count % 8 == 0:
            print(f"[*] Stream settling ({poll_count * POLL_INTERVAL_S:.1f}s): bubbles={len(msgs)}, assistant_msgs={len(assistant_msgs)}, chars={len(cur_text)}, is_gen={is_gen}, seen_gen={seen_generating}, stable={stable_count}/{SETTLE_POLLS}", file=sys.stderr, flush=True)

        # 5. Settle check:
        # If Muse was actively generating and has now stopped (Stop button disappeared), and text is stable:
        if seen_generating and not is_gen and cur_text and cur_text == last_text:
            print(f"[*] Muse AI generation completed ({len(assistant_msgs)} bubble(s), {len(cur_text)} chars).", file=sys.stderr, flush=True)
            return cur_text

        # If Muse is still marked as generating, require text to remain static for 20 polls (10s) as safety timeout
        if is_gen:
            if cur_text and cur_text == last_text:
                stable_count += 1
                if stable_count >= 20:
                    print(f"[*] Muse AI stream completed (10s static threshold; {len(assistant_msgs)} bubble(s), {len(cur_text)} chars).", file=sys.stderr, flush=True)
                    return cur_text
            else:
                stable_count = 0
        elif cur_text and cur_text == last_text:
            stable_count += 1
            if stable_count >= SETTLE_POLLS:
                print(f"[*] Muse AI response complete ({len(assistant_msgs)} bubble(s), {len(cur_text)} chars).", file=sys.stderr, flush=True)
                return cur_text
        else:
            stable_count = 0

        last_text = cur_text

    print(
        "Warning: generation timed out or took longer than expected; returning captured text.",
        file=sys.stderr,
    )
    return captured_texts[-1] if captured_texts else ""


def _extract_option_labels(page, message_selector: str, start_idx: int) -> List[str]:
    """Read Muse's interactive choice buttons (option chips) from NEW assistant
    bubbles only (index > start_idx). Muse renders one full-width button per
    option (class contains 'rounded-10'/'w-full'); icon-only buttons (copy/edit)
    have empty text and are ignored."""
    sel = message_selector or ".hatch-chat-groupable-bubble"
    try:
        loc = page.locator(sel)
        count = loc.count()
    except Exception:
        return []

    labels: List[str] = []
    for i in range(start_idx, count):
        el = loc.nth(i)
        try:
            classes = el.get_attribute("class") or ""
            if "bg-chat-user-bubble" in classes:
                continue
            for b in el.locator("button").all():
                try:
                    txt = (b.inner_text() or "").strip()
                    if not txt or len(txt) > 120:
                        continue
                    b_cls = b.get_attribute("class") or ""
                    if "rounded-10" in b_cls or "w-full" in b_cls:
                        if txt not in labels:
                            labels.append(txt)
                except Exception:
                    continue
        except Exception:
            continue
    return labels


def _launch_browser_and_context(
    p,
    cfg: Dict[str, Any],
    headless: bool = True,
    with_storage: bool = True,
):
    """Launch browser with built-in location spoofing (mimicking Location Guard), proxy, and optional CDP."""
    cdp_url = cfg.get("cdp_url")
    if cdp_url:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            sys.exit(
                f"\n[Error] Could not connect to Chrome at {cdp_url} ({e}).\n\n"
                f"Why did this happen?\n"
                f"  Chrome was not running with remote debugging enabled.\n\n"
                f"How to fix this:\n"
                f"  Option 1: If you have a desktop VPN app running (Nord, Proton, etc.),\n"
                f"            you DO NOT need --cdp! Just run:\n"
                f"                python muse_relay.py login\n\n"
                f"  Option 2: If your VPN is a Chrome extension, launch Chrome with debugging:\n"
                f"                python muse_relay.py launch-chrome\n"
                f"            Then in another terminal run:\n"
                f"                python muse_relay.py login --cdp http://127.0.0.1:9222\n"
            )
        contexts = browser.contexts
        ctx = contexts[0] if contexts else browser.new_context()
        try:
            ctx.add_init_script(_MEDIA_CAPTURE_INIT)
        except Exception:
            pass
        return browser, ctx

    launch_kwargs: Dict[str, Any] = {"headless": headless}
    proxy = cfg.get("proxy")
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}

    browser = p.chromium.launch(**launch_kwargs)

    # Location spoofing matching Location Guard + US Locale/Timezone
    ctx_kwargs: Dict[str, Any] = {
        "geolocation": cfg.get("geolocation", {"latitude": 40.7128, "longitude": -74.0060}),
        "permissions": ["geolocation"],
        "locale": cfg.get("locale", "en-US"),
        "timezone_id": cfg.get("timezone_id", "America/New_York"),
        "accept_downloads": True,
    }
    if with_storage and STATE_FILE.exists():
        ctx_kwargs["storage_state"] = str(STATE_FILE)

    ctx = browser.new_context(**ctx_kwargs)
    try:
        ctx.add_init_script(_MEDIA_CAPTURE_INIT)
    except Exception:
        pass
    return browser, ctx


# ---------------------------------------------------------------------------
# Archive download + extraction
# ---------------------------------------------------------------------------

def _extract_archive(archive_path: Path, cwd: str) -> List[str]:
    """Safely extract tar.gz or zip archive into target directory, flattening single top-level folder if present."""
    target_dir = Path(cwd)
    target_dir.mkdir(parents=True, exist_ok=True)
    extracted_names: List[str] = []

    # 1. TAR Archive (.tar.gz, .tgz, .tar)
    if tarfile.is_tarfile(str(archive_path)):
        with tarfile.open(str(archive_path), "r:*") as tar:
            members = tar.getmembers()
            prefixes = {m.name.split("/")[0] for m in members if "/" in m.name}
            single_root = list(prefixes)[0] if len(prefixes) == 1 else None

            for member in members:
                orig_name = member.name
                if single_root and (orig_name == single_root or orig_name.startswith(single_root + "/")):
                    parts = orig_name.split("/")
                    if len(parts) > 1:
                        member.name = "/".join(parts[1:])
                    else:
                        continue
                if member.name:
                    try:
                        tar.extract(member, path=str(target_dir), filter=getattr(tarfile, "data_filter", None))
                    except TypeError:
                        tar.extract(member, path=str(target_dir))
                    if not member.isdir():
                        extracted_names.append(member.name)

    # 2. ZIP Archive (.zip)
    elif zipfile.is_zipfile(str(archive_path)):
        with zipfile.ZipFile(str(archive_path), "r") as zf:
            infolist = zf.infolist()
            prefixes = {info.filename.split("/")[0] for info in infolist if "/" in info.filename}
            single_root = list(prefixes)[0] if len(prefixes) == 1 else None

            for info in infolist:
                orig_name = info.filename
                dest_name = orig_name
                if single_root and orig_name.startswith(single_root + "/"):
                    dest_name = orig_name[len(single_root) + 1:]
                if dest_name and not info.is_dir():
                    dest_path = target_dir / dest_name
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(dest_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted_names.append(dest_name)

    return extracted_names


def _download_and_extract_recent_archives(page, cwd: str, n_before: int = 0) -> List[str]:
    """Detect downloadable archive cards attached by Muse in the current turn, download and extract to cwd."""
    if not cwd or not os.path.isdir(cwd):
        return []

    extracted: List[str] = []
    try:
        msg_sel = ".hatch-chat-groupable-bubble"
        all_bubbles = page.locator(msg_sel)
        total_bubbles = all_bubbles.count()
        if total_bubbles == 0:
            return []

        # Only look inside newly generated bubbles from this turn
        start_idx = max(0, n_before) if n_before < total_bubbles else max(0, total_bubbles - 1)
        target_bubbles = [all_bubbles.nth(i) for i in range(start_idx, total_bubbles)]

        options_btn = None
        for b in reversed(target_bubbles):
            btns = b.locator("[data-testid='hatch-sandbox-file-card-options']")
            if btns.count() > 0 and btns.last.is_visible():
                options_btn = btns.last
                break

        if not options_btn:
            return []

        card_container = options_btn.locator("xpath=./ancestor::span[contains(@class, 'rounded') or contains(@class, 'hatch-chat-groupable-bubble')][1]")
        card_text = card_container.inner_text().lower() if card_container.count() > 0 else ""
        if not any(ext in card_text for ext in (".tar", ".zip", ".tgz", ".gz", "archive")):
            return []

        if any(arch in card_text for arch in _DOWNLOADED_ARCHIVES):
            return []

        print("[*] Detected downloadable project archive in Muse chat; triggering download...", file=sys.stderr, flush=True)
        options_btn.click()
        page.wait_for_timeout(600)

        download_item = page.locator(
            "[role='menuitem']:has-text('Download'), [data-slot='dropdown-menu-item']:has-text('Download'), button:has-text('Download')"
        ).first
        if not download_item.is_visible():
            return []

        tmp_dir = Path(tempfile.gettempdir()) / "muse_downloads"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        archive_path = tmp_dir / f"project_archive_{int(time.time())}.tar.gz"

        with page.expect_download(timeout=30000) as dl_info:
            download_item.click()
        dl = dl_info.value
        suggested_name = dl.suggested_filename
        if suggested_name:
            archive_path = tmp_dir / suggested_name
        dl.save_as(str(archive_path))
        _DOWNLOADED_ARCHIVES.add(archive_path.name.lower())
        if suggested_name:
            _DOWNLOADED_ARCHIVES.add(suggested_name.lower())
        print(f"[*] Successfully downloaded archive: {archive_path.name} ({archive_path.stat().st_size} bytes)", file=sys.stderr, flush=True)

        extracted = _extract_archive(archive_path, cwd)
        print(f"[*] Extracted {len(extracted)} files directly into {cwd}", file=sys.stderr, flush=True)
    except Exception as ex:
        print(f"[!] Archive download/extraction encountered: {ex}", file=sys.stderr, flush=True)

    return extracted


def _new_bubbles_show_images(page, message_selector: str, start_idx: int) -> bool:
    """True when bubbles created this turn contain an image node / image skeleton.
    Used to avoid harvesting blobs that only exist because scrolling lazy-loaded
    OLD page media (avatars, banners, history images)."""
    sel = message_selector or ".hatch-chat-groupable-bubble"
    try:
        loc = page.locator(sel)
        count = loc.count()
    except Exception:
        return False
    for i in range(start_idx, count):
        el = loc.nth(i)
        try:
            classes = el.get_attribute("class") or ""
            if "bg-chat-user-bubble" in classes:
                continue
            if el.locator("img, [data-slot*='image' i], [data-slot*='skeleton' i]").count() > 0:
                return True
        except Exception:
            continue
    return False


def _harvest_new_media(
    page,
    cwd: str,
    blob_index: int,
    reply_text: str = "",
    message_selector: str = "",
    start_idx: int = 0,
) -> List[str]:
    """Fetch media blobs created since ``blob_index`` and save them into ``cwd``.

    Muse renders generated images/attachments via URL.createObjectURL() blob:
    URLs; the <img> nodes are often missing or stuck on skeletons, so the blob
    registry is the reliable source. Filenames come from paths Muse claimed in
    its reply (in order); leftovers get ``muse-image-N.<ext>``.
    Never overwrites an existing file.

    Gate: only harvest when this turn actually shows images (image nodes in the
    new bubbles) or the reply claims image files — otherwise lazy-loaded OLD
    page media (which also creates new blob entries while scrolling) would be
    saved as fake "new" files. Content identical to a file already in the
    workspace is skipped as well.
    """
    if not cwd or not os.path.isdir(cwd):
        return []
    # A mid-turn page reload resets window.__museBlobLog; then the pre-send index
    # is meaningless, so fall back to scanning the (re-populated) full log.
    try:
        total = page.evaluate("() => (window.__museBlobLog || []).length")
    except Exception:
        total = 0
    start = blob_index if 0 <= blob_index <= total else 0
    try:
        entries = page.evaluate(
            "i => (window.__museBlobLog || []).slice(i)", start
        )
    except Exception:
        return []

    media = [
        e for e in (entries or [])
        if str(e.get("type", "")).startswith("image/")
        and int(e.get("size") or 0) >= _MIN_MEDIA_BYTES
        and e.get("url")
    ]
    if not media:
        return []

    image_exts = set(_MEDIA_TYPE_EXT.values())
    claimed_imgs = [
        cp for cp in extract_claimed_paths(reply_text, cwd)
        if cp.rsplit(".", 1)[-1].lower() in image_exts
    ]
    if not claimed_imgs and not _new_bubbles_show_images(page, message_selector, start_idx):
        print(
            "[*] Media blob harvest skipped: no image in this turn's bubbles and no image claims in reply.",
            file=sys.stderr, flush=True,
        )
        return []

    # Claimed image paths from this reply that don't exist yet, in textual order.
    target_paths: List[Path] = []
    for cp in claimed_imgs:
        ext = cp.rsplit(".", 1)[-1].lower() if "." in cp else ""
        if ext in image_exts and not os.path.exists(cp):
            target_paths.append(Path(cp))

    try:
        b64_list = page.evaluate(
            """async (urls) => {
                const out = [];
                for (const u of urls) {
                    try {
                        const r = await fetch(u);
                        const buf = await r.arrayBuffer();
                        const bytes = new Uint8Array(buf);
                        let bin = "";
                        const chunk = 0x8000;
                        for (let i = 0; i < bytes.length; i += chunk) {
                            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                        }
                        out.push(btoa(bin));
                    } catch (e) { out.push(null); }
                }
                return out;
            }""",
            [e["url"] for e in media],
        )
    except Exception as ex:
        print(f"[!] Media blob harvest failed: {ex}", file=sys.stderr, flush=True)
        return []

    saved: List[str] = []
    for idx, b64 in enumerate(b64_list or []):
        if not b64:
            continue
        try:
            data = base64.b64decode(b64)
        except Exception:
            continue
        if len(data) < _MIN_MEDIA_BYTES:
            continue
        mtype = str(media[idx].get("type", "")).lower()
        ext = _MEDIA_TYPE_EXT.get(mtype, "png")

        if idx < len(target_paths):
            dest = target_paths[idx]
            if dest.exists():
                continue
        else:
            dest = Path(cwd) / f"muse-image-{idx + 1}.{ext}"
            n = 2
            while dest.exists():
                dest = Path(cwd) / f"muse-image-{idx + 1}-{n}.{ext}"
                n += 1
        if _workspace_has_content(cwd, hashlib.sha256(data).hexdigest()):
            print(
                "[*] Media blob skipped: identical image already exists in workspace.",
                file=sys.stderr, flush=True,
            )
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except Exception as ex:
            print(f"[!] Could not save media to {dest}: {ex}", file=sys.stderr, flush=True)
            continue
        saved.append(str(dest))
        print(f"[*] Saved media from Muse chat: {dest} ({len(data)} bytes)", file=sys.stderr, flush=True)

    return saved


def _switch_muse_thread_for_project(page, cwd: str, composer_sel: str) -> str:
    """Navigate to the Muse thread registered for this project, or start a fresh one."""
    reg = load_thread_registry()
    key = norm_key(cwd)
    url = reg.get(key) or MUSE_NEW_THREAD_URL
    print(f"[Daemon] Project changed to `{cwd}`; switching Muse thread -> {url}", file=sys.stderr, flush=True)
    page.goto(url, timeout=60_000)
    try:
        page.wait_for_selector(composer_sel, timeout=30_000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    return page.url


class BrowserManager:
    """Manages Playwright on a dedicated worker thread for thread-safe HTTP request handling."""

    def __init__(self, chat_url: str, headless: bool = True):
        self.chat_url = chat_url
        self.headless = headless
        self.task_queue: queue.Queue = queue.Queue()
        self.ready_event = threading.Event()
        self.lock = threading.Lock()
        self.url = ""
        self.active_cwd = ""
        self.error: Optional[Exception] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready_event.wait(timeout=60)
        if self.error:
            raise self.error

    def _run(self) -> None:
        sync_playwright, _ = require_playwright()
        cfg = load_config()
        selectors = cfg.get("selectors", {})
        composer_sel = selectors.get("composer") or DAEMON_COMPOSER_SEL
        send_sel = selectors.get("send_button") or DAEMON_SEND_SEL
        msg_sel = selectors.get("message") or DAEMON_MSG_SEL

        try:
            with sync_playwright() as p:
                browser, ctx = _launch_browser_and_context(
                    p, cfg, headless=self.headless, with_storage=True
                )
                page = ctx.new_page()
                print(f"[Daemon] Opening chat at {self.chat_url}...")
                page.goto(self.chat_url, timeout=60_000)
                try:
                    page.wait_for_selector(composer_sel, timeout=30_000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)

                self.url = page.url
                self.ready_event.set()
                print("[Daemon] Browser warm and ready for requests.")

                while True:
                    task = self.task_queue.get()
                    action = task[0]
                    if action == "stop":
                        break
                    elif action == "send":
                        _, text, timeout_s, res_q, req_cwd = task
                        try:
                            # Auto-switch Muse thread when the request comes from a different project.
                            if req_cwd and os.path.isdir(req_cwd) and norm_key(req_cwd) != norm_key(self.active_cwd):
                                self.url = _switch_muse_thread_for_project(page, req_cwd, composer_sel)
                                self.chat_url = self.url
                                self.active_cwd = req_cwd
                            try:
                                page.wait_for_selector(msg_sel, timeout=5000)
                            except Exception:
                                pass
                            page.wait_for_timeout(1000)
                            msgs_before = _extract_messages(page, msg_sel)
                            n_before = len(msgs_before)
                            prev_texts = {
                                m["text"].strip()
                                for m in msgs_before
                                if m.get("author") == "assistant" and m.get("text", "").strip()
                            }
                            try:
                                blob_index = page.evaluate(
                                    "() => (window.__museBlobLog || []).length"
                                )
                            except Exception:
                                blob_index = 0
                            print(f"[*] Sending prompt to Muse: {text[:120]!r}", file=sys.stderr, flush=True)
                            _fill_composer(page, composer_sel, text)
                            _click_send(page, send_sel, composer_sel)
                            reply = _wait_for_reply(
                                page, msg_sel, n_before, timeout_s=timeout_s,
                                prompt_text=text, prev_texts=prev_texts,
                            )
                            option_labels = _extract_option_labels(page, msg_sel, start_idx=n_before + 1)
                            if option_labels:
                                listed = "\n".join(f"{i + 1}. {lbl}" for i, lbl in enumerate(option_labels))
                                reply = (
                                    f"{reply.rstrip()}\n\n"
                                    f"[MUSE OFFERED CHOICES — NOTHING HAS BEEN EXECUTED]\n"
                                    f"{listed}\n"
                                    f"Show these options to the user and wait for their answer."
                                )
                                print(f"[*] Muse offered {len(option_labels)} choice(s): {option_labels}", file=sys.stderr, flush=True)
                            extracted = _download_and_extract_recent_archives(page, req_cwd, n_before=n_before)
                            media_saved = _harvest_new_media(
                                page, req_cwd, blob_index, reply,
                                message_selector=msg_sel, start_idx=n_before,
                            )
                            if media_saved:
                                extracted = list(extracted) + media_saved
                            # Remember the resolved thread URL for this project.
                            if req_cwd:
                                self.url = page.url
                                self.chat_url = page.url
                                self.active_cwd = req_cwd
                                reg = load_thread_registry()
                                reg[norm_key(req_cwd)] = page.url
                                save_thread_registry(reg)
                            res_q.put(("ok", (reply, extracted)))
                        except Exception as ex:
                            res_q.put(("error", str(ex)))
                    elif action == "read":
                        _, limit, res_q = task
                        try:
                            msgs = _extract_messages(page, msg_sel)
                            res_q.put(("ok", msgs[-limit:]))
                        except Exception as ex:
                            res_q.put(("error", str(ex)))
                    elif action == "navigate":
                        _, new_url, res_q = task
                        try:
                            print(f"[Daemon] Navigating to: {new_url}", file=sys.stderr, flush=True)
                            page.goto(new_url, timeout=60_000)
                            try:
                                page.wait_for_selector(composer_sel, timeout=30_000)
                            except Exception:
                                pass
                            page.wait_for_timeout(2000)
                            self.url = page.url
                            self.chat_url = page.url
                            self.active_cwd = ""
                            cfg["chat_url"] = self.url
                            save_config(cfg)
                            print(f"[Daemon] Successfully navigated to: {self.url}", file=sys.stderr, flush=True)
                            res_q.put(("ok", self.url))
                        except Exception as ex:
                            res_q.put(("error", str(ex)))

                browser.close()
        except Exception as e:
            self.error = e
            self.ready_event.set()

    def stop(self) -> None:
        self.task_queue.put(("stop",))

    def clear_active_thread(self) -> None:
        """Forget the current project's thread so the next send starts fresh."""
        cwd = self.active_cwd
        self.active_cwd = ""
        if cwd:
            reg = load_thread_registry()
            reg.pop(norm_key(cwd), None)
            save_thread_registry(reg)

    def navigate(self, new_url: str) -> str:
        with self.lock:
            res_q = queue.Queue()
            self.task_queue.put(("navigate", new_url, res_q))
            try:
                status, val = res_q.get(timeout=60)
            except queue.Empty:
                raise RuntimeError("Muse navigation timed out.")
            if status == "error":
                raise RuntimeError(val)
            return val

    def send_prompt(self, text: str, timeout_s: int = DEFAULT_TIMEOUT, cwd: str = "") -> Tuple[str, List[str]]:
        with self.lock:
            res_q = queue.Queue()
            self.task_queue.put(("send", text, timeout_s, res_q, cwd))
            try:
                status, val = res_q.get(timeout=timeout_s + 30)
            except queue.Empty:
                raise RuntimeError(f"Muse prompt timed out after {timeout_s}s waiting for response.")
            if status == "error":
                raise RuntimeError(val)
            return val

    def get_messages(self, limit: int = 10) -> List[Dict[str, str]]:
        with self.lock:
            res_q = queue.Queue()
            self.task_queue.put(("read", limit, res_q))
            try:
                status, val = res_q.get(timeout=30)
            except queue.Empty:
                raise RuntimeError("Muse get_messages timed out.")
            if status == "error":
                raise RuntimeError(val)
            return val
