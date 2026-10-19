"""Reply parsing, tool-call detection, and local filesystem intent handling.

Everything here is pure stdlib (re/os/json) so it can be imported and unit
tested without Playwright installed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from .config import BUBBLE_SEP


def strip_system_tags(text: str) -> str:
    """Remove <system-reminder>...</system-reminder> and unwrap <pasted_content> tags injected by Claude Code."""
    cleaned = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"<pasted_content[^>]*>(.*?)</pasted_content[^>]*>", r"\1", cleaned, flags=re.DOTALL)
    return cleaned.strip()


def is_title_generation_query(prompt: str) -> bool:
    """Detect if prompt is an automated session title generation query (Claude Code, Continue, etc.)."""
    p_lower = prompt.lower()
    return bool(
        "<session>" in p_lower
        or "write the title in the predominant language" in p_lower
        or ("session title" in p_lower and "title:" in p_lower)
        or re.search(r"\b(?:generate|write|create)\s+(?:a\s+)?(?:short\s+)?(?:session\s+)?title\b", p_lower)
    )


def generate_local_session_title(text: str) -> str:
    """Generate a clean, concise 3-5 word title locally from Claude Code's session prompt."""
    m = re.search(r"<session>(.*?)</session>", text, flags=re.DOTALL)
    raw = m.group(1).strip() if m else text.strip()
    raw = re.sub(r"^(?:please\s+|can\s+you\s+|could\s+you\s+|i\s+want\s+to\s+)", "", raw, flags=re.IGNORECASE)
    m_fn = re.search(r"\b([a-zA-Z0-9_\-]+\.[a-zA-Z0-9_]{1,6})\b", raw)
    words = [w for w in raw.split() if w]
    if len(words) <= 5:
        title = " ".join(words)
    elif m_fn:
        title = f"{words[0].capitalize()} {m_fn.group(1)}"
    else:
        title = " ".join(words[:5])
    title = title.strip()[:50]
    return title[0].upper() + title[1:] if title else "Coding Session"


def is_dump_all_contents_query(text: str) -> bool:
    """Check if user prompt asks to output/read the contents of existing local files in the current folder."""
    t = text.strip().lower()
    # If the user is asking to create, transfer, save, generate, or build, it is NOT a local dump!
    if re.search(r"\b(?:create|make|write|generate|build|transfer|save|disk|workspace|implement|develop|solve|coding|codeblocks|code\s*block)\b", t):
        return False
    if re.search(r"\b(?:output|print|show|display|dump|read|cat|give\s+me|get)\b.*\b(?:contents?|text|code|body)\b.*\b(?:all\s+)?(?:files?|folder|directory|dir|project|here)\b", t):
        return True
    if re.search(r"\b(?:contents?|what(?:'s|\s+is)\s+(?:in|inside))\b.*\b(?:all\s+)?(?:files?|folder|directory|here)\b", t):
        return True
    if re.search(r"\b(?:read|cat|dump|output|print|show)\s+(?:all\s+)?(?:the\s+)?files?\s+(?:in\s+)?(?:this|the|current)?\s*(?:folder|directory|dir|here)?\b", t):
        if any(w in t for w in ("content", "contents", "inside", "text", "cat", "dump", "read")):
            return True
    return False


def is_folder_analysis_query(text: str) -> bool:
    """Check if user prompt asks a question or analysis about the files in the project folder."""
    t = text.strip().lower()
    # If the user is asking to create, transfer, save, generate, or build, it is NOT an analysis of existing local files!
    if re.search(r"\b(?:create|make|write|generate|build|transfer|save|disk|workspace|implement|develop)\b", t):
        return False
    if re.search(r"\b(?:all\s+)?(?:the\s+)?files?\b.*\b(?:this|the|current)?\s*(?:folder|directory|dir|project|repo|codebase|workspace|here)\b", t):
        return True
    if re.search(r"\b(?:this|the|current)?\s*(?:folder|directory|dir|project|repo|codebase|workspace|here)\b.*\b(?:all\s+)?(?:the\s+)?files?\b", t):
        return True
    return False


def is_delete_all_files_query(text: str) -> bool:
    """Check if the prompt requests deleting all files in the current folder."""
    t = text.strip().lower()
    # E.g. "clear the directory", "empty the folder", "dọn sạch thư mục", "clean the folder"
    if re.search(r"\b(?:clear|clean|wipe|empty|don|xoa|xoá|xóa|dọn)\b(?:\s+(?:sach|sạch))?\s+(?:out\s+)?(?:all\s+)?(?:the|this|các|tất cả)?\s*(?:folder|directory|dir|here|thư mục|tệp|file)\b", t):
        return True
    # E.g. "delete/remove all/the files in the folder/directory"
    if re.search(r"\b(?:delete|remove|clear|clean|wipe|erase|empty|xoa|xoá|xóa|dọn|don)\b.*\b(?:all\s+)?(?:the\s+)?(?:files|everything|tệp|file|tất cả|hết|het)\b", t):
        return True
    if re.search(r"\b(?:delete|remove|xoa|xoá|xóa|dọn)\b\s+(?:all\s+)?(?:the\s+)?(?:files|tệp|file|tất cả(?:\s+các)?\s+(?:file|tệp))\b", t):
        return True
    if t in (
        "delete files", "delete all files", "clean folder", "clear folder", "empty folder",
        "delete the files in the folder", "delete the files in this folder",
        "xóa các file", "xoá các file", "xóa hết", "xóa tất cả file", "xoá tất cả tệp",
        "dọn sạch thư mục", "xóa sạch thư mục", "xóa hết file", "xoa het", "xoa cac file", "don sach thu muc",
        "clear the directory", "empty the directory", "empty directory", "clear directory"
    ):
        return True
    return False


