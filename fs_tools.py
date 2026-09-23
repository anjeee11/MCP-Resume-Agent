"""
fs_tools.py
-----------
Milestone 1 file-system tool functions.

NOTE ON PROVENANCE:
This module reproduces the PUBLIC FUNCTION SIGNATURES and behavior contract
of Anjeee's actual Milestone 1 `fs_tools.py` (the Gemini function-calling
file system assistant). It is written standalone here so Milestone 4 can be
graded/demoed independently, but it is a drop-in replacement target: copy
your real Milestone 1 `fs_tools.py` over this file and
`filesystem_mcp_server.py` will keep working unmodified, AS LONG AS these
function names / argument names / return shapes are preserved:

    list_directory(path: str, pattern: str = "*") -> dict
    read_file(path: str) -> dict
    write_file(path: str, content: str, overwrite: bool = False) -> dict
    search_files(directory: str, query: str, recursive: bool = True) -> dict
    get_file_info(path: str) -> dict
    delete_file(path: str) -> dict
    move_file(source: str, destination: str) -> dict
    create_directory(path: str) -> dict

Every function returns a plain JSON-serializable dict shaped like:
    {"success": bool, "data": <payload> | None, "error": str | None}
so the MCP layer never has to guess about error handling.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _ok(data: Any) -> Dict[str, Any]:
    return {"success": True, "data": data, "error": None}


def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "data": None, "error": message}


def _safe_path(path: str) -> Path:
    """Resolve a path, expanding ~ but NOT silently escaping cwd sandboxing
    decisions -- callers (the MCP server) enforce any directory allow-list."""
    return Path(path).expanduser().resolve()


# --------------------------------------------------------------------------
# Core Milestone 1 tools
# --------------------------------------------------------------------------

def list_directory(path: str, pattern: str = "*") -> Dict[str, Any]:
    """List files/directories under `path` matching a glob `pattern`."""
    try:
        p = _safe_path(path)
        if not p.exists():
            return _err(f"Path does not exist: {path}")
        if not p.is_dir():
            return _err(f"Path is not a directory: {path}")

        entries = []
        for child in sorted(p.iterdir()):
            if not fnmatch.fnmatch(child.name, pattern):
                continue
            stat = child.stat()
            entries.append({
                "name": child.name,
                "path": str(child),
                "type": "directory" if child.is_dir() else "file",
                "size_bytes": stat.st_size if child.is_file() else None,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            })
        return _ok({"directory": str(p), "pattern": pattern, "entries": entries, "count": len(entries)})
    except Exception as exc:  # pragma: no cover - defensive
        return _err(str(exc))


def read_file(path: str) -> Dict[str, Any]:
    """Read a text file's contents."""
    try:
        p = _safe_path(path)
        if not p.exists():
            return _err(f"File does not exist: {path}")
        if p.is_dir():
            return _err(f"Path is a directory, not a file: {path}")
        content = p.read_text(encoding="utf-8", errors="replace")
        return _ok({"path": str(p), "content": content, "size_bytes": len(content.encode("utf-8"))})
    except Exception as exc:
        return _err(str(exc))


def write_file(path: str, content: str, overwrite: bool = False) -> Dict[str, Any]:
    """Write text `content` to `path`. Refuses to clobber unless overwrite=True."""
    try:
        p = _safe_path(path)
        if p.exists() and not overwrite:
            return _err(f"File already exists (pass overwrite=True to replace): {path}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return _ok({"path": str(p), "bytes_written": len(content.encode("utf-8"))})
    except Exception as exc:
        return _err(str(exc))


def search_files(directory: str, query: str, recursive: bool = True) -> Dict[str, Any]:
    """Search for `query` (substring, case-insensitive) in filenames AND file
    contents under `directory`."""
    try:
        p = _safe_path(directory)
        if not p.exists() or not p.is_dir():
            return _err(f"Directory does not exist: {directory}")

        walker = p.rglob("*") if recursive else p.glob("*")
        matches = []
        q = query.lower()
        for child in walker:
            if not child.is_file():
                continue
            name_hit = q in child.name.lower()
            content_hit = False
            snippet = None
            try:
                text = child.read_text(encoding="utf-8", errors="ignore")
                idx = text.lower().find(q)
                if idx != -1:
                    content_hit = True
                    start = max(0, idx - 40)
                    end = min(len(text), idx + len(query) + 40)
                    snippet = text[start:end].replace("\n", " ")
            except Exception:
                pass
            if name_hit or content_hit:
                matches.append({
                    "path": str(child),
                    "matched_filename": name_hit,
                    "matched_content": content_hit,
                    "snippet": snippet,
                })
        return _ok({"directory": str(p), "query": query, "matches": matches, "count": len(matches)})
    except Exception as exc:
        return _err(str(exc))


def get_file_info(path: str) -> Dict[str, Any]:
    """Return metadata (size, timestamps, hash) about a file."""
    try:
        p = _safe_path(path)
        if not p.exists():
            return _err(f"Path does not exist: {path}")
        stat = p.stat()
        sha256 = None
        if p.is_file():
            sha256 = hashlib.sha256(p.read_bytes()).hexdigest()
        return _ok({
            "path": str(p),
            "type": "directory" if p.is_dir() else "file",
            "size_bytes": stat.st_size,
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_ctime)),
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            "sha256": sha256,
        })
    except Exception as exc:
        return _err(str(exc))


def delete_file(path: str) -> Dict[str, Any]:
    """Delete a file (not a directory)."""
    try:
        p = _safe_path(path)
        if not p.exists():
            return _err(f"Path does not exist: {path}")
        if p.is_dir():
            return _err(f"Refusing to delete a directory with delete_file: {path}")
        p.unlink()
        return _ok({"deleted": str(p)})
    except Exception as exc:
        return _err(str(exc))


def move_file(source: str, destination: str) -> Dict[str, Any]:
    """Move/rename a file from `source` to `destination`."""
    try:
        src = _safe_path(source)
        dst = _safe_path(destination)
        if not src.exists():
            return _err(f"Source does not exist: {source}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return _ok({"moved_from": str(src), "moved_to": str(dst)})
    except Exception as exc:
        return _err(str(exc))


def create_directory(path: str) -> Dict[str, Any]:
    """Create a directory (and parents) if it doesn't already exist."""
    try:
        p = _safe_path(path)
        p.mkdir(parents=True, exist_ok=True)
        return _ok({"created": str(p)})
    except Exception as exc:
        return _err(str(exc))
