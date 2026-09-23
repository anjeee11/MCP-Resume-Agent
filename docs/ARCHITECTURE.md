# Milestone 4 — Architecture: Agent ↔ MCP Interaction

## 1. System overview

```mermaid
flowchart TB
    subgraph Agent["matching_agent.py  (LangGraph state machine)"]
        A1[extract_requirements]
        A2[discover_resumes]
        A3[load_resumes]
        A4[enrich_market_context]
        A5[score_resumes]
        A6[build_report]
        A1 --> A2 --> A3 --> A4 --> A5 --> A6
    end

    subgraph MCM["mcp_client_manager.py"]
        C1[ClientSession: filesystem]
        C2[ClientSession: skills_db]
    end

    subgraph FS["filesystem_mcp_server.py  (stdio, JSON-RPC 2.0)"]
        FST["Tools:\nlist_directory, read_file, write_file,\nsearch_files, get_file_info, delete_file,\nmove_file, create_directory,\nwatch_directory, batch_process"]
        FSR["Resources:\nconfig://server\ndirectory://{path}\nwatch-state://{path}"]
        FS1[fs_tools.py]
        FST --> FS1
    end

    subgraph SDB["skills_db_mcp_server.py  (stdio, JSON-RPC 2.0)"]
        SDT["Tools:\nget_skill_demand,\nlist_tracked_skills"]
        SDR["Resource:\nskills-db://catalog"]
        SDT --> SQLITE[(skills_market.db)]
    end

    A2 -- "watch_directory / list_directory" --> C1
    A3 -- "batch_process" --> C1
    A4 -- "get_skill_demand" --> C2
    C1 <--> |JSON-RPC 2.0 over stdio| FST
    C2 <--> |JSON-RPC 2.0 over stdio| SDT
```

## 2. Why two MCP servers instead of one

`filesystem_mcp_server.py` owns everything I/O-shaped: reading resumes,
watching a directory, batch operations. `skills_db_mcp_server.py` owns a
completely unrelated capability (skills market intelligence) and is a
separate process with its own tool namespace and its own resource. The
agent (`matching_agent.py`) doesn't know or care that they're implemented
differently under the hood — it just holds two `ClientSession`s through
`MCPClientManager` and calls whichever server owns the capability it needs.
This is the point of MCP: **capabilities are servers, not imports.**
Swapping `skills_db_mcp_server.py` for a real jobs-market API tomorrow
means editing one file; `matching_agent.py` doesn't change.

## 3. State machine — one agent run

```mermaid
stateDiagram-v2
    [*] --> ExtractRequirements
    ExtractRequirements --> DiscoverResumes: required_skills[] parsed from JD
    DiscoverResumes --> LoadResumes: watch_directory() baseline + list_directory()
    LoadResumes --> EnrichMarketContext: batch_process(read_file) over all discovered paths
    EnrichMarketContext --> ScoreResumes: get_skill_demand() per required skill (2nd MCP server)
    ScoreResumes --> BuildReport: heuristic soft-penalty score, optional Gemini LLM sanity-check
    BuildReport --> [*]: ranked markdown report

    note right of DiscoverResumes
        MCP call: filesystem.watch_directory
        MCP call: filesystem.list_directory
    end note
    note right of LoadResumes
        MCP call: filesystem.batch_process
        (concurrent reads, partial-failure tolerant)
    end note
    note right of EnrichMarketContext
        MCP call: skills_db.get_skill_demand
        (2nd, independent MCP server)
    end note
```

## 4. Request/response lifecycle for a single tool call

```mermaid
sequenceDiagram
    participant Node as LangGraph node<br/>(e.g. load_resumes)
    participant Mgr as MCPClientManager
    participant Sess as ClientSession
    participant Srv as filesystem_mcp_server.py

    Node->>Mgr: await manager.call("filesystem", "batch_process", {...})
    Mgr->>Sess: session.call_tool("batch_process", args)
    Sess->>Srv: JSON-RPC 2.0 request (tools/call)  [stdio]
    Srv->>Srv: _enforce_sandbox() on every path
    Srv->>Srv: ThreadPoolExecutor fan-out over fs_tools.read_file
    Srv-->>Sess: JSON-RPC 2.0 response (CallToolResult)
    Sess-->>Mgr: CallToolResult(isError, content)
    alt isError == True (protocol-level failure)
        Mgr-->>Node: raise MCPToolError
    else isError == False
        Mgr-->>Node: parsed {"success", "data", "error"} dict
    end
```

## 5. Error-handling layers (why there are two)

| Layer | Example | Surfaced as |
|---|---|---|
| Protocol-level | path outside sandbox, unsupported `batch_process` operation, malformed args | `McpError` → `CallToolResult(isError=True)` → `MCPToolError` raised to the agent |
| Business-level | file not found, file already exists without `overwrite=True` | Normal tool result: `{"success": false, "error": "..."}` — the call succeeded, the *operation* didn't |

Keeping these separate means a single bad resume file (business-level)
never aborts a batch, but a client trying to read `/etc/passwd`
(protocol-level abuse) is stopped hard, with a real JSON-RPC error, not a
silently-ignored request.

## 6. Configuration management

`filesystem_mcp_server.py`'s `ServerConfig` is driven entirely by
environment variables (`MCP_FS_ROOT`, `MCP_FS_MAX_BATCH`,
`MCP_FS_BATCH_WORKERS`, `MCP_FS_WATCH_POLL`), so the exact same server
binary can be sandboxed to a CI temp dir, a demo folder, or Anjeee's real
resume drop folder without touching code. The active configuration is
itself discoverable at runtime via the `config://server` MCP resource.