def detect_delete_intent(text: str, cwd: str, tool_names: set) -> Optional[Tuple[str, str, dict]]:
    """
    Detect if the user prompt requests deleting local files in cwd.
    Returns: (thought, tool_name, tool_input) or None.
    """
    t = text.strip().lower()
    cmd_tool = "PowerShell" if "PowerShell" in tool_names else ("Bash" if "Bash" in tool_names else None)
    if not cmd_tool:
        return None

    # 1. Delete all files in current folder / directory
    if is_delete_all_files_query(t):
        existing = []
        try:
            for item in os.listdir(cwd):
                if not item.startswith("."):
                    existing.append(item)
        except Exception:
            pass

        if existing:
            if cmd_tool == "PowerShell":
                paths_str = ", ".join(f"'{os.path.join(cwd, f)}'" for f in existing)
                cmd = f"Remove-Item -Path {paths_str} -Recurse -Force"
            else:
                paths_str = " ".join(f"'{os.path.join(cwd, f)}'" for f in existing)
                cmd = f"rm -rf {paths_str}"
            thought = f"Deleting files in `{cwd}` ({', '.join(existing)})."
            return thought, cmd_tool, {"command": cmd}
        else:
            return None

    # 2. Delete specific file(s) mentioned in prompt
    is_delete_verb = bool(re.search(r"\b(?:delete|remove|rm|erase|del|unlink|xoa|xoá|xóa|dọn|don)\b", t))
    if is_delete_verb:
        existing_names = []
        try:
            for item in os.listdir(cwd):
                if not item.startswith("."):
                    existing_names.append(item)
        except Exception:
            pass

        file_candidates = re.findall(r"\b([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)\b", text)
        for item in existing_names:
            if item not in file_candidates:
                if re.search(rf"\b{re.escape(item)}\b", text, re.IGNORECASE):
                    file_candidates.append(item)

        valid_files_to_delete = []
        for fc in file_candidates:
            full = os.path.normpath(os.path.join(cwd, fc))
            if (os.path.isfile(full) or os.path.isdir(full)) and (fc, full) not in valid_files_to_delete:
                valid_files_to_delete.append((fc, full))

        if valid_files_to_delete:
            if cmd_tool == "PowerShell":
                paths_str = ", ".join(f"'{p}'" for _, p in valid_files_to_delete)
                cmd = f"Remove-Item -Path {paths_str} -Recurse -Force"
            else:
                paths_str = " ".join(f"'{p}'" for _, p in valid_files_to_delete)
                cmd = f"rm -rf {paths_str}"
            names_str = ", ".join(f"`{f}`" for f, _ in valid_files_to_delete)
            thought = f"Deleting {names_str} on your computer."
            return thought, cmd_tool, {"command": cmd}

    return None


def is_file_list_query(p: str) -> bool:
    """Check if the user prompt is asking to inspect or list files in the current folder."""
    p_lower = p.strip().lower()
    if p_lower in ("ls", "dir", "get-childitem", "pwd"):
        return True
    # If the user is asking to create, make, write, generate, transfer, save, delete, etc.
    if re.search(r"\b(?:create|make|write|generate|add|edit|modify|update|delete|remove|clear|clean|wipe|erase|transfer|save|disk|workspace|build|develop|implement|tạo|viết|thêm|sửa|xóa|xoá|xoa|dọn|don|cập nhật|chạy)\b", p_lower):
        return False
    # If the user is asking for file contents/code, it's a content query, not a pure listing query
    if any(w in p_lower for w in ("content", "contents", "inside", "code", "text", "body")):
        return False
    # If the user is asking to explain, analyze, or understand what files do, it's an analysis query
    if re.search(r"\b(?:explain|analyze|summarize|describe|what\s+do|how\s+do|purpose|meaning|mean|do\s+the\s+files\s+do)\b", p_lower):
        return False
    if re.search(r"\b(?:what|show|list|see|tell me|display|check|find|view|output)\b.*\b(?:files|folders|directory contents|workspace contents)\b", p_lower):
        return True
    if re.search(r"\b(?:what files|which files|list files|show files)\b", p_lower):
        return True
    # Vietnamese listing: "liệt kê file", "xem danh sách tệp", "cho tôi xem các file"
    if re.search(r"\b(?:liệt kê|liet ke|xem|hiển thị|hien thi|danh sách|danh sach|cho (?:tôi|toi) (?:xem|biet|biết))\b.*\b(?:file|tệp|thư mục|folder)\b", p_lower):
        return True
    return False


def _detect_intent_tool_call(user_msg: str, tool_names: set, cwd: str) -> Optional[Tuple[str, str, dict]]:
    """
    Detect if the user prompt directly requests a local filesystem action that requires
    querying the user's computer rather than Muse's remote cloud container.
    """
    clean_msg = strip_system_tags(user_msg)

    # 0. Delete / remove files on local computer
    del_intent = detect_delete_intent(clean_msg, cwd, tool_names)
    if del_intent:
        return del_intent

    # 1. File listing / workspace inspection
    if is_file_list_query(clean_msg):
        if "Glob" in tool_names:
            thought = "Checking the files in your current working directory on your computer."
            return thought, "Glob", {"pattern": "*"}
        elif "PowerShell" in tool_names:
            return "Listing files in your current directory on your computer.", "PowerShell", {"command": "Get-ChildItem -Name"}
        elif "Bash" in tool_names:
            return "Listing files in your current directory on your computer.", "Bash", {"command": "ls"}

    # 2. Read / inspect of a specific file on the user's computer
    m_read = re.search(
        r"\b(?:read|cat|view|inspect|check|open|show(?:\s+me)?|what(?:'s|\s+is)\s+(?:in|inside)|đọc|doc|xem|mở|mo)\s+(?:the\s+)?(?:contents?\s+of\s+)?(?:nội dung\s+|noi dung\s+)?(?:file\s+|tệp\s+|tep\s+)?`?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)`?",
        clean_msg,
        flags=re.IGNORECASE,
    )
    if m_read and ("Read" in tool_names or "FileRead" in tool_names):
        fname = m_read.group(1).strip()
        abs_path = fname if os.path.isabs(fname) else os.path.normpath(os.path.join(cwd, fname))
        t_name = "Read" if "Read" in tool_names else "FileRead"
        return f"Reading `{fname}` on your computer.", t_name, {"file_path": abs_path}

    # 3. Direct execution of a command
    m_run = re.search(
        r"^(?:(?:run|execute|chay|chạy|thực thi|thuc thi)\s+(?:(?:the\s+)?command\s+|lệnh\s+)?)?(?:`([^`]+)`|\"([^\"]+)\"|((?:python|python3|py|git|pytest|npm|node|cargo|go|gcc|g\+\+|dotnet|make|pip)\s+[^\n\r]+|pytest))\s*$",
        clean_msg,
        flags=re.IGNORECASE,
    )
    if m_run and ("PowerShell" in tool_names or "Bash" in tool_names):
        cmd = (m_run.group(1) or m_run.group(2) or m_run.group(3)).strip()
        t_name = "PowerShell" if "PowerShell" in tool_names else "Bash"
        return f"Executing `{cmd}` on your computer.", t_name, {"command": cmd}

    return None


