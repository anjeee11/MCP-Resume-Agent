"""
skills_db_mcp_server.py
=========================
Milestone 4 — Part B, Bonus: a SECOND, independent MCP server, used to
demonstrate the agent talking to multiple MCP servers at once (filesystem +
this one), each with its own process, its own tools, and its own resources.

This one is a tiny "skills market intelligence" database: given a skill
name, it returns a demand score and a one-line market note. In a real
deployment this could be backed by a jobs-market API or a warehouse table;
here it's a small in-memory SQLite database seeded at startup so the demo
is fully offline and reproducible.

Exposes:
    TOOL      get_skill_demand(skill: str) -> demand score + note
    TOOL      list_tracked_skills() -> every skill this DB knows about
    RESOURCE  skills-db://catalog -> full catalog as a browsable resource
"""

import json
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DB_PATH = Path(__file__).parent / "skills_market.db"

_SEED_DATA = [
    ("python", 96, "Consistently top-3 most-requested language across backend and ML job postings."),
    ("fastapi", 78, "Fast-growing; increasingly preferred over Flask for new Python services."),
    ("langgraph", 71, "Emerging but rising sharply as agentic-workflow adoption grows."),
    ("langchain", 74, "Widely used for LLM app scaffolding; strong demand in AI-focused roles."),
    ("react", 90, "Dominant frontend framework; near-universal requirement for frontend roles."),
    ("typescript", 85, "Now expected alongside React/JS in most frontend and full-stack postings."),
    ("javascript", 88, "Baseline requirement for essentially all frontend roles."),
    ("docker", 82, "Standard expectation for backend/DevOps roles."),
    ("kubernetes", 75, "Common at mid-to-large companies; less so at early-stage startups."),
    ("aws", 87, "Most in-demand cloud platform across job postings."),
    ("pytorch", 80, "Leading deep-learning framework in ML job postings."),
    ("tensorflow", 62, "Still present but losing share to PyTorch in new postings."),
    ("sql", 91, "Near-universal requirement across data and backend roles."),
    ("postgresql", 73, "Most commonly named relational database in backend job postings."),
    ("mlops", 68, "Growing demand as companies operationalize ML models."),
    ("chromadb", 40, "Niche but growing alongside the RAG/vector-DB ecosystem."),
    ("rag", 66, "Increasingly named explicitly in AI engineering job postings."),
    ("figma", 58, "Common design-collaboration requirement for frontend/design-adjacent roles."),
    ("css", 84, "Baseline frontend requirement."),
    ("go", 69, "Growing in infra/backend roles, especially at scale-focused companies."),
    ("rust", 55, "Niche but growing, especially in systems and performance-critical roles."),
]


def _init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS skills (
            skill TEXT PRIMARY KEY,
            demand_score INTEGER NOT NULL,
            note TEXT NOT NULL
        )
    """)
    conn.executemany(
        "INSERT OR REPLACE INTO skills (skill, demand_score, note) VALUES (?, ?, ?)",
        _SEED_DATA,
    )
    conn.commit()
    conn.close()


_init_db()

mcp = FastMCP("skills-market-mcp-server")


@mcp.tool()
def get_skill_demand(skill: str) -> dict:
    """Look up market-demand info for a single skill (case-insensitive).
    Returns {"success": False} if the skill isn't tracked yet, so the
    caller can fall back to a neutral weighting rather than crashing."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT skill, demand_score, note FROM skills WHERE lower(skill) = ?",
        (skill.lower().strip(),),
    ).fetchone()
    conn.close()
    if row is None:
        return {"success": False, "data": None, "error": f"'{skill}' is not a tracked skill"}
    return {"success": True, "data": {"skill": row[0], "demand_score": row[1], "note": row[2]}, "error": None}


@mcp.tool()
def list_tracked_skills() -> dict:
    """Return every skill this database currently tracks, with scores."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT skill, demand_score, note FROM skills ORDER BY demand_score DESC").fetchall()
    conn.close()
    return {
        "success": True,
        "error": None,
        "data": [{"skill": r[0], "demand_score": r[1], "note": r[2]} for r in rows],
    }


@mcp.resource("skills-db://catalog")
def catalog_resource() -> str:
    """Full skills catalog exposed as a browsable MCP resource."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT skill, demand_score, note FROM skills ORDER BY skill").fetchall()
    conn.close()
    data = [{"skill": r[0], "demand_score": r[1], "note": r[2]} for r in rows]
    return json.dumps(data, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
