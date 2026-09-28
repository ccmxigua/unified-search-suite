#!/usr/bin/env python3
"""MCP server exposing unified_search as a tool for ACP agents (Claude CLI etc.)."""

import json
import math
import re
import subprocess
import sys
import os
from mcp.server.fastmcp import FastMCP

UNIFIED_SEARCH_BIN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "scripts",
    "unified-search.sh",
)

mcp = FastMCP("unified_search", instructions="""
unified_search — Multi-source web search optimized for Chinese and English queries.
Use this for web searches, information gathering, and fact-checking.
""")

@mcp.tool(
    name="unified_search",
    description=(
        "Search the web using the configured search providers. "
        "Optimized for Chinese and English queries. "
        "Use this for information gathering, fact-checking, research, and current events."
    ),
)
def unified_search(query: str) -> str:
    """Run unified-search.sh with the given query and return results."""
    env = os.environ.copy()
    env.setdefault("OPENCLAW_CONFIG", os.path.expanduser("~/.openclaw/openclaw.json"))
    try:
        timeout_seconds = float(env.get("UNIFIED_SEARCH_MCP_TIMEOUT_SECONDS", "120"))
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
    except (TypeError, ValueError):
        timeout_seconds = 120.0
    try:
        configured_search_timeout = float(env.get("UNIFIED_SEARCH_TIMEOUT_SECONDS", "105"))
        if not math.isfinite(configured_search_timeout) or configured_search_timeout <= 0:
            configured_search_timeout = 105.0
    except (TypeError, ValueError):
        configured_search_timeout = 105.0
    # Leave time for the wrapper to serialize a structured result before the
    # MCP subprocess hard timeout fires.
    env["UNIFIED_SEARCH_TIMEOUT_SECONDS"] = str(
        min(configured_search_timeout, max(0.01, timeout_seconds - 5.0))
    )

    try:
        result = subprocess.run(
            ["bash", UNIFIED_SEARCH_BIN, query],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return json.dumps({
            "status": "timeout",
            "timeout_seconds": timeout_seconds,
            "stdout": stdout,
            "stderr": stderr,
        }, ensure_ascii=False)

    if result.returncode != 0:
        return json.dumps({
            "status": "error",
            "returncode": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
        }, ensure_ascii=False)
    if (result.stderr or "").strip():
        try:
            search_result = json.loads(result.stdout or "")
        except json.JSONDecodeError:
            search_result = result.stdout or ""
        provider_errors = sorted({
            name.lower() for name in re.findall(
                r"^\[(exa|tavily|grok|tinyfish)\]\s+error:",
                result.stderr or "",
                re.I | re.M,
            )
        })
        return json.dumps({
            # Generic stderr diagnostics do not prove that results are
            # incomplete. Mark partial only for explicit provider failures.
            "status": "partial" if provider_errors else "success",
            "result": search_result,
            "diagnostics": result.stderr.strip(),
            "provider_errors": provider_errors,
        }, ensure_ascii=False)
    return result.stdout.strip()


if __name__ == "__main__":
    mcp.run(transport="stdio")