def clean_file_list(raw_output: str) -> List[str]:
    """Parse and clean file listing output, removing internal .git and __pycache__ entries."""
    cleaned = raw_output.strip()
    files = []
    if cleaned.startswith("[") and cleaned.endswith("]"):
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                files = [str(f) for f in parsed]
        except Exception:
            pass
    if not files:
        lines = [line.strip().strip("'\"[],") for line in cleaned.splitlines()]
        files = [l for l in lines if l]

    filtered = []
    for f in files:
        norm = f.replace("/", "\\")
        if norm.startswith(".git\\") or norm == ".git" or "\\.git\\" in norm or norm.startswith(".git/"):
            continue
        if norm.startswith("__pycache__\\") or norm == "__pycache__" or "\\__pycache__\\" in norm or norm.startswith("__pycache__/"):
            continue
        filtered.append(f)
    return sorted(set(filtered))


def format_file_list(files: List[str], cwd: str) -> str:
    """Format file listing output cleanly."""
    if not files:
        return f"No files were found in `{cwd}`."
    file_items = "\n".join(f"- `{f}`" for f in sorted(files))
    return f"Here are the files in your current folder (`{cwd}`):\n\n{file_items}"


def is_pure_listing_query(text: str) -> bool:
    """Check if the user literally only wants a directory listing (ls/dir), not analysis/answering a question."""
    t = text.strip().lower()
    if t in ("ls", "dir", "get-childitem", "pwd"):
        return True
    if re.search(r"\b(?:contain|related|login|credential|secret|token|personal|do|does|why|how|which|search|find|mean|explain|breakdown|responsible|handle|used for)\b", t):
        return False
    if re.search(r"\b(?:what|show|list|see|tell me|display)\b.*\b(?:files|folders|directory|folder)\b", t):
        return True
    return False


def find_referenced_local_files(text: str, cwd: str) -> List[Tuple[str, str, str]]:
    """
    Find files mentioned in text that exist on disk in cwd.
    Returns: list of (filename, abs_path, content)
    """
    candidates = re.findall(r"\b([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)\b", text)
    found = []
    seen = set()
    for c in candidates:
        rel = os.path.basename(c)
        if rel in seen:
            continue
        full_path = os.path.normpath(os.path.join(cwd, c))
        if os.path.isfile(full_path):
            seen.add(rel)
            try:
                if os.path.getsize(full_path) < 150_000:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        found.append((rel, full_path, f.read()))
            except Exception:
                pass

    if found:
        return found

    # Fallback: If no explicit filename was matched, but user refers to local files / website / project,
    # extract distinctive terms (e.g. capitalized brand names, quoted strings) and search cwd
    t_lower = text.lower()
    if any(k in t_lower for k in ("local file", "local files", "website", "site", "web", "html", "top left", "header", "navbar", "page", "browser")):
        keywords = []
        for m in re.finditer(r'["\']([^"\']+)["\']', text):
            keywords.append(m.group(1).strip())
        for m in re.finditer(r'\b([A-Z][a-zA-Z0-9_]{2,})\b', text):
            w = m.group(1).strip()
            if w.lower() not in ("from", "what", "where", "when", "here", "there", "please", "banana", "this", "that", "with", "have"):
                keywords.append(w)

        search_exts = {".html", ".htm", ".js", ".css", ".ts", ".jsx", ".tsx", ".json", ".py"}
        cand_matches = []
        for root, dirs, files in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", ".next", "__pycache__", "dist", "build")]
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext in search_exts:
                    fpath = os.path.join(root, fn)
                    try:
                        if os.path.getsize(fpath) < 150_000:
                            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                                content = f.read()
                                for kw in keywords:
                                    if kw and kw in content:
                                        rel_path = os.path.relpath(fpath, cwd)
                                        if rel_path not in seen:
                                            seen.add(rel_path)
                                            # Prioritize html files for UI / website prompts
                                            priority = 0 if ext in (".html", ".htm") else 1
                                            cand_matches.append((priority, rel_path, fpath, content))
                                        break
                    except Exception:
                        pass
            if len(cand_matches) >= 5:
                break

        cand_matches.sort(key=lambda x: (x[0], len(x[1])))
        for _, rel_path, fpath, content in cand_matches[:3]:
            found.append((rel_path, fpath, content))

    return found


LANG_EXT_MAP: Dict[str, set] = {
    "js": {"js", "javascript"},
    "ts": {"ts", "typescript"},
    "jsx": {"jsx", "javascript"},
    "tsx": {"tsx", "typescript"},
    "py": {"py", "python"},
    "txt": {"txt", "text"},
    "html": {"html"},
    "css": {"css"},
    "json": {"json"},
    "md": {"md", "markdown"},
    "sh": {"sh", "bash"},
    "bat": {"bat", "batch"},
    "ps1": {"ps1", "powershell"},
    "yaml": {"yaml", "yml"},
    "yml": {"yaml", "yml"},
    "sql": {"sql"},
    "c": {"c"},
    "cpp": {"cpp", "c++"},
    "cs": {"cs", "c#"},
    "rs": {"rs", "rust"},
    "go": {"go", "golang"},
    "java": {"java"},
}

ALL_LANG_TAGS: set = set(LANG_EXT_MAP.keys()) | {alias for aliases in LANG_EXT_MAP.values() for alias in aliases} | {"shell", "diff", "patch", "dockerfile", "xml", "toml"}


def is_valid_card_ending(fname: str, tag: str) -> bool:
    """Check if the tag matches the filename extension."""
    if "." not in fname:
        return False
    ext = fname.split(".")[-1].lower()
    tag_l = tag.lower()
    return tag_l == ext or tag_l in LANG_EXT_MAP.get(ext, set())


def is_workspace_promise(text: str, default_fname: str = "") -> bool:
    """Detect if Muse is hallucinating an internal ~/workspace container or background runner without emitting code."""
    t = text.lower()
    has_workspace = bool(
        "~/workspace" in t
        or "workspace/" in t
        or re.search(r"\b(?:in|into|inside|from|at)\s+(?:my\s+)?workspace\b", t)
        or re.search(r"\b(?:building it now|still building|i'll report back|will report back|built and verified)\b", t)
    )
    if not has_workspace:
        return False
    # If the response already contains code blocks or file cards, it's not an empty promise
    _, files = parse_cards_and_fences(text, default_fname=default_fname)
    return len(files) == 0


