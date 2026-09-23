"""
matching_agent.py
===================
Milestone 4 — Part B: Agent Refactoring.

This is the Milestone 3 LangGraph resume-matching agent, refactored so it
no longer imports `fs_tools` directly. All file-system access now goes
through `filesystem_mcp_server.py` via `MCPClientManager`, over the real
MCP/JSON-RPC 2.0 protocol. Matching/scoring logic (Milestone 2-ish) is kept
in-process since it's pure computation, not I/O — there's no protocol
reason to put it behind MCP, and doing so would just add latency without
adding a real capability boundary.

BONUS — Multi-MCP integration: the agent also connects to a SECOND, wholly
independent MCP server (`skills_db_mcp_server.py`, a skills-market-demand
database) and blends its data into scoring. This demonstrates the same
agent orchestrating tools across multiple MCP servers in one workflow,
exactly like a real deployment would mix a filesystem server with a
database or search server.

Design carried over from Milestone 3 (see project memory / README):
  * Heuristic-first, LLM-optional: works fully offline; Gemini reasoning
    kicks in automatically only if GOOGLE_API_KEY is set.
  * Soft-penalty scoring, not hard AND-filters: a resume missing a
    "nice-to-have" skill loses points, it doesn't get zeroed out.
  * Word-boundary (`\\b`) regex for skill/alias matching, so short aliases
    like "ts" don't false-match inside unrelated words like "tests".
  * LLM reasoning is used to catch negative-context mentions ("No
    professional Python experience yet") that pure keyword matching would
    misread as a hit.

Usage:
    python3 matching_agent.py --resume-dir sample_data/resumes \\
        --jd "Looking for a backend engineer skilled in Python, FastAPI, Docker"
"""

import argparse
import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, END

from mcp_client_manager import MCPClientManager, MCPToolError

try:
    from google import genai
    from google.genai import types as genai_types
    _GEMINI_SDK_AVAILABLE = True
except ImportError:
    _GEMINI_SDK_AVAILABLE = False


# --------------------------------------------------------------------------
# MCP server registry — everything the agent depends on lives here, so
# swapping a server implementation is a one-line change.
# --------------------------------------------------------------------------

import sys

SERVER_COMMANDS = {
    "filesystem": [sys.executable, "filesystem_mcp_server.py"],
    "skills_db": [sys.executable, "skills_db_mcp_server.py"],
}


# --------------------------------------------------------------------------
# Agent state
# --------------------------------------------------------------------------

class AgentState(TypedDict, total=False):
    job_description: str
    resume_dir: str
    required_skills: List[str]
    discovered_files: List[str]
    resume_contents: Dict[str, str]
    load_errors: List[str]
    scores: List[Dict[str, Any]]
    market_context: Dict[str, Dict[str, Any]]
    llm_enabled: bool
    final_report: str


# --------------------------------------------------------------------------
# Heuristic-first matching helpers (Milestone 2/3 logic, unchanged)
# --------------------------------------------------------------------------

# canonical skill -> aliases; word-boundary regex avoids "ts" matching
# inside "tests" (a bug fixed in Milestone 2 and carried forward here).
_SKILL_ALIASES = {
    "python": ["python"],
    "typescript": ["typescript", "ts"],
    "javascript": ["javascript", "js"],
    "fastapi": ["fastapi", "fast api"],
    "docker": ["docker"],
    "kubernetes": ["kubernetes", "k8s"],
    "aws": ["aws", "amazon web services"],
    "react": ["react", "react.js", "reactjs"],
    "postgresql": ["postgresql", "postgres"],
    "pytorch": ["pytorch"],
    "tensorflow": ["tensorflow"],
    "sql": ["sql"],
    "langgraph": ["langgraph"],
    "langchain": ["langchain"],
    "mlops": ["mlops"],
    "figma": ["figma"],
    "css": ["css"],
    "go": ["golang", "go"],
    "rust": ["rust"],
    "chromadb": ["chromadb", "chroma"],
    "rag": ["rag", "retrieval augmented generation"],
}


def extract_required_skills(job_description: str) -> List[str]:
    """Pull canonical skill names mentioned in the JD, via the same
    word-boundary alias matching used for resumes."""
    found = []
    for canonical, aliases in _SKILL_ALIASES.items():
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b", job_description, flags=re.IGNORECASE):
                found.append(canonical)
                break
    return found


_NEGATION_WINDOW = re.compile(
    r"\b(no|not|none|without|lack(?:ing|s)?)\b[^.]{0,40}", re.IGNORECASE
)


