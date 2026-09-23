"""
filesystem_mcp_server.py
=========================
Milestone 4 — Part A: MCP Server Implementation.

Wraps the Milestone 1 file-system tools (`fs_tools.py`) as a standards-
compliant Model Context Protocol (MCP) server, built on the official
Anthropic `mcp` Python SDK (`mcp.server.fastmcp.FastMCP`).

Why FastMCP instead of hand-rolled JSON-RPC?
---------------------------------------------
MCP's wire format IS JSON-RPC 2.0 (initialize / tools/list / tools/call /
resources/list / resources/read, etc.) — that's what the spec mandates.
FastMCP implements that JSON-RPC 2.0 layer, request routing, capability
negotiation and error-code mapping for us, correctly, so this file can
focus on the domain logic instead of re-implementing a JSON-RPC engine
that the SDK already gets right. Everything below still satisfies the
assignment's "JSON-RPC 2.0 compliant interface" requirement because the
transport really is JSON-RPC 2.0 — you can see it by running the server
in inspector mode (see README) and watching the raw frames.

What this server exposes
-------------------------
MCP TOOLS (actions, callable by the agent):
    list_directory, read_file, write_file, search_files, get_file_info,
    delete_file, move_file, create_directory   <- Milestone 1 tools, unchanged
    watch_directory                            <- NEW: directory-change polling
    batch_process                              <- NEW: concurrent multi-file ops

MCP RESOURCES (discoverable, read-only data — the "resource discovery"
requirement):
    config://server                 server configuration snapshot
    directory://{path}              live listing of a directory as a resource
    watch-state://{path}            last known snapshot for a watched directory

Error handling
--------------
Two layers, on purpose:
  1. Protocol-level errors (bad args, path outside sandbox, tool crash) are
     raised as `McpError` with a proper JSON-RPC error code, so a
     misbehaving client sees a real protocol error, not a 200-OK lie.
  2. Logical/business errors from `fs_tools` (file not found, etc.) are
     NOT protocol errors — the tool call succeeded, the *operation*
     didn't. Those come back as a normal tool result with
     `{"success": false, "error": "..."}`, exactly like Milestone 1 did,
     so agent code written against Milestone 1's contract keeps working.

Configuration management
-------------------------
Controlled by environment variables (see `ServerConfig`), so the same
server binary can be pointed at different sandboxes/limits without code
changes — e.g. in CI vs. the demo video vs. Anjeee's real resume folder.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS, INTERNAL_ERROR

import fs_tools

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_AVAILABLE = True
except ImportError:  # graceful degradation -> pure polling mode
    _WATCHDOG_AVAILABLE = False


# --------------------------------------------------------------------------
# Configuration management
# --------------------------------------------------------------------------

@dataclass
class ServerConfig:
    """Server-wide configuration, overridable via environment variables so
    deployment behavior can change without touching code."""

    allowed_roots: List[str] = field(default_factory=lambda: [
        os.environ.get("MCP_FS_ROOT", str(Path.cwd()))
    ])
    max_batch_size: int = int(os.environ.get("MCP_FS_MAX_BATCH", "50"))
    max_batch_workers: int = int(os.environ.get("MCP_FS_BATCH_WORKERS", "8"))
    watch_poll_default_seconds: float = float(os.environ.get("MCP_FS_WATCH_POLL", "2.0"))
    server_name: str = "filesystem-mcp-server"
    server_version: str = "4.0.0"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "server_name": self.server_name,
            "server_version": self.server_version,
            "allowed_roots": self.allowed_roots,
            "max_batch_size": self.max_batch_size,
            "max_batch_workers": self.max_batch_workers,
            "watch_poll_default_seconds": self.watch_poll_default_seconds,
            "watchdog_available": _WATCHDOG_AVAILABLE,
        }


CONFIG = ServerConfig()


def _enforce_sandbox(path: str) -> None:
    """Protocol-level guard: refuse to operate outside allowed_roots.
    Raises McpError (JSON-RPC error), not a soft {"success": False}, because
    this is a client-abuse / misconfiguration case, not a normal file-not-found."""
    resolved = str(Path(path).expanduser().resolve())
    roots = [str(Path(r).expanduser().resolve()) for r in CONFIG.allowed_roots]
    if not any(resolved == r or resolved.startswith(r + os.sep) for r in roots):
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"Path '{path}' is outside allowed sandbox roots {roots}",
        ))


# --------------------------------------------------------------------------
# MCP server instance
# --------------------------------------------------------------------------

mcp = FastMCP(CONFIG.server_name)


# --------------------------------------------------------------------------
# Tools: Milestone 1 file system tools, exposed 1:1
# --------------------------------------------------------------------------

@mcp.tool()
def list_directory(path: str, pattern: str = "*") -> Dict[str, Any]:
    """List files and subdirectories under `path`, optionally filtered by a
    glob `pattern` (e.g. '*.pdf'). Returns entries with type/size/modified."""
    _enforce_sandbox(path)
    return fs_tools.list_directory(path, pattern)


@mcp.tool()
def read_file(path: str) -> Dict[str, Any]:
    """Read and return the full text content of a file."""
    _enforce_sandbox(path)
    return fs_tools.read_file(path)


@mcp.tool()
def write_file(path: str, content: str, overwrite: bool = False) -> Dict[str, Any]:
    """Write `content` to `path`. Fails unless overwrite=True if the file exists."""
    _enforce_sandbox(path)
    return fs_tools.write_file(path, content, overwrite)


@mcp.tool()
def search_files(directory: str, query: str, recursive: bool = True) -> Dict[str, Any]:
    """Search filenames and file contents under `directory` for `query`."""
    _enforce_sandbox(directory)
    return fs_tools.search_files(directory, query, recursive)


@mcp.tool()
def get_file_info(path: str) -> Dict[str, Any]:
    """Return metadata (size, timestamps, sha256) for a file or directory."""
    _enforce_sandbox(path)
    return fs_tools.get_file_info(path)


@mcp.tool()
def delete_file(path: str) -> Dict[str, Any]:
    """Delete a single file."""
    _enforce_sandbox(path)
    return fs_tools.delete_file(path)


@mcp.tool()
def move_file(source: str, destination: str) -> Dict[str, Any]:
    """Move or rename a file from `source` to `destination`."""
    _enforce_sandbox(source)
    _enforce_sandbox(destination)
    return fs_tools.move_file(source, destination)


@mcp.tool()
def create_directory(path: str) -> Dict[str, Any]:
    """Create a directory (and any missing parents)."""
    _enforce_sandbox(path)
    return fs_tools.create_directory(path)


# --------------------------------------------------------------------------
# NEW MCP-specific capability #1: watch_directory
# --------------------------------------------------------------------------
#
# A JSON-RPC tool call is inherently request/response, so "watching" is
# implemented as change-since-last-check polling, backed by an optional
# watchdog.Observer that keeps accumulating events in the background between
# polls (so nothing is missed even if the client polls infrequently).

class _DirectoryWatchState:
    def __init__(self) -> None:
        self.snapshots: Dict[str, Dict[str, float]] = {}   # dir -> {filename: mtime}
        self.pending_events: Dict[str, List[Dict[str, Any]]] = {}  # dir -> events
        self.observers: Dict[str, Any] = {}                 # dir -> watchdog Observer

    def _snapshot_dir(self, directory: str) -> Dict[str, float]:
        p = Path(directory)
        return {f.name: f.stat().st_mtime for f in p.iterdir() if f.is_file()}

    def start_background_watch(self, directory: str) -> None:
        if not _WATCHDOG_AVAILABLE or directory in self.observers:
            return

        state = self

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    state.pending_events.setdefault(directory, []).append({
                        "event": "created", "path": event.src_path,
                        "detected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })

            def on_modified(self, event):
                if not event.is_directory:
                    state.pending_events.setdefault(directory, []).append({
                        "event": "modified", "path": event.src_path,
                        "detected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })

            def on_deleted(self, event):
                if not event.is_directory:
                    state.pending_events.setdefault(directory, []).append({
                        "event": "deleted", "path": event.src_path,
                        "detected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })

        observer = Observer()
        observer.schedule(Handler(), directory, recursive=False)
        observer.daemon = True
        observer.start()
        self.observers[directory] = observer

    def poll(self, directory: str) -> Dict[str, Any]:
        before = self.snapshots.get(directory)
        after = self._snapshot_dir(directory)
        self.snapshots[directory] = after

        new_files, modified_files = [], []
        if before is not None:
            for name, mtime in after.items():
                if name not in before:
                    new_files.append(name)
                elif mtime > before[name]:
                    modified_files.append(name)
            removed_files = [name for name in before if name not in after]
        else:
            removed_files = []

        events = self.pending_events.pop(directory, [])
        return {
            "directory": directory,
            "is_first_poll": before is None,
            "new_files": sorted(new_files),
            "modified_files": sorted(modified_files),
            "removed_files": sorted(removed_files),
            "background_events_since_last_poll": events,
            "total_files_now": len(after),
            "watchdog_backend_active": directory in self.observers,
        }


_WATCH_STATE = _DirectoryWatchState()


@mcp.tool()
def watch_directory(directory: str, enable_background_watch: bool = True) -> Dict[str, Any]:
    """Monitor `directory` for new/changed/removed resume files.

    Each call is a POLL: it diffs the current directory contents against the
    last time this tool was called for the same directory and reports what
    changed. On the very first call for a directory it just takes a
    baseline snapshot (`is_first_poll: true`, no diff yet).

    If `enable_background_watch` is True and the `watchdog` package is
    available, a background filesystem observer is also started for that
    directory so that create/modify/delete events are captured continuously
    between polls (surfaced under `background_events_since_last_poll`),
    not just at the two snapshot instants.

    Typical agent usage: call once at startup to baseline, then call again
    on a timer (e.g. every N seconds) or right before a matching run to
    pick up newly-dropped resumes automatically.
    """
    try:
        _enforce_sandbox(directory)
        p = Path(directory).expanduser().resolve()
        if not p.exists() or not p.is_dir():
            return fs_tools._err(f"Directory does not exist: {directory}")

        if enable_background_watch:
            _WATCH_STATE.start_background_watch(str(p))

        return fs_tools._ok(_WATCH_STATE.poll(str(p)))
    except McpError:
        raise
    except Exception as exc:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=str(exc)))


# --------------------------------------------------------------------------
# NEW MCP-specific capability #2: batch_process
# --------------------------------------------------------------------------

_ALLOWED_BATCH_OPS = {"read_file", "get_file_info", "delete_file"}


def _run_one(operation: str, path: str) -> Dict[str, Any]:
    fn = getattr(fs_tools, operation)
    result = fn(path)
    return {"path": path, **result}


@mcp.tool()
def batch_process(paths: List[str], operation: str = "read_file",
                   max_workers: Optional[int] = None) -> Dict[str, Any]:
    """Apply a single file operation to MANY files concurrently.

    Args:
        paths: list of file paths to process (e.g. every resume in a batch
            upload). Capped at `ServerConfig.max_batch_size` per call.
        operation: one of 'read_file' | 'get_file_info' | 'delete_file'.
        max_workers: thread pool size override (defaults to server config).

    Returns a summary plus a per-file result list, so a partial failure in
    one resume (e.g. a corrupt PDF-as-text file) never aborts the whole
    batch — every other file is still processed and reported.
    """
    if operation not in _ALLOWED_BATCH_OPS:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"operation must be one of {sorted(_ALLOWED_BATCH_OPS)}, got '{operation}'",
        ))
    if len(paths) > CONFIG.max_batch_size:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=f"batch of {len(paths)} exceeds max_batch_size={CONFIG.max_batch_size}",
        ))
    for path in paths:
        _enforce_sandbox(path)

    workers = max_workers or CONFIG.max_batch_workers
    started = time.time()
    results: List[Dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_one, operation, path): path for path in paths}
        for future in as_completed(futures):
            path = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"path": path, "success": False, "data": None, "error": str(exc)})

    results.sort(key=lambda r: paths.index(r["path"]))
    succeeded = sum(1 for r in results if r.get("success"))
    return fs_tools._ok({
        "operation": operation,
        "requested": len(paths),
        "succeeded": succeeded,
        "failed": len(paths) - succeeded,
        "elapsed_seconds": round(time.time() - started, 4),
        "workers_used": workers,
        "results": results,
    })


# --------------------------------------------------------------------------
# Resources: discovery endpoints
# --------------------------------------------------------------------------

@mcp.resource("config://server")
def server_config_resource() -> str:
    """Current server configuration (sandbox roots, batch limits, etc.)."""
    return json.dumps(CONFIG.as_dict(), indent=2)


@mcp.resource("directory://{path}")
def directory_resource(path: str) -> str:
    """Live listing of a directory, exposed as a browsable MCP resource
    (as opposed to `list_directory`, which is the imperative tool form of
    the same data)."""
    _enforce_sandbox(path)
    result = fs_tools.list_directory(path)
    return json.dumps(result, indent=2)


@mcp.resource("watch-state://{path}")
def watch_state_resource(path: str) -> str:
    """Last known snapshot for a directory that `watch_directory` has polled
    at least once (empty if never polled)."""
    resolved = str(Path(path).expanduser().resolve())
    snapshot = _WATCH_STATE.snapshots.get(resolved)
    return json.dumps({
        "directory": resolved,
        "has_snapshot": snapshot is not None,
        "file_count": len(snapshot) if snapshot else 0,
        "files": snapshot or {},
    }, indent=2)


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

if __name__ == "__main__":
    # stdio transport: the standard way an agent spawns and talks to a
    # local MCP server (JSON-RPC 2.0 messages over stdin/stdout).
    mcp.run(transport="stdio")