# ---------------------------------------------------------------------------
# False-completion guard: Muse claims files it never delivered
# ---------------------------------------------------------------------------

# EN / VI wording that turns a path mention into a "I created/saved this" claim.
_CLAIM_PHRASE_RE = re.compile(
    r"(?:"
    r"đã\s*(?:lưu|tạo|ghi|viết|hoàn\s*thành|xong|sẵn\s*sàng)"
    r"|lưu\s*(?:vào|tại|xuống|trữ)"
    r"|tạo\s*(?:xong|tại|trong|ra|được)"
    r"|xong\b|sẵn\s*sàng"
    r"|\b(?:saved|created|stored|written|generated|done|ready)\b"
    r"|\b(?:hoàn\s*thành|đã\s*(?:tạo|lưu|ghi|xóa|xoá)|lưu\s*xong)\b"
    r")",
    re.IGNORECASE,
)

# Full path with separators (drive letter / UNC / relative) ending in an extension.
# Final lookahead (?!\.?\w) allows a sentence-final period ("...hero.jpg.") while
# still rejecting truncated matches inside longer dotted names ("file.jpg.txt").
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w.])"
    r"(?:[A-Za-z]:)?"
    r"(?:[\\/]|[\w.\-]+[\\/])"
    r"(?:[\w.\-]+[\\/])*"
    r"(?:[A-Za-z_][\w.\-]*\.[A-Za-z][A-Za-z0-9_]{0,5}|[\w.\-]*[A-Za-z][\w.\-]*\.[A-Za-z][A-Za-z0-9_]{0,5})"
    r"(?!\.?\w)"
)

# Bare filename like hero.jpg (only trusted next to a claim phrase or on its own line).
_BARE_FILENAME_RE = re.compile(
    r"(?<![\w./\\|*?<>])([A-Za-z0-9_\-]{1,80}\.[A-Za-z][A-Za-z0-9_]{0,5})(?!\.?\w)"
)


def extract_claimed_paths(text: str, cwd: str = "") -> List[str]:
    """Absolute paths of files the reply claims to have created/saved on the user's PC.

    A token counts as a delivery claim when it is:
    - a full/relative path with separators (``E:\\...\\hero.jpg``, ``assets/x.png``), or
    - a bare filename (``hero.jpg``) that sits on its own line or next to a
      completion-claim phrase (EN/ES/VI).

    URLs and glob patterns are ignored. Returns paths in order of appearance, deduped.
    """
    if not text:
        return []
    candidates: List[Tuple[int, str]] = []

    def _is_url_tail(pos: int) -> bool:
        # True when the text right before `pos` is still part of a URL scheme
        # (e.g. the match starts inside https://host/path.jpg).
        i = pos - 1
        run = 0
        while i >= 0 and (text[i].isalnum() or text[i] in ".-_/\\"):
            i -= 1
            run += 1
        return run >= 1 and i >= 0 and text[i] == ":"

    for m in _PATH_TOKEN_RE.finditer(text):
        if _is_url_tail(m.start()):
            continue
        candidates.append((m.start(), m.group(0)))

    full_ranges = [m.span(0) for m in _PATH_TOKEN_RE.finditer(text)]

    for m in _BARE_FILENAME_RE.finditer(text):
        fname = m.group(1)
        start, end = m.span(1)
        # Skip filenames already consumed as part of a full-path match.
        if any(rs <= start and end <= re_ for rs, re_ in full_ranges):
            continue
        # Skip filenames that are the tail of a URL (https://host/img.jpg).
        if _is_url_tail(start):
            continue
        own_line = bool(re.search(r"(?m)^\s*" + re.escape(fname) + r"\s*$", text))
        window = text[max(0, start - 60): end + 60]
        if own_line or _CLAIM_PHRASE_RE.search(window):
            candidates.append((start, fname))

    candidates.sort(key=lambda c: c[0])

    claims: List[str] = []
    seen: set = set()

    def _add(tok: str) -> None:
        tok = tok.strip(" \t\"'`<>[](){}.,;:")
        tok = re.sub(r":\d+$", "", tok)
        if not tok or "://" in tok:
            return
        if tok.startswith(("http:", "https:", "ftp:", "data:", "~")):
            return
        if any(c in tok for c in "*?<>|"):
            return
        if os.path.isabs(tok) or re.match(r"^[A-Za-z]:[\\/]", tok):
            ap = os.path.normpath(tok)
        elif cwd:
            ap = os.path.normpath(os.path.join(cwd, tok))
        else:
            ap = os.path.normpath(tok)
        key = os.path.normcase(ap)
        if key in seen:
            return
        seen.add(key)
        claims.append(ap)

    for _pos, tok in candidates:
        _add(tok)

    return claims


def missing_claimed_paths(text: str, cwd: str = "") -> List[str]:
    """Subset of extract_claimed_paths() that does not exist on disk right now."""
    return [p for p in extract_claimed_paths(text, cwd) if not os.path.exists(p)]


def build_missing_files_note(missing: List[str], cwd: str = "") -> str:
    """Short honest footnote for partial deliveries (some files real, some missing)."""
    listed = "\n".join(f"- `{p}`" for p in missing[:30])
    more = f"\n- ... and {len(missing) - 30} more" if len(missing) > 30 else ""
    return (
        "[Harness verification — IMPORTANT] The following file(s) mentioned above were "
        f"NOT found on disk and were not created (`{cwd}`):\n{listed}{more}\n"
        "Report this accurately to the user; do NOT claim these files exist."
    )


def build_unfulfilled_claim_report(reply: str, missing: List[str], cwd: str) -> str:
    """Replace a false success claim with a transparent failure report for Claude Code."""
    listed = "\n".join(f"- `{p}`" for p in missing[:30])
    more = f"\n- ... and {len(missing) - 30} more" if len(missing) > 30 else ""
    excerpt = reply.strip()[:600]
    return (
        "[HARNESS VERIFICATION FAILED — do NOT repeat Muse's success claim]\n"
        "Muse's reply contained path strings and success wording but emitted no file "
        "content, no code block, no archive, and no image attachment. A live filesystem "
        f"check against `{cwd}` confirms NOTHING was written:\n{listed}{more}\n\n"
        f"Muse's reply was:\n\"\"\"\n{excerpt}\n\"\"\"\n\n"
        "Do NOT tell the user these files were created. Report the failure honestly and offer:\n"
        "1. Re-request each file's full content directly in chat (markdown code block with a "
        "filename header) so the harness can write it.\n"
        "2. For binary images (.jpg/.png/.gif): Muse cannot emit binary files — request "
        "SVG / HTML-canvas placeholders instead, or generate placeholder images locally with a script.\n"
        "3. List exactly which claimed files exist on disk and which are missing."
    )


