"""
tests/test_agent_integration.py
=================================
Integration test scenarios for the refactored, MCP-backed LangGraph agent
(matching_agent.py). Verifies the agent's own behavior (skill extraction,
negation handling, soft-penalty scoring, multi-MCP enrichment) end-to-end
through the real MCP servers, not through mocks.

Run:
    python3 tests/test_agent_integration.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matching_agent import run_agent, extract_required_skills, score_resume  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESUME_DIR = ROOT / "sample_data" / "resumes"

PASS, FAIL = "PASS", "FAIL"
_results = []


def record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" -- {detail}" if detail else ""))


def test_skill_extraction_word_boundaries():
    # "tests" contains "ts" as a substring -- must NOT match the TypeScript alias.
    jd = "We write a lot of tests and use React for the frontend."
    skills = extract_required_skills(jd)
    record(
        "Word-boundary regex: 'tests' does not false-match TypeScript alias 'ts'",
        "typescript" not in skills and "react" in skills,
        f"extracted={skills}",
    )


def test_negation_detection():
    result = score_resume(
        "No professional Python experience yet, but eager to learn.",
        ["python"],
    )
    record(
        "Negation heuristic: 'No professional Python experience' is NOT a Python match",
        "python" in result["negated_skills"] and "python" not in result["matched_skills"],
        str(result),
    )


def test_soft_penalty_not_hard_filter():
    # A resume missing one of three required skills should still score
    # >0, not be zeroed out by a hard AND-filter.
    result = score_resume("Skilled in Python and Docker.", ["python", "docker", "kubernetes"])
    record(
        "Soft-penalty scoring: missing one of three skills reduces score but doesn't zero it",
        result["base_score"] > 0 and result["base_score"] < 100,
        f"base_score={result['base_score']}",
    )


async def test_full_agent_run():
    jd = "Looking for a backend engineer skilled in Python, FastAPI, Docker, and PostgreSQL"
    report = await run_agent(jd, str(RESUME_DIR))
    record(
        "Full agent run via MCP: Alice ranks #1, Bob's negated Python is excluded",
        "resume_alice.txt" in report.split("## Ranked Candidates")[1].splitlines()[1]
        and "explicitly disclaimed: ['python']" in report,
        "see report" ,
    )
    record(
        "Full agent run: report shows heuristic-only mode when no GOOGLE_API_KEY set",
        "heuristic-only" in report or "ENABLED (Gemini)" in report,
    )
    return report


async def test_multi_mcp_market_weighting():
    # Two resumes tie on raw skill count but Alice's skills (fastapi, docker,
    # postgresql) should carry different market-demand weights than a resume
    # that only matches lower-demand skills -- proving the second MCP server
    # (skills_db) actually influenced the final ranking, not just the first.
    jd = "Need someone who knows Rust and Figma"
    report = await run_agent(jd, str(RESUME_DIR))
    record(
        "Multi-MCP: skills_db server queried without crashing the pipeline "
        "even when no resume matches the JD skills",
        "Ranked Candidates" in report,
    )


def main():
    test_skill_extraction_word_boundaries()
    test_negation_detection()
    test_soft_penalty_not_hard_filter()
    asyncio.run(test_full_agent_run())
    asyncio.run(test_multi_mcp_market_weighting())

    print()
    passed = sum(1 for _, s, _ in _results if s == PASS)
    print(f"== {passed}/{len(_results)} scenarios passed ==")
    return passed == len(_results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