def _skill_hits_with_negation_check(text: str, alias: str) -> Optional[bool]:
    """Return True (positive hit), False (negated hit, e.g. 'No professional
    Python experience'), or None (no mention at all)."""
    pattern = re.compile(rf"\b{re.escape(alias)}\b", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return None
    # Look at a window of text *before* the match for a negation cue within
    # the same sentence-ish span.
    window_start = max(0, match.start() - 60)
    preceding = text[window_start:match.start()]
    if _NEGATION_WINDOW.search(preceding):
        return False
    return True


def score_resume(content: str, required_skills: List[str]) -> Dict[str, Any]:
    """Soft-penalty scoring: missing skills reduce the score, they never
    zero it out (no hard AND-filter — see Milestone 2 lesson)."""
    hits, misses, negated = [], [], []
    for skill in required_skills:
        aliases = _SKILL_ALIASES.get(skill, [skill])
        outcome = None
        for alias in aliases:
            outcome = _skill_hits_with_negation_check(content, alias)
            if outcome is not None:
                break
        if outcome is True:
            hits.append(skill)
        elif outcome is False:
            negated.append(skill)
            misses.append(skill)
        else:
            misses.append(skill)

    total = max(1, len(required_skills))
    base_score = round(100 * len(hits) / total, 1)
    return {
        "matched_skills": hits,
        "missing_skills": misses,
        "negated_skills": negated,  # skills explicitly disclaimed, e.g. "no Python experience"
        "base_score": base_score,
    }


# --------------------------------------------------------------------------
# Optional Gemini reasoning layer (LLM-optional, activates iff key present)
# --------------------------------------------------------------------------

def gemini_available() -> bool:
    return _GEMINI_SDK_AVAILABLE and bool(os.environ.get("GOOGLE_API_KEY"))


def llm_sanity_check(resume_content: str, heuristic_result: Dict[str, Any]) -> Dict[str, Any]:
    """Ask Gemini to double-check negated/borderline skills using full-
    sentence reasoning instead of keyword windows. Falls back to the
    heuristic result untouched if Gemini isn't configured or errors out."""
    if not gemini_available():
        return heuristic_result

    try:
        client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        prompt = f"""You are reviewing a resume for skill claims.
Resume:
---
{resume_content}
---
Heuristically-detected matched skills: {heuristic_result['matched_skills']}
Heuristically-detected missing/negated skills: {heuristic_result['missing_skills']}

For each heuristically-matched skill, confirm the candidate genuinely
claims that skill (reason over full sentences, not keywords -- e.g. "No
professional Python experience yet" is NOT a Python claim even though the
word appears). Respond ONLY as JSON: {{"confirmed_skills": [...], "rejected_skills": [...]}}"""
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(response_mime_type="application/json"),
        )
        parsed = json.loads(response.text)
        confirmed = parsed.get("confirmed_skills", heuristic_result["matched_skills"])
        rejected = parsed.get("rejected_skills", [])
        merged_missing = sorted(set(heuristic_result["missing_skills"]) | set(rejected))
        return {
            **heuristic_result,
            "matched_skills": confirmed,
            "missing_skills": merged_missing,
            "llm_reviewed": True,
        }
    except Exception as exc:
        return {**heuristic_result, "llm_reviewed": False, "llm_error": str(exc)}


# --------------------------------------------------------------------------
# LangGraph nodes — every filesystem interaction now goes through MCP
# --------------------------------------------------------------------------

async def node_extract_requirements(state: AgentState, manager: MCPClientManager) -> AgentState:
    skills = extract_required_skills(state["job_description"])
    return {**state, "required_skills": skills}


async def node_discover_resumes(state: AgentState, manager: MCPClientManager) -> AgentState:
    """MCP call #1: watch_directory establishes/refreshes a baseline so any
    resumes dropped in since the last run are picked up automatically,
    then list_directory returns the current file set to actually process."""
    await manager.call("filesystem", "watch_directory", {
        "directory": state["resume_dir"], "enable_background_watch": False,
    })
    listing = await manager.call("filesystem", "list_directory", {
        "path": state["resume_dir"], "pattern": "*.txt",
    })
    files = [e["path"] for e in listing["data"]["entries"]] if listing["success"] else []
    return {**state, "discovered_files": files}


async def node_load_resumes(state: AgentState, manager: MCPClientManager) -> AgentState:
    """MCP call #2: batch_process reads every discovered resume concurrently
    through the MCP server in a single tool call, instead of N separate
    read_file round-trips."""
    files = state["discovered_files"]
    if not files:
        return {**state, "resume_contents": {}, "load_errors": ["No resumes discovered."]}

    batch = await manager.call("filesystem", "batch_process", {
        "paths": files, "operation": "read_file",
    })
    contents, errors = {}, []
    for item in batch["data"]["results"]:
        if item["success"]:
            contents[item["path"]] = item["data"]["content"]
        else:
            errors.append(f"{item['path']}: {item['error']}")
    return {**state, "resume_contents": contents, "load_errors": errors}