CLARIFY_WAIT_NOTE = (
    "[SYSTEM — Muse is WAITING for the user to choose an option. Nothing has been executed. "
    "Relay Muse's question (and the numbered option list if present) to the user and wait "
    "for their answer. Do NOT claim any work is done.]"
)

_CHOICE_RE = re.compile(
    r"(?:"
    r"bạn (?:muốn|chọn|thích|ưu tiên|có muốn|muốn mình)"
    r"|muốn mình"
    r"|vào file nào|file nào|thư mục nào|dòng nào"
    r"|hãy cho (?:mình|tôi|bạn|biết)"
    r"|cho mình biết (?:bạn|file|đâu)"
    r"|cần mình (?:xác nhận|chọn|hỏi)"
    r"|bạn (?:confirm|xác nhận)"
    r"|which (?:one|file|folder|option|place|location|version)"
    r"|do you want(?: me)? (?:to|in)?"
    r"|would you (?:like|prefer|rather)"
    r"|shall I (?:use|put|add|write|save|continue)"
    r"|let me know (?:which|what|if|whether|your|a |the )"
    r"|pick (?:one|a|the)"
    r"|choice between"
    r")",
    re.IGNORECASE,
)

# Strong markers ask for a user decision even without a trailing '?'.
_STRONG_CHOICE_RE = re.compile(
    r"(?:"
    r"bạn (?:muốn|chọn)"
    r"|vào file nào|file nào|thư mục nào"
    r"|which (?:one|file|folder|option|place|location|version)"
    r"|do you want(?: me)? "
    r"|would you (?:like|prefer|rather)"
    r"|shall I (?:use|put|add|write|save|continue)"
    r"|let me know (?:which|what|if|whether|your)"
    r"|pick (?:one|a|the)"
    r"|choice between"
    r")",
    re.IGNORECASE,
)


def is_clarifying_question(text: str) -> bool:
    """True when Muse's reply is a question asking the user to pick/confirm
    (instead of actually doing the work). Such replies must never be turned
    into tool calls or completion claims by the harness.
    """
    t = (text or "").strip()
    if not t:
        return False
    if "[MUSE OFFERED CHOICES" in t:
        return True
    if "```" in t:
        # Delivered real code/content -> this is an answer, not just a question.
        return False
    if _STRONG_CHOICE_RE.search(t[:4000]):
        # Explicit decision request: counts even without a trailing '?'.
        return True
    if not _CHOICE_RE.search(t[:4000]):
        return False
    if t.rstrip()[-1] in "?？":
        return True
    # Options list may follow the question mark (bubble includes button labels).
    return "?" in t.rstrip()[-400:] or "？" in t.rstrip()[-400:]


def guard_false_completion(
    reply: str,
    claim_source: str,
    tools_to_emit: List[Tuple[str, dict]],
    thought: str,
    extracted_files: List[str],
    cwd: str,
    tool_names: set,
    default_fname: str,
    send_fn,
) -> Tuple[str, List[Tuple[str, dict]], str, List[str]]:
    """Verify Muse's file-creation claims against disk; re-prompt or report honestly.

    Muse often emits bare path strings and 'done' wording without delivering any
    file content. Without this check Claude Code relays that false success straight
    to the user (e.g. "4 images saved to assets/" when nothing was written).

    ``send_fn(prompt, cwd=cwd)`` must re-send a prompt to Muse and return
    ``(reply, extracted_files)``. Returns ``(reply, tools, thought, extracted)``.
    """
    def _missing(text: str) -> List[str]:
        miss = missing_claimed_paths(text, cwd)
        tool_paths = set()
        for _tn, ti in tools_to_emit:
            if isinstance(ti, dict) and isinstance(ti.get("file_path"), str):
                tool_paths.add(os.path.normcase(os.path.normpath(ti["file_path"])))
        return [m for m in miss if os.path.normcase(os.path.normpath(m)) not in tool_paths]

    missing = _missing(claim_source)
    if not missing:
        return reply, tools_to_emit, thought, extracted_files

    if tools_to_emit or extracted_files:
        # Partial delivery: real outputs exist alongside unfulfilled claims.
        print(
            f"[*] Honesty guard: {len(missing)} claimed path(s) not on disk; appending verification note.",
            file=sys.stderr, flush=True,
        )
        reply = f"{reply.strip()}\n\n{build_missing_files_note(missing, cwd)}"
        return reply, tools_to_emit, thought, extracted_files

    # Nothing deliverable at all -> re-prompt Muse (max 2x) for real content.
    for attempt in range(2):
        print(
            f"[*] Honesty guard: Muse claimed {len(missing)} file(s) that do not exist on disk "
            f"(attempt {attempt + 1}/2); re-prompting for actual content...",
            file=sys.stderr, flush=True,
        )
        listed = "\n".join(f"- `{m}`" for m in missing[:30])
        retry_prompt = (
            "VERIFICATION FAILED - I checked my computer: these files you mentioned do NOT exist; "
            "you sent only path strings with no file content:\n"
            f"{listed}\n\n"
            "Output the actual full contents of each file NOW - one markdown code block per file with "
            "a filename header (### `filename.ext`). If a file is a binary image (.jpg/.png/.gif) that "
            "you cannot produce as binary: do NOT repeat paths or claim success - output an SVG/HTML "
            "placeholder with the same subject instead, or state clearly that you cannot generate it."
        )
        try:
            retry_reply, retry_extracted = send_fn(retry_prompt, cwd=cwd)
        except Exception as ex:
            print(f"[!] Honesty guard re-prompt failed: {ex}", file=sys.stderr, flush=True)
            break
        if retry_extracted:
            extracted_files = list(retry_extracted)
        if retry_reply:
            reply = retry_reply
            claim_source = retry_reply
        retry_thought, retry_tools = detect_tool_calls(reply, tool_names, cwd, default_fname=default_fname)
        if retry_tools:
            thought, tools_to_emit = retry_thought, retry_tools
            missing = []
            break
        missing = _missing(claim_source)
        if not missing:
            break

    if tools_to_emit or extracted_files:
        if missing:
            reply = f"{reply.strip()}\n\n{build_missing_files_note(missing, cwd)}"
        return reply, tools_to_emit, thought, extracted_files

    if missing:
        print(
            "[*] Honesty guard: Muse still delivered nothing after retries; returning transparent failure report.",
            file=sys.stderr, flush=True,
        )
        reply = build_unfulfilled_claim_report(reply, missing, cwd)
    return reply, tools_to_emit, thought, extracted_files


