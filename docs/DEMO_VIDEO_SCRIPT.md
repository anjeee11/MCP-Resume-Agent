# Demo Video Script — Milestone 4 (target: 5:30)

## 0:00–0:40 — Intro & framing
"This is Milestone 4: converting the Milestone 1 file-system tools into a
real MCP server, and refactoring the Milestone 3 LangGraph agent to use it
through an MCP client instead of direct imports. I'll show the server on
its own first, then the refactored agent, then the multi-MCP bonus."

Show: `README.md` file tree at a glance (VS Code explorer or `ls`).

## 0:40–1:40 — The MCP server, standalone
Run:
```
npx @modelcontextprotocol/inspector python filesystem_mcp_server.py
```
- Point out the **Tools** tab: all 8 Milestone 1 tools plus
  `watch_directory` and `batch_process`.
- Point out the **Resources** tab: `config://server`,
  `directory://{path}`, `watch-state://{path}` — this is the "resource
  discovery" requirement.
- Call `list_directory` on `sample_data/resumes` live, show the JSON
  result.
- Call `read_file` on a path outside the sandbox root (e.g. `/etc/passwd`)
  to show the protocol-level error (`isError: true`, sandbox message) —
  contrast with calling `read_file` on a nonexistent file inside the
  sandbox, which comes back as a normal `{"success": false}` result, not
  a crash. Narrate the two-layer error handling from `ARCHITECTURE.md`.

## 1:40–2:40 — The new capabilities
- Call `watch_directory` on `sample_data/watched_resumes` twice: once for
  the baseline (`is_first_poll: true`), then drop a new `.txt` file in
  via a terminal `echo`, call it again, show `new_files` populated.
- Call `batch_process` with `operation: "get_file_info"` over all three
  seed resumes, show `succeeded/failed` counts and `elapsed_seconds` —
  narrate that this ran concurrently via a thread pool, not sequentially.

## 2:40–3:10 — Run the automated test suite
```
python tests/test_mcp_server.py
```
Let all "10/10 scenarios passed" scroll by; briefly narrate 2–3 of the
scenario names on screen (resource discovery, sandbox rejection, batch
partial-failure tolerance).

## 3:10–4:40 — The refactored agent
Open `matching_agent.py` briefly:
- Show `SERVER_COMMANDS` — two servers, filesystem + skills_db.
- Show one node (`node_load_resumes`) and narrate: "this used to call
  `fs_tools.read_file` in-process; now it's `manager.call("filesystem",
  "batch_process", ...)` — a real JSON-RPC round trip."
- Show `ARCHITECTURE.md`'s Mermaid state diagram rendered (VS Code Mermaid
  preview or GitHub preview).

Run it live:
```
python matching_agent.py --resume-dir sample_data/resumes --jd "Looking for a backend engineer skilled in Python, FastAPI, Docker, and PostgreSQL"
```
Narrate the ranked output as it prints: Alice #1 (all 4 skills), Carla
partial, **Bob correctly excluded because his resume says "No
professional Python experience yet"** — same negation-handling lesson
carried forward from Milestone 3, now running through MCP instead of
direct calls.

## 4:40–5:15 — Bonus: multi-MCP integration
Point at the `market_weighted_score` vs `base_score` difference in the
output. Explain: that gap comes from `skills_db_mcp_server.py` — a
*second*, completely separate MCP server, queried mid-graph
(`enrich_market_context` node) for skill demand data. Show
`skills_db_mcp_server.py`'s tool list quickly in the inspector if time
allows.

Run:
```
python tests/test_agent_integration.py
```
Let "6/6 scenarios passed" show.

## 5:15–5:30 — Wrap-up
"So: same Milestone 1 capabilities, same Milestone 3 agent logic, but now
everything crosses a real MCP/JSON-RPC boundary, with two independent
servers, resource discovery, sandboxed error handling, and two new
MCP-native capabilities. Code and docs are in the repo."