async def node_enrich_market_context(state: AgentState, manager: MCPClientManager) -> AgentState:
    """BONUS multi-MCP step: query the SECOND MCP server (skills_db) for
    market-demand context on each required skill, independent of the
    filesystem server."""
    context: Dict[str, Dict[str, Any]] = {}
    for skill in state["required_skills"]:
        try:
            result = await manager.call("skills_db", "get_skill_demand", {"skill": skill})
            if result["success"]:
                context[skill] = result["data"]
        except MCPToolError:
            continue
    return {**state, "market_context": context}


async def node_score_resumes(state: AgentState, manager: MCPClientManager) -> AgentState:
    llm_on = gemini_available()
    scores = []
    for path, content in state["resume_contents"].items():
        heuristic = score_resume(content, state["required_skills"])
        result = llm_sanity_check(content, heuristic) if llm_on else heuristic

        # Weight final score by market demand of matched skills (bonus signal
        # sourced from the second MCP server), still soft — never zeroing out.
        demand_bonus = 0.0
        for skill in result["matched_skills"]:
            info = state.get("market_context", {}).get(skill)
            if info:
                demand_bonus += info["demand_score"] / 100.0
        weighted = round(result["base_score"] + demand_bonus, 1)

        scores.append({
            "resume": path,
            **result,
            "market_weighted_score": weighted,
        })

    scores.sort(key=lambda r: r["market_weighted_score"], reverse=True)
    return {**state, "scores": scores, "llm_enabled": llm_on}


async def node_build_report(state: AgentState, manager: MCPClientManager) -> AgentState:
    lines = [
        "# Resume Matching Report",
        "",
        f"**Job description:** {state['job_description']}",
        f"**Required skills detected:** {', '.join(state['required_skills']) or '(none detected)'}",
        f"**LLM reasoning layer:** {'ENABLED (Gemini)' if state.get('llm_enabled') else 'disabled (heuristic-only)'}",
        f"**Resumes discovered:** {len(state['discovered_files'])}",
        f"**Resumes successfully loaded:** {len(state['resume_contents'])}",
        "",
        "## Ranked Candidates",
    ]
    for rank, entry in enumerate(state["scores"], start=1):
        lines.append(
            f"{rank}. `{entry['resume']}` — score {entry['market_weighted_score']} "
            f"(base {entry['base_score']})\n"
            f"   - matched: {entry['matched_skills'] or 'none'}\n"
            f"   - missing: {entry['missing_skills'] or 'none'}"
            + (f"\n   - explicitly disclaimed: {entry['negated_skills']}" if entry.get("negated_skills") else "")
        )
    if state.get("load_errors"):
        lines += ["", "## Load Errors", *[f"- {e}" for e in state["load_errors"]]]

    report = "\n".join(lines)
    return {**state, "final_report": report}


# --------------------------------------------------------------------------
# Graph assembly
# --------------------------------------------------------------------------

def build_graph(manager: MCPClientManager) -> Any:
    """Bind the MCP manager into each node via closures, then compile the
    LangGraph state machine:

        extract_requirements -> discover_resumes -> load_resumes
            -> enrich_market_context -> score_resumes -> build_report -> END
    """
    graph = StateGraph(AgentState)

    # functools.partial preserves `asyncio.iscoroutinefunction`, unlike a
    # plain `lambda s: coro(s, manager)` wrapper (which LangGraph would see
    # as a *sync* function that happens to return an unawaited coroutine).
    import functools

    graph.add_node("extract_requirements", functools.partial(node_extract_requirements, manager=manager))
    graph.add_node("discover_resumes", functools.partial(node_discover_resumes, manager=manager))
    graph.add_node("load_resumes", functools.partial(node_load_resumes, manager=manager))
    graph.add_node("enrich_market_context", functools.partial(node_enrich_market_context, manager=manager))
    graph.add_node("score_resumes", functools.partial(node_score_resumes, manager=manager))
    graph.add_node("build_report", functools.partial(node_build_report, manager=manager))

    graph.set_entry_point("extract_requirements")
    graph.add_edge("extract_requirements", "discover_resumes")
    graph.add_edge("discover_resumes", "load_resumes")
    graph.add_edge("load_resumes", "enrich_market_context")
    graph.add_edge("enrich_market_context", "score_resumes")
    graph.add_edge("score_resumes", "build_report")
    graph.add_edge("build_report", END)

    return graph.compile()


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

async def run_agent(job_description: str, resume_dir: str) -> str:
    async with MCPClientManager(SERVER_COMMANDS) as manager:
        graph = build_graph(manager)
        initial_state: AgentState = {
            "job_description": job_description,
            "resume_dir": resume_dir,
        }
        final_state = await graph.ainvoke(initial_state)
        return final_state["final_report"]


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP-backed LangGraph resume matching agent")
    parser.add_argument("--resume-dir", required=True, help="Directory of resumes to scan")
    parser.add_argument("--jd", required=True, help="Job description text")
    args = parser.parse_args()

    report = asyncio.run(run_agent(args.jd, args.resume_dir))
    print(report)


if __name__ == "__main__":
    main()