def clean_bottom_card_content(content: str) -> Tuple[str, str]:
    """Separate introductory conversational text from the actual content of a bottom-tagged card."""
    parts = re.split(r"\n{2,}", content.strip())
    if len(parts) <= 1:
        return "", content.strip()

    split_idx = 0
    prose_markers = [
        r"(?:grab it here|here is|here's|here are|created (?:in|inside) (?:my )?workspace|check it out|one note|note:)\s*:?$",
        r":\s*$",
    ]
    for i, p in enumerate(parts):
        p_clean = p.strip()
        is_intro = False
        p_lines = [l.strip() for l in p_clean.splitlines() if l.strip()]
        last_line = p_lines[-1] if p_lines else ""
        for pat in prose_markers:
            if re.search(pat, last_line, re.IGNORECASE):
                is_intro = True
                break
        if not is_intro:
            if re.search(r"^(?:done|created|already done|sure|okay|here)\b.*?\b(?:workspace|pc|hands)\b", p_clean, re.IGNORECASE):
                is_intro = True
            elif p_clean.startswith('"') and p_clean.endswith('"') and i < len(parts) - 1:
                is_intro = True
        if is_intro:
            split_idx = i + 1

    intro_parts = parts[:split_idx]
    code_parts = parts[split_idx:] if split_idx < len(parts) else parts[-1:]
    return "\n\n".join(intro_parts).strip(), "\n\n".join(code_parts).strip()


def parse_cards_and_fences(block: str, default_fname: str = "") -> Tuple[str, List[Tuple[str, str]]]:
    """
    Extract generated files from a response block, supporting:
    1. Single or multiple Muse native file cards:
       - Top-headed: <filename>\n<TAG>\n<code>
       - Bottom-tagged: <code>\n<filename>\n<TAG>
    2. Markdown code fences with filenames in header, info string, or first line comment.
    3. Muse web code block widgets rendered with a language badge (<lang_tag>\n<content>).
    Returns: (thought_commentary, [(filename, content), ...])
    """
    found_files: List[Tuple[str, str]] = []
    thoughts: List[str] = []
    lines = block.splitlines()

    # Find all (i, filename, tag) occurrences
    matches = []
    for i in range(len(lines) - 1):
        l1 = lines[i].strip()
        l2 = lines[i + 1].strip()
        m = re.match(r"^([a-zA-Z0-9_.\-\\/]+\.([a-zA-Z0-9_]+))$", l1)
        if m and is_valid_card_ending(l1, l2):
            matches.append((i, l1, l2))

    if matches:
        first_i, first_fname, first_tag = matches[0]
        lines_after = len(lines) - (first_i + 2)
        lines_before = first_i

        is_top_headed = False
        if lines_after > 0:
            if lines_before <= 2:
                is_top_headed = True
            elif lines_after > lines_before:
                is_top_headed = True

        if is_top_headed:
            # Case A: Filename and TAG are at the TOP of code
            intro_lines = lines[:first_i]
            clean_intro = [l for l in intro_lines if not re.match(r"^[a-zA-Z0-9_.\-\\/]+\.[a-zA-Z0-9_]+$", l.strip())]
            if clean_intro:
                thoughts.append("\n".join(clean_intro).strip())

            for idx, (m_i, m_fname, m_tag) in enumerate(matches):
                start_c = m_i + 2
                end_c = matches[idx + 1][0] if idx + 1 < len(matches) else len(lines)
                raw_code = lines[start_c:end_c]
                code_txt = "\n".join(raw_code).strip()
                if code_txt:
                    found_files.append((m_fname, code_txt))
        else:
            # Case B: Filename and TAG are at the BOTTOM of code
            prev_idx = 0
            for card_idx, fname, tag in matches:
                content_lines = lines[prev_idx:card_idx]
                content = "\n".join(content_lines).strip()
                intro, clean_content = clean_bottom_card_content(content)
                if intro:
                    thoughts.append(intro)
                if clean_content:
                    found_files.append((fname, clean_content))
                prev_idx = card_idx + 2

            if prev_idx < len(lines):
                trailing = "\n".join(lines[prev_idx:]).strip()
                if trailing:
                    thoughts.append(trailing)

    # 2. If no native cards, check markdown code fences
    if not found_files:
        raw_fences = list(re.finditer(r"```([a-zA-Z0-9_+\-]*)(?::([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+))?\n+(.*?)\n+```", block, re.DOTALL))
        for m in raw_fences:
            lang = m.group(1)
            fn_in_fence_tag = m.group(2)
            content = m.group(3)

            pre_text = block[:m.start()]
            pre_lines = [l.strip() for l in pre_text.splitlines() if l.strip()]
            last_pre_line = pre_lines[-1] if pre_lines else ""

            fname = fn_in_fence_tag
            if not fname and last_pre_line:
                m_fn = re.search(r"[`\"']?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)[`\"']?", last_pre_line)
                if m_fn:
                    cand = m_fn.group(1)
                    if "." in cand and len(cand.split(".")[-1]) <= 6:
                        fname = cand

            if not fname:
                first_line = content.splitlines()[0].strip() if content.splitlines() else ""
                m_first = re.match(r"^(?:#|//|/\*|<!--)\s*(?:filename:?\s*)?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)", first_line, re.IGNORECASE)
                if m_first:
                    fname = m_first.group(1)
                    content = "\n".join(content.splitlines()[1:]).strip()

            if not fname and default_fname:
                fname = default_fname

            if fname and fname not in [f[0] for f in found_files]:
                found_files.append((fname, content))

        if found_files:
            cleaned = re.sub(r"```[a-zA-Z0-9_+\-]*.*?\n+.*?\n+```", "", block, flags=re.DOTALL).strip()
            if cleaned:
                thoughts.append(cleaned)

    # 3. If no native cards or fences, check Muse web code block widgets (<lang_tag>\n<content>)
    if not found_files:
        lines = block.splitlines()
        for i, line in enumerate(lines):
            tag = line.strip().lower()
            if tag in ALL_LANG_TAGS and i + 1 < len(lines):
                code_lines = lines[i + 1:]
                code_content = "\n".join(code_lines).strip()
                intro_lines = lines[:i]
                intro_text = "\n".join(intro_lines).strip()

                fname = default_fname
                m_fn = re.search(r"[`\"']?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]{1,6})[`\"']?", intro_text)
                if m_fn:
                    cand = m_fn.group(1)
                    if "." in cand and len(cand.split(".")[-1]) <= 6:
                        fname = cand

                if fname and code_content:
                    found_files.append((fname, code_content))
                    if intro_text:
                        thoughts.append(intro_text)
                    break

    thought_str = "\n\n".join(thoughts).strip()
    return thought_str, found_files


