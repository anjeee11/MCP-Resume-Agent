"""
tests/test_mcp_server.py
=========================
End-to-end test scenarios for filesystem_mcp_server.py.

These are integration tests: they spawn the real MCP server as a
subprocess over stdio (exactly how the LangGraph agent will talk to it)
and drive it through the actual JSON-RPC 2.0 protocol using the official
`mcp` client SDK. Nothing is mocked.

Run:
    python3 tests/test_mcp_server.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "sample_data" / "resumes"
WATCH_DIR = ROOT / "sample_data" / "watched_resumes"

PASS, FAIL = "PASS", "FAIL"
_results = []


def record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" -- {detail}" if detail else ""))


async def run_scenarios():
    server_env = dict(os.environ)
    server_env["MCP_FS_ROOT"] = str(ROOT)  # sandbox to the project dir

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "filesystem_mcp_server.py")],
        env=server_env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ---- Scenario 1: resource discovery -----------------------
            resources = await session.list_resource_templates()
            names = {r.name for r in resources.resourceTemplates}
            record(
                "Scenario 1: resource discovery lists config + directory + watch-state",
                {"directory_resource", "watch_state_resource"} <= names,
                f"found={names}",
            )

            tools = await session.list_tools()
            tool_names = {t.name for t in tools.tools}
            expected = {
                "list_directory", "read_file", "write_file", "search_files",
                "get_file_info", "delete_file", "move_file", "create_directory",
                "watch_directory", "batch_process",
            }
            record(
                "Scenario 1b: tool discovery exposes all 8 Milestone-1 tools + 2 new ones",
                expected <= tool_names,
                f"missing={expected - tool_names}",
            )

            # ---- Scenario 2: read config resource ----------------------
            cfg = await session.read_resource("config://server")
            cfg_data = json.loads(cfg.contents[0].text)
            record(
                "Scenario 2: config resource reports sandbox root",
                str(ROOT) in cfg_data["allowed_roots"][0] or cfg_data["allowed_roots"][0] == str(ROOT),
                f"allowed_roots={cfg_data['allowed_roots']}",
            )

            # ---- Scenario 3: list_directory tool call -------------------
            listing = await session.call_tool("list_directory", {"path": str(SAMPLE_DIR)})
            listing_data = json.loads(listing.content[0].text)
            record(
                "Scenario 3: list_directory finds 3 seeded resumes",
                listing_data["success"] and listing_data["data"]["count"] == 3,
                f"count={listing_data['data']['count'] if listing_data['success'] else listing_data['error']}",
            )

            # ---- Scenario 4: read_file tool call -------------------------
            target = str(SAMPLE_DIR / "resume_alice.txt")
            read_result = await session.call_tool("read_file", {"path": target})
            read_data = json.loads(read_result.content[0].text)
            record(
                "Scenario 4: read_file returns Alice's resume content",
                read_data["success"] and "Alice Chen" in read_data["data"]["content"],
            )

            # ---- Scenario 5: search_files tool call ----------------------
            search_result = await session.call_tool(
                "search_files", {"directory": str(SAMPLE_DIR), "query": "LangGraph"}
            )
            search_data = json.loads(search_result.content[0].text)
            record(
                "Scenario 5: search_files finds Carla via content match",
                search_data["success"] and search_data["data"]["count"] == 1
                and "carla" in search_data["data"]["matches"][0]["path"].lower(),
            )

            # ---- Scenario 6: logical error surfaces as tool result, not crash --
            missing = await session.call_tool("read_file", {"path": str(SAMPLE_DIR / "nope.txt")})
            missing_data = json.loads(missing.content[0].text)
            record(
                "Scenario 6: reading a missing file returns success=False, not a protocol crash",
                missing_data["success"] is False and "does not exist" in missing_data["error"],
            )

            # ---- Scenario 7: protocol-level error (sandbox escape attempt) --
            # McpError raised inside a tool handler surfaces as a CallToolResult
            # with isError=True (per MCP spec: tool errors are reported IN the
            # result so the calling model can see and react to them), rather
            # than as a raw JSON-RPC transport exception.
            escape = await session.call_tool("read_file", {"path": "/etc/passwd"})
            record(
                "Scenario 7: sandbox escape is rejected as a tool-level protocol error",
                escape.isError and "outside allowed sandbox roots" in escape.content[0].text,
                escape.content[0].text[:120],
            )

            # ---- Scenario 8: watch_directory baseline + poll diff ----------
            WATCH_DIR.mkdir(parents=True, exist_ok=True)
            for f in WATCH_DIR.glob("*"):
                f.unlink()

            baseline = await session.call_tool(
                "watch_directory", {"directory": str(WATCH_DIR), "enable_background_watch": False}
            )
            baseline_data = json.loads(baseline.content[0].text)
            record(
                "Scenario 8a: watch_directory first call is a baseline snapshot",
                baseline_data["success"] and baseline_data["data"]["is_first_poll"] is True,
            )

            (WATCH_DIR / "new_resume.txt").write_text("New Candidate\nSkills: Go, Rust\n")
            poll2 = await session.call_tool(
                "watch_directory", {"directory": str(WATCH_DIR), "enable_background_watch": False}
            )
            poll2_data = json.loads(poll2.content[0].text)
            record(
                "Scenario 8b: watch_directory second poll detects the new file",
                poll2_data["success"] and "new_resume.txt" in poll2_data["data"]["new_files"],
                f"new_files={poll2_data['data']['new_files']}",
            )

            # ---- Scenario 9: batch_process across all resumes --------------
            all_paths = [str(p) for p in SAMPLE_DIR.glob("*.txt")]
            batch = await session.call_tool(
                "batch_process", {"paths": all_paths, "operation": "get_file_info"}
            )
            batch_data = json.loads(batch.content[0].text)
            record(
                "Scenario 9: batch_process processes all resumes concurrently, all succeed",
                batch_data["success"] and batch_data["data"]["succeeded"] == len(all_paths)
                and batch_data["data"]["failed"] == 0,
                f"succeeded={batch_data['data']['succeeded']}/{len(all_paths)} "
                f"in {batch_data['data']['elapsed_seconds']}s",
            )

            # ---- Scenario 10: batch_process rejects an unsupported op -------
            bad_op = await session.call_tool(
                "batch_process", {"paths": all_paths, "operation": "write_file"}
            )
            record(
                "Scenario 10: batch_process rejects unsupported operation",
                bad_op.isError and "operation must be one of" in bad_op.content[0].text,
                bad_op.content[0].text[:120],
            )

    print()
    passed = sum(1 for _, s, _ in _results if s == PASS)
    print(f"== {passed}/{len(_results)} scenarios passed ==")
    return passed == len(_results)


if __name__ == "__main__":
    ok = asyncio.run(run_scenarios())
    sys.exit(0 if ok else 1)
