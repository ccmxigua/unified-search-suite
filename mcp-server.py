#!/usr/bin/env python3
"""MCP server exposing unified_search as a tool for ACP agents (Claude CLI etc.)."""

import json
import math
import re
import subprocess
import sys
import os
from typing import Literal
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

def _run(args: list[str], timeout: float | None = None) -> str:
    """Run a native route with a shared, bounded subprocess result contract."""
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
    if timeout is not None:
        if not math.isfinite(timeout) or timeout <= 0:
            return _invalid("timeout must be a finite positive number")
        configured_search_timeout = timeout
        timeout_seconds = min(timeout_seconds, timeout + 5)
    # Leave time for the wrapper to serialize a structured result before the
    # MCP subprocess hard timeout fires.
    env["UNIFIED_SEARCH_TIMEOUT_SECONDS"] = str(
        min(configured_search_timeout, max(0.01, timeout_seconds - 5.0))
    )
    if args[0] != "--":
        args = [args[0], "--timeout", env["UNIFIED_SEARCH_TIMEOUT_SECONDS"], *args[1:]]

    try:
        result = subprocess.run(
            ["bash", UNIFIED_SEARCH_BIN, *args],
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

    try:
        search_result = json.loads(result.stdout or "")
    except json.JSONDecodeError:
        search_result = result.stdout or ""
    if result.returncode != 0:
        return json.dumps({
            "status": "error",
            "returncode": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "result": search_result,
        }, ensure_ascii=False)
    if isinstance(search_result, dict) and "status" not in search_result:
        search_result["status"] = "error" if search_result.get("error") or search_result.get("ok") is False else "partial" if search_result.get("truncated") else "success"
    if (result.stderr or "").strip():
        provider_errors = sorted({
            name.lower() for name in re.findall(
                r"^\[(exa|tavily|grok|tinyfish)\]\s+error:",
                result.stderr or "",
                re.I | re.M,
            )
        })
        status = search_result.get("status") if isinstance(search_result, dict) else None
        if status not in {"success", "empty", "partial", "error", "timeout"}:
            status = "partial" if provider_errors else "success"
        elif provider_errors and status in {"success", "empty"}:
            status = "partial"
        return json.dumps({
            # Preserve the search layer's status, including reference failures.
            "status": status,
            "result": search_result,
            "diagnostics": result.stderr.strip(),
            "provider_errors": provider_errors,
        }, ensure_ascii=False)
    return json.dumps(search_result, ensure_ascii=False) if isinstance(search_result, dict) else result.stdout.strip()


def _invalid(message):
    return json.dumps({"status": "error", "error": {"code": "invalid_arguments", "message": message}})


@mcp.tool()
def unified_search(
    query: str,
    mode: Literal["auto", "fast", "deep", "answer"] = "auto",
    source: str | None = None,
    num: int | None = None,
    intent: Literal["factual", "status", "comparison", "tutorial", "exploratory", "news", "resource"] | None = None,
    freshness: Literal["pd", "pw", "pm", "py"] | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    read_top: int = 0,
    content_max_chars: int = 12000,
    content_timeout: float = 30,
    content_fallback: Literal["none", "mineru"] = "none",
    timeout: float | None = None,
) -> str:
    """Search the web with optional hard domain/date filters and top-result full text.

    Dates are inclusive UTC YYYY-MM-DD; undated results are excluded with date
    filters. Hard filters use Exa/Tavily and omit unverified answer synthesis.
    source is a comma-separated list of exa,tavily,grok,tinyfish. read_top is 0-10.
    MinerU fallback is opt-in and requires a configured token. mode=auto retains
    automatic intent/query expansion when no search options are supplied.
    """
    if not query.strip():
        return _invalid("query must not be empty")
    if mode not in {"auto", "fast", "deep", "answer"} or content_fallback not in {"none", "mineru"}:
        return _invalid("Unsupported mode or content fallback")
    if num is not None and not 1 <= num <= 20:
        return _invalid("num must be 1-20")
    if not 0 <= read_top <= 10 or not 1 <= content_max_chars <= 200000 or not math.isfinite(content_timeout) or content_timeout <= 0:
        return _invalid("Invalid content limits")
    if content_fallback != "none" and not read_top:
        return _invalid("content_fallback requires read_top")
    if source is not None and (not source.strip() or any(s.strip() not in {"exa", "tavily", "grok", "tinyfish"} for s in source.split(","))):
        return _invalid("Unknown search source")
    options = []
    for name, value in (("source", source), ("num", num), ("intent", intent), ("freshness", freshness),
                        ("include-domains", ",".join(include_domains) if include_domains else None),
                        ("exclude-domains", ",".join(exclude_domains) if exclude_domains else None),
                        ("start-date", start_date), ("end-date", end_date)):
        if value is not None:
            options.append(f"--{name}={value}")
    if read_top:
        options.extend([f"--read-top={read_top}", f"--content-max-chars={content_max_chars}",
                        f"--content-timeout={content_timeout}", f"--content-fallback={content_fallback}"])
    if not options and mode == "auto":
        return _run(["--", query], timeout)
    return _run(["search-layer", "--mode", "deep" if mode == "auto" else mode, *options, "--", query], timeout)


@mcp.tool()
def extract_content(url: str, timeout: float = 30, max_chars: int = 20000,
                    fallback: Literal["none", "mineru"] = "none") -> str:
    """Extract public HTTP(S) HTML or PDF text, with quality/attempt metadata.

    Explicit fallback=mineru may call the configured external MinerU API for
    failed or short local extraction. Text PDFs use local extraction without OCR;
    cloud errors preserve resumable task handles. max_chars is 1-200000.
    """
    if not 1 <= max_chars <= 200000 or fallback not in {"none", "mineru"}:
        return _invalid("Invalid extraction limits or fallback")
    return _run(["content-extract", f"--url={url}", f"--max-chars={max_chars}", f"--fallback={fallback}"], timeout)


@mcp.tool()
def fetch_thread(url: str, max_comments: int = 100, timeout: float = 60) -> str:
    """Fetch a public issue, PR, or discussion with comments and references.

    max_comments is 1-500; HN has a 200-comment tree ceiling. Truncation and
    fetch errors are retained in the structured response.
    """
    if not 1 <= max_comments <= 500:
        return _invalid("max_comments must be 1-500")
    return _run(["fetch-thread", "--format", "json", "--max-comments", str(max_comments), "--", url], timeout)


if __name__ == "__main__":
    mcp.run(transport="stdio")