_SHELL_TOOL_NAMES = {"Bash", "PowerShell", "GitBash"}


def _sanitize_tool_input(t_name: str, t_input: Any, cwd: str, allowed: set) -> Optional[dict]:
    """Validate/normalize tool input from Muse. Returns dict or None if unusable."""
    if not isinstance(t_name, str) or t_name not in allowed:
        return None

    # Raw string payload (e.g. [TOOL: Bash "git status"]) -> wrap as command.
    if isinstance(t_input, str):
        t_input = {"command": t_input} if t_name in _SHELL_TOOL_NAMES else {}
    if not isinstance(t_input, dict):
        return None

    if t_name in _SHELL_TOOL_NAMES:
        cmd = t_input.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return None
        return {"command": cmd.strip()}

    if t_name in ("Write", "Edit", "NotebookEdit"):
        fp = t_input.get("file_path")
        if not isinstance(fp, str) or not fp.strip():
            return None
        if not os.path.isabs(fp):
            fp = os.path.normpath(os.path.join(cwd, fp))
        t_input["file_path"] = fp
        if t_name == "Write" and not isinstance(t_input.get("content"), str):
            return None
        return t_input

    if t_name in ("Read", "NotebookRead"):
        fp = t_input.get("file_path")
        if not isinstance(fp, str) or not fp.strip():
            return None
        if not os.path.isabs(fp):
            fp = os.path.normpath(os.path.join(cwd, fp))
        return {"file_path": fp}

    if t_name in ("Glob", "Grep"):
        if not any(isinstance(v, str) and v.strip() for v in t_input.values()):
            return None
        return t_input

    if t_name == "WebFetch":
        if not isinstance(t_input.get("url"), str) or not t_input.get("url", "").startswith(("http://", "https://")):
            return None
        return t_input

    # Other allowed tools (e.g. TodoWrite): accept any dict payload.
    return t_input


