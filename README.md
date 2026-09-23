# Milestone 4 — MCP-Based Resume Matching System

Converts the Milestone 1 file-system tools into a real Model Context
Protocol (MCP) server, and refactors the Milestone 3 LangGraph resume
matching agent to talk to it (and to a second, bonus MCP server) via an
MCP client — instead of importing tool functions directly.

## What's here

| File | Role |
|---|---|
| `fs_tools.py` | Milestone 1 file-system functions (swap-in point for your real M1 file — see docstring). |
| `filesystem_mcp_server.py` | **Part A.** MCP server wrapping `fs_tools.py` + new `watch_directory()` / `batch_process()` capabilities. Built on the official `mcp` Python SDK (`FastMCP`), JSON-RPC 2.0 over stdio. |
| `skills_db_mcp_server.py` | **Part B bonus.** A second, independent MCP server (skills market-demand DB) to prove multi-MCP orchestration. |
| `mcp_client_manager.py` | Reusable async MCP client wrapper managing sessions to multiple servers at once. |
| `matching_agent.py` | **Part B.** Milestone 3's LangGraph agent, refactored to use MCP clients for all file I/O. |
| `docs/ARCHITECTURE.md` | State machine + sequence diagrams of agent ↔ MCP interaction (Mermaid). |
| `tests/test_mcp_server.py` | 10 end-to-end test scenarios against the real MCP server (spawned subprocess, real JSON-RPC). |
| `tests/test_agent_integration.py` | 6 test scenarios covering the agent's matching logic + full MCP-backed runs. |
| `sample_data/` | Seed resumes + a scratch directory for the `watch_directory` demo. |

## Setup

```powershell
uv venv
uv pip install -r requirements.txt
```

(Or `pip install -r requirements.txt --break-system-packages` if not using `uv`.)

Optional — to see the Gemini LLM sanity-check layer activate:
```powershell
$env:GOOGLE_API_KEY = "your-key-here"
```
Without a key, the agent runs fully offline on heuristics alone (same
heuristic-first / LLM-optional pattern as Milestones 1–3).

## Run the MCP server standalone (for inspection)

```powershell
python filesystem_mcp_server.py
```
This blocks, speaking JSON-RPC 2.0 over stdin/stdout — it's meant to be
spawned by a client, not run interactively. To poke at it interactively,
use the official MCP Inspector:

```powershell
npx @modelcontextprotocol/inspector python filesystem_mcp_server.py
```

## Run the agent

```powershell
python matching_agent.py --resume-dir sample_data/resumes `
    --jd "Looking for a backend engineer skilled in Python, FastAPI, Docker, and PostgreSQL"
```

The agent spawns **both** MCP servers itself (`filesystem_mcp_server.py`
and `skills_db_mcp_server.py`) as subprocesses over stdio — nothing extra
needs to be running first.

Sample output:
```
# Resume Matching Report

**Job description:** Looking for a backend engineer skilled in Python, FastAPI, Docker, and PostgreSQL
**Required skills detected:** python, fastapi, docker, postgresql
**LLM reasoning layer:** disabled (heuristic-only)
**Resumes discovered:** 3
**Resumes successfully loaded:** 3

## Ranked Candidates
1. `.../resume_alice.txt` — score 103.3 (base 100.0)
   - matched: ['python', 'fastapi', 'docker', 'postgresql']
   - missing: none
2. `.../resume_carla.txt` — score 26.0 (base 25.0)
   - matched: ['python']
   - missing: ['fastapi', 'docker', 'postgresql']
3. `.../resume_bob.txt` — score 0.0 (base 0.0)
   - matched: none
   - missing: ['python', 'fastapi', 'docker', 'postgresql']
   - explicitly disclaimed: ['python']
```
Note Bob's resume ("No professional Python experience yet") is correctly
excluded rather than false-matched on the keyword "Python" — the
negation-window heuristic (and, if enabled, Gemini's full-sentence
reasoning) catches it, same as the Milestone 3 lesson this carries forward.

## Run the tests

```powershell
python tests/test_mcp_server.py        # 10/10 scenarios — MCP server only
python tests/test_agent_integration.py # 6/6 scenarios  — agent + both MCP servers
```

## What changed vs. Milestone 1 / Milestone 3

- **Milestone 1** exposed `fs_tools.py` functions to Gemini via native
  function-calling. Here, the *same* functions are exposed via the
  standardized MCP tool interface instead — any MCP-compatible client
  (not just this one agent) can now discover and call them.
- **Milestone 3**'s agent imported file tools directly in-process. Here,
  every file operation is a JSON-RPC 2.0 call to a separate server
  process, enforced by an explicit sandbox check
  (`_enforce_sandbox`) that a direct-import architecture didn't need
  (and couldn't enforce as cleanly) because there was no process boundary.
- **New in this milestone:** `watch_directory()` (diff-based directory
  polling with an optional `watchdog`-backed background observer) and
  `batch_process()` (concurrent multi-file operations, partial-failure
  tolerant), plus a second MCP server to show multi-MCP orchestration.

## Known simplifications / what to swap in for production

- `fs_tools.py` here is self-contained per the docstring at its top —
  drop in your real Milestone 1 file to use it unmodified, as long as the
  8 function signatures listed there are preserved.
- `_SKILL_ALIASES` in `matching_agent.py` is a small hand-built dictionary
  standing in for Milestone 2's embedding/ChromaDB-based matcher, to keep
  this milestone's demo runnable without your real M2 index. Swap in a
  call to your real Milestone 2 module by replacing `score_resume()`.
- `skills_db_mcp_server.py`'s market-demand numbers are illustrative seed
  data, not a live feed — the point is the multi-MCP wiring, not the data
  source.
