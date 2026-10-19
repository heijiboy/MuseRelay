"""Project briefing and local-folder content builders.

These give Muse persistent awareness of the user's local project (working dir,
file tree, CLAUDE.md instructions) even on a brand-new chat thread.
"""

from __future__ import annotations

import os
from typing import Dict, List


def get_local_folder_contents(cwd: str, max_files: int = 50, max_size_bytes: int = 100_000) -> Dict[str, str]:
    """Read all text files in cwd, skipping VCS and dependencies."""
    files_content: Dict[str, str] = {}
    ignored_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode", "dist", "build"}
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith(".")]
        for f in sorted(files):
            if f.startswith("."):
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, cwd).replace("\\", "/")
            try:
                if os.path.isfile(full_path) and os.path.getsize(full_path) <= max_size_bytes:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as fp:
                        files_content[rel_path] = fp.read()
            except Exception:
                pass
            if len(files_content) >= max_files:
                break
        if len(files_content) >= max_files:
            break
    return files_content


def format_folder_contents(contents: Dict[str, str], cwd: str) -> str:
    """Format dictionary of local file contents clearly."""
    if not contents:
        return f"No files were found in your local folder (`{cwd}`)."
    parts = [f"Here are the contents of the files in your folder (`{cwd}`):\n"]
    for fname, fcontent in sorted(contents.items()):
        parts.append(f"{fname}:\n{fcontent.strip()}\n")
    return "\n".join(parts).strip()


def list_local_files(cwd: str, max_files: int = 100) -> List[str]:
    """List relative paths of files in cwd, skipping VCS/dependencies."""
    found = []
    ignored_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode", "dist", "build"}
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith(".")]
        for f in sorted(files):
            if f.startswith("."):
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, cwd).replace("\\", "/")
            found.append(rel_path)
            if len(found) >= max_files:
                break
        if len(found) >= max_files:
            break
    return sorted(found)


def build_project_briefing(cwd: str, max_tree_entries: int = 150, max_instr_chars: int = 2500) -> str:
    """Compact project briefing (working dir + file tree + project instructions) sent with every prompt.

    Gives Muse persistent awareness of the local project even on a brand-new
    chat thread, mirroring what a coding agent does with CLAUDE.md.
    """
    if not cwd or not os.path.isdir(cwd):
        return ""
    parts = [
        "[Project context — user's local machine]",
        f"Working directory: {cwd}",
    ]

    files = list_local_files(cwd, max_files=max_tree_entries)
    if files:
        tree = "\n".join(f"- {f}" for f in files)
        parts.append(f"File tree:\n{tree}")
    else:
        parts.append("File tree: (empty folder)")

    for instr_name in ("CLAUDE.md", "AGENTS.md", "MUSE.md", ".muse.md"):
        instr_path = os.path.join(cwd, instr_name)
        if os.path.isfile(instr_path):
            try:
                with open(instr_path, "r", encoding="utf-8", errors="replace") as fp:
                    instr = fp.read().strip()[:max_instr_chars]
                if instr:
                    parts.append(f"Project instructions ({instr_name}):\n{instr}")
                break
            except Exception:
                pass

    return "\n\n".join(parts)