def detect_tool_calls(reply: str, tool_names: set, cwd: str, default_fname: str = "") -> Tuple[str, List[Tuple[str, dict]]]:
    """
    Detect if Muse's reply represents actions to execute on the local computer
    (e.g., creating files, executing commands, reading files), or if Muse inspected
    its remote cloud container and needs to be redirected to the local PC.
    Returns: (thought_text, [(tool_name, tool_input_dict), ...])
    """
    clean_reply = reply.strip()
    tools: List[Tuple[str, dict]] = []
    thoughts: List[str] = []

    # 0a. Check if Muse reported that a specific file is not in its cloud workspace
    m_no_file = re.search(
        r"there'?s no\s+([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)\s+in (?:my )?workspace",
        clean_reply,
        flags=re.IGNORECASE,
    )
    if m_no_file:
        missing_name = m_no_file.group(1).strip()
        local_path = os.path.normpath(os.path.join(cwd, missing_name))
        if os.path.isfile(local_path) and ("Read" in tool_names or "FileRead" in tool_names):
            t_name = "Read" if "Read" in tool_names else "FileRead"
            return f"Reading `{missing_name}` from your computer.", [(t_name, {"file_path": local_path})]

    # 0c. Check if Muse states it cannot read a file or needs to read a local file
    m_cant_read = re.search(
        r"(?:can't|cannot|unable to)\s+(?:open|read|access|be sure about|tell)\s+.*?`?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)`?|`?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)`?\s+(?:I\s+)?(?:can't|cannot)\s+be\s+sure\s+about\s+without\s+reading|paste\s+(?:its|the)\s+contents?\s+(?:of\s+)?`?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)`?",
        clean_reply,
        flags=re.IGNORECASE,
    )
    if m_cant_read:
        fname = (m_cant_read.group(1) or m_cant_read.group(2) or m_cant_read.group(3) or "").strip()
        if fname:
            local_path = fname if os.path.isabs(fname) else os.path.normpath(os.path.join(cwd, fname))
            if os.path.isfile(local_path) and ("Read" in tool_names or "FileRead" in tool_names):
                t_name = "Read" if "Read" in tool_names else "FileRead"
                return f"Reading `{fname}` on your computer to inspect its contents.", [(t_name, {"file_path": local_path})]

    # Split into bubbles / blocks
    if "<!-- bubble -->" in clean_reply:
        blocks = [b.strip() for b in clean_reply.split("<!-- bubble -->") if b.strip()]
    else:
        blocks = [clean_reply]

    write_tool_name = "Write" if "Write" in tool_names else ("FileWrite" if "FileWrite" in tool_names else None)

    for block in blocks:
        # Check explicit tags: [TOOL: ToolName {...}]
        m_tags = list(re.finditer(r"\[(?:TOOL|TOOL_USE|ACTION):\s*([a-zA-Z0-9_]+)\s*(\{.*?\})\s*\]", block, flags=re.DOTALL))
        if m_tags:
            for m_tag in m_tags:
                t_name = m_tag.group(1).strip()
                try:
                    raw = json.loads(m_tag.group(2).strip())
                except Exception:
                    continue
                t_input = _sanitize_tool_input(t_name, raw, cwd, tool_names)
                if t_input:
                    tools.append((t_name, t_input))
            th = block[:m_tags[0].start()].strip()
            if th:
                thoughts.append(th)
            continue

        # Check tool markdown block: ```tool:ToolName ... ```
        m_blocks = list(re.finditer(r"```tool:([a-zA-Z0-9_]+)\n+(.*?)\n+```", block, flags=re.DOTALL))
        if m_blocks:
            for mb in m_blocks:
                t_name = mb.group(1).strip()
                content = mb.group(2).strip()
                t_input = None
                try:
                    t_input = _sanitize_tool_input(t_name, json.loads(content), cwd, tool_names)
                except Exception:
                    t_input = None
                # Fallback: broken JSON but content looks like a raw shell command.
                if t_input is None and t_name in _SHELL_TOOL_NAMES and content and not content.lstrip().startswith("{"):
                    t_input = _sanitize_tool_input(t_name, content, cwd, tool_names)
                if t_input:
                    tools.append((t_name, t_input))
            th = block[:m_blocks[0].start()].strip()
            if th:
                thoughts.append(th)
            continue

        # Check bash/powershell code fence
        m_cmd = re.search(r"```(?:bash|sh|powershell|cmd|shell)\n+(.*?)\n+```", block, flags=re.DOTALL)
        if m_cmd and ("Bash" in tool_names or "PowerShell" in tool_names):
            cmd = m_cmd.group(1).strip()
            t_name = "PowerShell" if "PowerShell" in tool_names else "Bash"
            tools.append((t_name, {"command": cmd}))
            th = block[:m_cmd.start()].strip()
            if th:
                thoughts.append(th)
            continue

        # Bare shell command(s) mixed with prose, e.g. "Intro:\n\ngit branch demo/v1\n\nHint..."
        if ("Bash" in tool_names or "PowerShell" in tool_names) and not tools:
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n", block) if p.strip()]
            cmd_paras = []
            thought_paras = []
            cmd_bin_re = (
                r"^(?:`[^`]+`|\"[^\"]+\"|"
                r"(?:cd|git|python3?|py|pip|npm|npx|node|yarn|pnpm|cargo|go|make|pytest|"
                r"composer|docker|dir|ls|echo|type|cat|powershell|cmd|curl|wget|mkdir|rmdir|md|"
                r"tar|7z|xcopy|robocopy|copy|move|ren|del|iwr|Invoke-WebRequest|Invoke-RestMethod|"
                r"Expand-Archive|Compress-Archive|Start-BitsTransfer|New-Item|Remove-Item|"
                r"Get-ChildItem|Set-Content|Copy-Item|Move-Item|Rename-Item)(?:\s.*)?)$"
            )
            for p in paragraphs:
                lines = [l.strip() for l in p.split("\n") if l.strip()]
                looks_cmd = False
                if lines and len(p) <= 1200 and not re.search(r"[.!?…]\s*$", lines[-1]):
                    if len(lines) == 1:
                        looks_cmd = bool(re.match(cmd_bin_re, lines[0], flags=re.IGNORECASE))
                    else:
                        # Multi-line command block: every line starts with a known
                        # binary or is a continuation (quoted string, flag, path, pipe).
                        looks_cmd = all(
                            re.match(cmd_bin_re, l, flags=re.IGNORECASE)
                            or re.match(r"^[\"'(\-\\./&|]|^-Uri|^-OutFile|^-Method|^-Body", l)
                            for l in lines
                        )
                if looks_cmd:
                    cmd_paras.append("\n".join(lines))
                else:
                    thought_paras.append(p)
            if cmd_paras:
                t_name = "PowerShell" if "PowerShell" in tool_names else "Bash"
                for c in cmd_paras:
                    tools.append((t_name, {"command": c}))
                if thought_paras:
                    thoughts.append("\n\n".join(thought_paras))
                continue

        # Check Muse native file cards and markdown code fences
        block_thought, found_files = parse_cards_and_fences(block, default_fname=default_fname)
        if found_files and write_tool_name:
            if block_thought:
                thoughts.append(block_thought)
            for fname, content in found_files:
                abs_path = fname if os.path.isabs(fname) else os.path.normpath(os.path.join(cwd, fname))
                tools.append((write_tool_name, {"file_path": abs_path, "content": content}))
            continue

        # Check if Muse text states it deleted or wants to delete existing files in cwd
        m_del = re.findall(r"\b(?:delete|deleted|remove|removed|deleting|removing|xoa|xoá|xóa|đã\s*xóa|đã\s*xoá)\s+[`\"]?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)[`\"]?", block, flags=re.IGNORECASE)
        if m_del and ("PowerShell" in tool_names or "Bash" in tool_names):
            del_paths = []
            for fn in m_del:
                full = os.path.normpath(os.path.join(cwd, fn))
                if (os.path.isfile(full) or os.path.isdir(full)) and full not in del_paths:
                    del_paths.append(full)
            if del_paths:
                cmd_tool = "PowerShell" if "PowerShell" in tool_names else "Bash"
                if cmd_tool == "PowerShell":
                    paths_str = ", ".join(f"'{p}'" for p in del_paths)
                    del_cmd = f"Remove-Item -Path {paths_str} -Recurse -Force"
                else:
                    paths_str = " ".join(f"'{p}'" for p in del_paths)
                    del_cmd = f"rm -rf {paths_str}"
                tools.append((cmd_tool, {"command": del_cmd}))
                continue

        # Normal text block (thought / commentary)
        thoughts.append(block)

    # 0b. Fallback: ONLY check if Muse reported its own remote server workspace directories
    # IF NO other actions/tools were generated!
    if not tools:
        cloud_dummy_files = ["banana.txt", "cherry.txt", "apple.txt", "cron.d", "onboarding_tour", "self_improvement", "spaces/"]
        reply_lower = clean_reply.lower()
        if any(dummy in reply_lower for dummy in cloud_dummy_files):
            if "Glob" in tool_names:
                return "Checking the actual files in your local working directory on your computer.", [("Glob", {"pattern": "*"})]
            elif "PowerShell" in tool_names:
                return "Listing files in your local folder.", [("PowerShell", {"command": "Get-ChildItem -Name"})]
            elif "Bash" in tool_names:
                return "Listing files in your local folder.", [("Bash", {"command": "ls"})]

    thought_str = "\n\n".join(thoughts).strip()
    if not thought_str and tools:
        if len(tools) == 1:
            thought_str = f"Performing {tools[0][0]} on your computer."
        else:
            thought_str = f"Performing {len(tools)} actions on your computer."

    return thought_str, tools
