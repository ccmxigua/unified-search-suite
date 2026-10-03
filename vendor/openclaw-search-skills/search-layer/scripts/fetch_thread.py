#!/usr/bin/env python3
"""
fetch-thread: Deep content fetcher for threaded discussions.

Fetches a URL's full discussion thread + extracts structured references.
GitHub-optimized (API for issues/PRs/discussions), with generic web fallback.

Usage:
  python3 fetch_thread.py <url> [--max-comments 100] [--extract-refs] [--format json|markdown]

Output (JSON):
  {
    "url": "...",
    "type": "github_issue|github_pr|github_discussion|web_page",
    "title": "...",
    "body": "...",
    "state": "open|closed|merged",
    "labels": [...],
    "comments": [{"author": "...", "date": "...", "body": "...", "reactions": {...}}],
    "refs": [{"type": "issue|pr|commit|url|duplicate", "url": "...", "context": "..."}],
    "metadata": {"created": "...", "updated": "...", "author": "...", "comment_count": N}
  }
"""

import json
import sys
import os
import re
import argparse
import ipaddress
import math
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone
import time

_REQUEST_DEADLINE = None
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_WEB_BODY_BYTES = 5 * 1024 * 1024
MAX_COMMENTS = 500


def _validate_http_url(url: str) -> None:
    """Reject non-web schemes and obvious local-network targets."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https and include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs containing embedded credentials are not allowed")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("local hostnames are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("non-public IP addresses are not allowed")


class _ValidatedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_urlopen(req: Request, timeout: float):
    _validate_http_url(req.full_url)
    return build_opener(_ValidatedRedirectHandler()).open(req, timeout=timeout)


def set_request_deadline(deadline):
    global _REQUEST_DEADLINE
    _REQUEST_DEADLINE = deadline


def _bounded_timeout(timeout):
    if _REQUEST_DEADLINE is None:
        return timeout
    remaining = _REQUEST_DEADLINE - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("request deadline exhausted")
    return min(timeout, remaining)


def _mark_truncated(result: dict, reason: str) -> None:
    result["truncated"] = True
    reasons = result.setdefault("truncation_reasons", [])
    if reason not in reasons:
        reasons.append(reason)


def _iter_nested_comments(nodes: list):
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        yield node
        yield from _iter_nested_comments(node.get("replies", []))


# ---------------------------------------------------------------------------
# HTTP helper (stdlib only, no requests dependency)
# ---------------------------------------------------------------------------
def _http_get(url: str, headers: dict | None = None,
              params: dict | None = None, timeout: int = 20) -> dict:
    """Simple GET returning {"status": int, "json": ..., "text": ...}.
    Raises on network errors; does NOT raise on 4xx/5xx (check status)."""
    if params:
        from urllib.parse import urlencode
        sep = "&" if "?" in url else "?"
        url = url + sep + urlencode(params)
    _validate_http_url(url)
    req = Request(url, method="GET")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with _safe_urlopen(req, timeout=_bounded_timeout(timeout)) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError(f"HTTP response exceeds {MAX_RESPONSE_BYTES} bytes")
            body = raw.decode("utf-8", errors="replace")
            status = resp.status
        error_body_truncated = False
    except HTTPError as e:
        raw = e.read(MAX_RESPONSE_BYTES + 1) if e.fp else b""
        error_body_truncated = len(raw) > MAX_RESPONSE_BYTES
        body = raw[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace")
        status = e.code
    result = {"status": status, "text": body}
    if error_body_truncated:
        result["truncated"] = True
    try:
        result["json"] = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        result["json"] = None
    return result


# ---------------------------------------------------------------------------
# GitHub token discovery
# ---------------------------------------------------------------------------
def _find_github_token() -> str | None:
    """Find GitHub token from env or git-credentials."""
    if tok := os.environ.get("GITHUB_TOKEN"):
        return tok
    if tok := os.environ.get("GH_TOKEN"):
        return tok
    cred_path = os.path.expanduser("~/.git-credentials")
    if os.path.isfile(cred_path):
        try:
            with open(cred_path) as f:
                for line in f:
                    line = line.strip()
                    if "github.com" in line:
                        # Format: https://user:token@github.com
                        match = re.search(r'://[^:]+:([^@]+)@github\.com', line)
                        if match:
                            return match.group(1)
        except Exception:
            pass
    return None


def _gh_headers(token: str | None) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ---------------------------------------------------------------------------
# Reference extraction from text
# ---------------------------------------------------------------------------
# Patterns for extracting references from markdown/text
_REF_PATTERNS = [
    # GitHub issue/PR references: #123, owner/repo#123, GH-123
    (r'(?:^|[\s(])(?:([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+))?#(\d+)(?=[\s).,;:!?\']|$)',
     'issue_ref'),
    (r'(?:^|[\s(])GH-(\d+)(?=[\s).,;:!?\']|$)', 'gh_ref'),
    # Full GitHub URLs
    (r'https?://github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)/(issues|pull|discussions)/(\d+)(?:#[^\s)]*)?',
     'github_url'),
    # Commit references: full SHA or short SHA in context
    (r'https?://github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)/commit/([0-9a-f]{7,40})',
     'commit_url'),
    (r'(?:^|[\s(])([0-9a-f]{40})(?=[\s).,;:!?\']|$)', 'full_sha'),
    # Duplicate markers
    (r'(?i)(?:duplicate\s+of|duplicates?|dup(?:licate)?(?:\s+of)?)\s+#(\d+)', 'duplicate'),
    (r'(?i)(?:duplicate\s+of|duplicates?)\s+https?://github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)/(issues|pull)/(\d+)',
     'duplicate_url'),
    # "See also", "Related", "Fixes", "Closes" references
    (r'(?i)(?:see\s+also|related(?:\s+to)?|fixes|closes|resolves|refs?)\s+#(\d+)', 'related_ref'),
    # Generic URLs (non-GitHub)
    (r'(?<!\S)(https?://(?!github\.com)[^\s<>\[\]()\'\"]+)', 'external_url'),
]


def extract_refs(text: str, repo_context: str = "") -> list:
    """Extract structured references from text.

    Args:
        text: The text to scan for references.
        repo_context: Default "owner/repo" for bare #123 references.

    Returns:
        List of {"type": ..., "url": ..., "context": ...} dicts.
    """
    if not text:
        return []

    refs = []
    seen_refs = set()

    def _add(ref_type: str, url: str, context: str = ""):
        canon = url.rstrip("/")
        identity = (canon, ref_type)
        if identity not in seen_refs:
            seen_refs.add(identity)
            refs.append({"type": ref_type, "url": url, "context": context})

    for pattern, kind in _REF_PATTERNS:
        for m in re.finditer(pattern, text, re.MULTILINE):
            # Get surrounding context (up to 80 chars around match)
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 40)
            ctx = text[start:end].replace("\n", " ").strip()

            if kind == 'issue_ref':
                repo = m.group(1) or repo_context
                num = m.group(2)
                if repo:
                    url = f"https://github.com/{repo}/issues/{num}"
                    _add("issue", url, ctx)

            elif kind == 'gh_ref':
                num = m.group(1)
                if repo_context:
                    url = f"https://github.com/{repo_context}/issues/{num}"
                    _add("issue", url, ctx)

            elif kind == 'github_url':
                repo = m.group(1)
                url_type = m.group(2)  # issues, pull, discussions
                num = m.group(3)
                full_url = f"https://github.com/{repo}/{url_type}/{num}"
                ref_type = {"issues": "issue", "pull": "pr",
                            "discussions": "discussion"}.get(url_type, "issue")
                _add(ref_type, full_url, ctx)

            elif kind == 'commit_url':
                repo = m.group(1)
                sha = m.group(2)
                url = f"https://github.com/{repo}/commit/{sha}"
                _add("commit", url, ctx)

            elif kind == 'full_sha':
                sha = m.group(1)
                if repo_context:
                    url = f"https://github.com/{repo_context}/commit/{sha}"
                    _add("commit", url, ctx)

            elif kind == 'duplicate':
                num = m.group(1)
                if repo_context:
                    url = f"https://github.com/{repo_context}/issues/{num}"
                    _add("duplicate", url, ctx)

            elif kind == 'duplicate_url':
                repo = m.group(1)
                url_type = m.group(2)
                num = m.group(3)
                url = f"https://github.com/{repo}/{url_type}/{num}"
                _add("duplicate", url, ctx)

            elif kind == 'related_ref':
                num = m.group(1)
                if repo_context:
                    url = f"https://github.com/{repo_context}/issues/{num}"
                    _add("related", url, ctx)

            elif kind == 'external_url':
                url = m.group(1).rstrip(".,;:!?")
                # Skip image URLs and common non-reference URLs
                if not re.search(r'\.(png|jpg|jpeg|gif|svg|ico|webp)(\?|$)', url, re.I):
                    _add("url", url, ctx)

    return refs


# ---------------------------------------------------------------------------
# GitHub API fetchers
# ---------------------------------------------------------------------------
def _parse_github_url(url: str) -> dict | None:
    """Parse a GitHub URL into components.
    Returns {"owner", "repo", "type", "number"} or None."""
    parsed = urlparse(url)
    if parsed.hostname not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 4:
        return None
    owner, repo = parts[0], parts[1]
    url_type = parts[2]  # issues, pull, discussions
    try:
        number = int(parts[3])
    except (ValueError, IndexError):
        return None
    type_map = {"issues": "issue", "pull": "pr", "discussions": "discussion"}
    gh_type = type_map.get(url_type)
    if not gh_type:
        return None
    return {"owner": owner, "repo": repo, "type": gh_type, "number": number}


def fetch_github_issue(owner: str, repo: str, number: int,
                       token: str | None, max_comments: int = 100) -> dict:
    """Fetch a GitHub issue with all comments via REST API."""
    max_comments = max(1, min(MAX_COMMENTS, int(max_comments)))
    base = f"https://api.github.com/repos/{owner}/{repo}"
    headers = _gh_headers(token)
    result = {
        "url": f"https://github.com/{owner}/{repo}/issues/{number}",
        "type": "github_issue",
        "title": "",
        "body": "",
        "state": "",
        "labels": [],
        "comments": [],
        "refs": [],
        "metadata": {},
    }
    repo_ctx = f"{owner}/{repo}"

    # Fetch issue
    try:
        r = _http_get(f"{base}/issues/{number}", headers=headers, timeout=20)
        if r["status"] >= 400:
            result["error"] = f"GitHub API {r['status']}: {r['text'][:200]}"
            if r.get("truncated"):
                _mark_truncated(result, "http_error_body_limit_reached")
            return result
        issue = r["json"]
    except Exception as e:
        result["error"] = f"Failed to fetch issue: {e}"
        return result

    # Check if it's actually a PR (GitHub API returns PRs via /issues/ too)
    if issue.get("pull_request"):
        result["type"] = "github_pr"
        result["url"] = f"https://github.com/{owner}/{repo}/pull/{number}"

    result["title"] = issue.get("title", "")
    result["body"] = issue.get("body", "") or ""
    result["state"] = issue.get("state", "")
    if issue.get("pull_request", {}).get("merged_at"):
        result["state"] = "merged"
    result["labels"] = [l.get("name", "") for l in issue.get("labels", [])]
    result["metadata"] = {
        "author": issue.get("user", {}).get("login", ""),
        "created": issue.get("created_at", ""),
        "updated": issue.get("updated_at", ""),
        "comment_count": issue.get("comments", 0),
        "reactions": _extract_reactions(issue.get("reactions", {})),
    }

    # Extract refs from body
    all_text = result["body"]

    # Fetch comments (paginated)
    page = 1
    per_page = min(max_comments, 100)
    fetched = 0
    comments_fetch_failed = False
    while fetched < max_comments:
        try:
            r = _http_get(
                f"{base}/issues/{number}/comments",
                headers=headers,
                params={"page": page, "per_page": per_page},
                timeout=20,
            )
            if r["status"] >= 400:
                print(f"[fetch-thread] comment page {page} error: HTTP {r['status']}", file=sys.stderr)
                comments_fetch_failed = True
                break
            comments = r["json"]
        except Exception as e:
            print(f"[fetch-thread] comment page {page} error: {e}", file=sys.stderr)
            comments_fetch_failed = True
            break

        if not comments:
            break

        for c in comments:
            body = c.get("body", "") or ""
            result["comments"].append({
                "author": c.get("user", {}).get("login", ""),
                "date": c.get("created_at", ""),
                "body": body,
                "reactions": _extract_reactions(c.get("reactions", {})),
            })
            all_text += "\n" + body
            fetched += 1
            if fetched >= max_comments:
                break

        if len(comments) < per_page:
            break
        page += 1

    result["metadata"]["fetched_comment_count"] = fetched
    expected_comments = result["metadata"].get("comment_count") or 0
    if expected_comments > fetched:
        if comments_fetch_failed:
            reason = "comments_fetch_incomplete"
        elif fetched >= max_comments:
            reason = "comments_limit_reached"
        else:
            reason = "comment_count_exceeds_fetched_count"
        _mark_truncated(
            result,
            reason,
        )

    # If it's a PR, also fetch review summaries within the same output cap.
    if result["type"] == "github_pr":
        review_page = 1
        review_fetched = 0
        review_budget = max_comments - len(result["comments"])
        if review_budget <= 0:
            _mark_truncated(result, "pr_review_summaries_not_fetched_due_comment_limit")
        while review_fetched < review_budget:
            per_review_page = min(100, review_budget - review_fetched)
            try:
                r = _http_get(
                    f"{base}/pulls/{number}/reviews",
                    headers=headers,
                    params={"per_page": per_review_page, "page": review_page},
                    timeout=20,
                )
                if r["status"] >= 400 or not isinstance(r["json"], list):
                    _mark_truncated(result, "review_comments_fetch_incomplete")
                    break
                reviews = r["json"]
                if not reviews:
                    break
                for review in reviews:
                    body = review.get("body", "") or ""
                    if body.strip():
                        if len(result["comments"]) < max_comments:
                            result["comments"].append({
                                "author": review.get("user", {}).get("login", ""),
                                "date": review.get("submitted_at", ""),
                                "body": f"[Review: {review.get('state', 'COMMENTED')}] {body}",
                                "reactions": {},
                            })
                            all_text += "\n" + body
                review_fetched += len(reviews)
                if len(reviews) < per_review_page:
                    break
                review_page += 1
            except Exception:
                _mark_truncated(result, "review_comments_fetch_incomplete")
                break
        if review_budget > 0 and review_fetched >= review_budget:
            _mark_truncated(result, "review_summaries_may_be_truncated_at_limit")

    # Extract all refs from combined text
    result["refs"] = extract_refs(all_text, repo_ctx)

    # Also check for timeline events (duplicate markers, cross-references)
    _enrich_with_timeline(owner, repo, number, token, result, repo_ctx)

    return result


def _extract_reactions(reactions: dict) -> dict:
    """Extract non-zero reaction counts."""
    keys = ["+1", "-1", "laugh", "hooray", "confused", "heart", "rocket", "eyes"]
    return {k: reactions.get(k, 0) for k in keys if reactions.get(k, 0) > 0}


def _enrich_with_timeline(owner: str, repo: str, number: int,
                          token: str | None, result: dict, repo_ctx: str):
    """Fetch issue timeline events for cross-references and duplicate markers."""
    headers = _gh_headers(token)
    headers["Accept"] = "application/vnd.github.mockingbird-preview+json"
    try:
        r = _http_get(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/timeline",
            headers=headers,
            params={"per_page": 100},
            timeout=20,
        )
        if r["status"] != 200:
            return
        events = r["json"]
        if isinstance(events, list) and len(events) >= 100:
            _mark_truncated(result, "timeline_may_be_truncated_after_100_events")
    except Exception:
        return

    for event in events:
        etype = event.get("event", "")

        if etype == "cross-referenced":
            source = event.get("source", {}).get("issue", {})
            if source:
                src_repo = source.get("repository", {}).get("full_name", repo_ctx)
                src_num = source.get("number")
                src_type = "pr" if source.get("pull_request") else "issue"
                if src_num:
                    url = f"https://github.com/{src_repo}/{'pull' if src_type == 'pr' else 'issues'}/{src_num}"
                    result["refs"].append({
                        "type": f"cross_ref_{src_type}",
                        "url": url,
                        "context": f"Referenced by {src_repo}#{src_num}: {source.get('title', '')}",
                    })

        elif etype == "marked_as_duplicate":
            # The canonical issue info isn't always in the event, but we already
            # catch "Duplicate of #X" in text extraction
            pass

        elif etype in ("referenced", "connected"):
            commit = event.get("commit_id")
            if commit:
                url = f"https://github.com/{repo_ctx}/commit/{commit}"
                result["refs"].append({
                    "type": "commit",
                    "url": url,
                    "context": f"Referenced in commit {commit[:7]}",
                })

    # Deduplicate refs
    seen = set()
    unique_refs = []
    for ref in result["refs"]:
        key = ref["url"].rstrip("/")
        if key not in seen:
            seen.add(key)
            unique_refs.append(ref)
    result["refs"] = unique_refs


# ---------------------------------------------------------------------------
# Generic web page fetcher (fallback)
# ---------------------------------------------------------------------------
def _apply_web_fallback(result: dict, fallback: dict, reason: str) -> None:
    """Merge generic-page recovery without mislabeling recovered content as a total failure."""
    result["title"] = result.get("title") or fallback.get("title", "")
    result["body"] = fallback.get("body", "")
    result["links"] = fallback.get("links", [])
    result["refs"] = fallback.get("refs", [])
    result["fallback_from"] = {"provider": "platform_api", "reason": reason[:300]}
    _mark_truncated(result, "platform_api_failed_web_fallback_used")
    if fallback.get("truncated"):
        fallback_reasons = fallback.get("truncation_reasons") or ["web_fallback_truncated"]
        for truncation_reason in fallback_reasons:
            _mark_truncated(result, truncation_reason)
    if fallback.get("error"):
        result["error"] = f"Platform API failed ({reason[:200]}); web fallback failed: {fallback['error'][:200]}"
    elif not result["title"] and not result["body"]:
        result["error"] = f"Platform API failed ({reason[:200]}); web fallback returned no content"


def fetch_v2ex(url: str, max_comments: int = 100) -> dict:
    """Fetch a V2EX topic via API and extract structured content."""
    max_comments = max(1, min(MAX_COMMENTS, int(max_comments)))
    result = {
        "url": url,
        "type": "v2ex_topic",
        "title": "",
        "body": "",
        "state": None,
        "labels": [],
        "comments": [],
        "refs": [],
        "links": [],
        "metadata": {},
    }

    # Extract topic ID from URL: v2ex.com/t/123456
    m = re.search(r'v2ex\.com/t/(\d+)', url)
    if not m:
        result["error"] = "Cannot parse V2EX topic ID from URL"
        return result

    topic_id = m.group(1)

    try:
        # Try V2EX API v1 (no auth required)
        topic_data = _http_get(
            f"https://www.v2ex.com/api/topics/show.json?id={topic_id}",
            headers={"User-Agent": "fetch-thread/1.0"})

        if topic_data["status"] == 200 and topic_data["json"]:
            topics = topic_data["json"]
            topic = topics[0] if isinstance(topics, list) and topics else {}
            result["title"] = topic.get("title", "")
            result["body"] = topic.get("content", "")
            result["metadata"] = {
                "author": topic.get("member", {}).get("username", ""),
                "created": topic.get("created", ""),
                "reply_count": topic.get("replies", 0),
                "node": topic.get("node", {}).get("name", ""),
            }

            # Replies via v1 API
            replies_data = _http_get(
                f"https://www.v2ex.com/api/replies/show.json?topic_id={topic_id}",
                headers={"User-Agent": "fetch-thread/1.0"})
            if replies_data["status"] == 200 and replies_data["json"]:
                raw_replies = replies_data["json"] or []
                if not isinstance(raw_replies, list):
                    raw_replies = []
                for r in raw_replies[:max_comments]:
                    result["comments"].append({
                        "author": r.get("member", {}).get("username", ""),
                        "date": r.get("created", ""),
                        "body": r.get("content", ""),
                    })
                if len(raw_replies) > max_comments:
                    _mark_truncated(result, "comments_limit_reached")
                result["metadata"]["fetched_comment_count"] = len(result["comments"])
                if result["metadata"].get("reply_count", 0) > len(result["comments"]):
                    reason = (
                        "comments_limit_reached"
                        if len(result["comments"]) >= max_comments
                        else "reply_count_exceeds_fetched_count"
                    )
                    _mark_truncated(result, reason)

            all_text = result["body"] + " " + " ".join(c["body"] for c in result["comments"])
            result["refs"] = extract_refs(all_text)
        else:
            suffix = " (error body truncated)" if topic_data.get("truncated") else ""
            raise Exception(f"V2EX API returned {topic_data['status']}{suffix}")

    except Exception as e:
        fallback = fetch_web_page(url)
        _apply_web_fallback(result, fallback, f"V2EX API failed: {e}")

    return result


def fetch_hn(url: str, max_comments: int = 200) -> dict:
    """Fetch a Hacker News item via Algolia API (no auth required)."""
    max_comments = max(1, min(200, int(max_comments)))
    result = {
        "url": url,
        "type": "hn_item",
        "title": "",
        "body": "",
        "state": None,
        "labels": [],
        "comments": [],
        "refs": [],
        "links": [],
        "metadata": {},
    }

    # Extract item ID: news.ycombinator.com/item?id=12345
    m = re.search(r'[?&]id=(\d+)', url)
    if not m:
        result["error"] = "Cannot parse HN item ID from URL"
        return result

    item_id = m.group(1)

    try:
        data = _http_get(
            f"https://hn.algolia.com/api/v1/items/{item_id}",
            headers={"User-Agent": "fetch-thread/1.0"})

        if data["status"] != 200 or not data["json"]:
            suffix = " (error body truncated)" if data.get("truncated") else ""
            raise Exception(f"HN API returned {data['status']}{suffix}")

        item = data["json"]
        result["title"] = item.get("title", "") or item.get("story_title", "")
        result["body"] = item.get("text", "") or item.get("url", "")
        result["metadata"] = {
            "author": item.get("author", ""),
            "created": item.get("created_at", ""),
            "score": item.get("points", 0),
            "comment_count": item.get("num_comments", 0),
            "type": item.get("type", ""),
        }

        from html import unescape

        comment_budget = {"count": 0, "truncated": False}

        def _parse_hn_comment(node: dict, depth: int = 0) -> dict | None:
            if comment_budget["count"] >= max_comments:
                comment_budget["truncated"] = True
                return None
            if not node.get("author"):
                return None
            comment_budget["count"] += 1
            body = unescape(re.sub(r'<[^>]+>', ' ', node.get("text", "") or ""))
            body = re.sub(r'\s+', ' ', body).strip()
            c = {
                "author": node.get("author", ""),
                "date": node.get("created_at", ""),
                "body": body,
                "depth": depth,
            }
            children = []
            child_nodes = node.get("children") or []
            for index, child in enumerate(child_nodes):
                parsed = _parse_hn_comment(child, depth + 1)
                if parsed:
                    children.append(parsed)
                if comment_budget["count"] >= max_comments:
                    if index < len(child_nodes) - 1:
                        comment_budget["truncated"] = True
                    break
            if children:
                c["replies"] = children
            return c

        children = item.get("children") or []
        result["comments_tree"] = []
        for index, node in enumerate(children):
            parsed = _parse_hn_comment(node)
            if parsed:
                result["comments_tree"].append(parsed)
            if comment_budget["count"] >= max_comments:
                if index < len(children) - 1:
                    comment_budget["truncated"] = True
                break
        # backward-compat flat list (top-level only, max 50)
        result["comments"] = [
            {"author": c["author"], "date": c["date"], "body": c["body"]}
            for c in result["comments_tree"][:50]
        ]
        result["metadata"]["fetched_comment_count"] = comment_budget["count"]
        if comment_budget["truncated"]:
            _mark_truncated(result, "comments_limit_reached")
        if result["metadata"].get("comment_count", 0) > result["metadata"]["fetched_comment_count"]:
            if not comment_budget["truncated"]:
                _mark_truncated(result, "comment_count_exceeds_fetched_tree")

        all_text = result["body"] + " " + " ".join(
            c.get("body", "") for c in _iter_nested_comments(result["comments_tree"])
        )
        result["refs"] = extract_refs(all_text)

    except Exception as e:
        fallback = fetch_web_page(url)
        _apply_web_fallback(result, fallback, f"HN API failed: {e}")

    return result


def fetch_reddit(url: str, max_comments: int = 200) -> dict:
    """Fetch a Reddit post + comment tree via .json endpoint (no auth required)."""
    try:
        max_comments = max(1, min(MAX_COMMENTS, int(max_comments)))
    except (TypeError, ValueError, OverflowError):
        return {
            "url": url, "type": "web_page", "title": "", "body": "",
            "state": None, "labels": [], "comments": [], "refs": [],
            "links": [], "metadata": {}, "error": "max_comments must be an integer",
        }
    result = {
        "url": url,
        "type": "reddit_post",
        "title": "",
        "body": "",
        "state": None,
        "labels": [],
        "comments": [],
        "comments_tree": [],
        "refs": [],
        "links": [],
        "metadata": {},
    }

    # Build .json URL: strip query/fragment, append .json
    try:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        json_url = f"https://www.reddit.com{path}.json?limit=500&depth=4"
    except Exception as e:
        result["error"] = f"Failed to build Reddit JSON URL: {e}"
        return result

    try:
        data = _http_get(json_url, headers={
            "User-Agent": "fetch-thread/1.0 (research bot)",
            "Accept": "application/json",
        })

        if data["status"] != 200 or not data["json"]:
            suffix = " (error body truncated)" if data.get("truncated") else ""
            raise Exception(f"Reddit API returned {data['status']}{suffix}")

        listing = data["json"]
        # Reddit returns [post_listing, comments_listing]
        if not isinstance(listing, list) or len(listing) < 1:
            raise Exception("Unexpected Reddit JSON structure")

        post_data = listing[0]["data"]["children"][0]["data"]
        result["title"] = post_data.get("title", "")
        result["body"] = post_data.get("selftext", "") or post_data.get("url", "")
        result["metadata"] = {
            "author": post_data.get("author", ""),
            "created": post_data.get("created_utc", ""),
            "score": post_data.get("score", 0),
            "upvote_ratio": post_data.get("upvote_ratio", 0),
            "comment_count": post_data.get("num_comments", 0),
            "subreddit": post_data.get("subreddit", ""),
            "flair": post_data.get("link_flair_text", ""),
        }

        from html import unescape

        comment_budget = {"count": 0, "truncated": False}

        def _parse_comment(node: dict, depth: int = 0) -> dict | None:
            if node.get("kind") != "t1":
                return None
            if comment_budget["count"] >= max_comments:
                comment_budget["truncated"] = True
                return None
            comment_budget["count"] += 1
            d = node["data"]
            body = unescape(d.get("body", "") or "")
            c = {
                "author": d.get("author", ""),
                "date": d.get("created_utc", ""),
                "score": d.get("score", 0),
                "body": body,
                "depth": depth,
            }
            replies_data = d.get("replies")
            if isinstance(replies_data, dict):
                children = replies_data.get("data", {}).get("children", [])
                sub = []
                child_nodes = children if depth < 4 else []
                for index, child in enumerate(child_nodes):
                    parsed = _parse_comment(child, depth + 1)
                    if parsed:
                        sub.append(parsed)
                    if comment_budget["count"] >= max_comments:
                        if index < len(child_nodes) - 1:
                            comment_budget["truncated"] = True
                        break
                sub = [x for x in sub if x]
                if sub:
                    c["replies"] = sub
            return c

        comments_raw = listing[1]["data"]["children"] if len(listing) > 1 else []
        tree = []
        for index, node in enumerate(comments_raw):
            parsed = _parse_comment(node)
            if parsed:
                tree.append(parsed)
            if comment_budget["count"] >= max_comments:
                if index < len(comments_raw) - 1:
                    comment_budget["truncated"] = True
                break

        # cap total nodes
        def _flatten(nodes, acc):
            for n in nodes:
                if len(acc) >= max_comments:
                    return
                acc.append(n)
                _flatten(n.get("replies", []), acc)

        flat = []
        _flatten(tree, flat)

        result["comments_tree"] = tree
        result["comments"] = [
            {"author": c["author"], "date": c["date"], "body": c["body"]}
            for c in flat[:max_comments]
        ]
        result["metadata"]["fetched_comment_count"] = comment_budget["count"]
        if comment_budget["truncated"]:
            _mark_truncated(result, "comments_limit_reached")
        if result["metadata"].get("comment_count", 0) > len(result["comments"]):
            if not comment_budget["truncated"]:
                _mark_truncated(result, "comment_count_exceeds_fetched_count")

        all_text = result["body"] + " " + " ".join(c["body"] for c in result["comments"])
        result["refs"] = extract_refs(all_text)

    except Exception as e:
        fallback = fetch_web_page(url)
        _apply_web_fallback(result, fallback, f"Reddit API failed: {e}")

    return result


def _detect_platform(url: str) -> str:
    """Detect platform from URL."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host == "v2ex.com" or host.endswith(".v2ex.com"):
        return "v2ex"
    if host == "news.ycombinator.com":
        return "hn"
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if host == "reddit.com" or host.endswith(".reddit.com"):
        return "reddit"
    return "web"


def _extract_links_from_html(html: str, base_url: str = "") -> list:
    """Extract links from HTML with anchor_text and surrounding_text.

    Returns list of {"url": ..., "anchor": ..., "context": ...}
    Must be called BEFORE stripping tags.
    """
    links = []
    seen = set()

    try:
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
    except Exception:
        return links

    # Strip noisy sections first (nav, footer, ads)
    clean = re.sub(r'<(nav|footer|header|aside)[^>]*>.*?</\1>', '', html,
                   flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<script[^>]*>.*?</script>', '', clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<style[^>]*>.*?</style>', '', clean, flags=re.DOTALL | re.IGNORECASE)

    soup = BeautifulSoup(clean, 'lxml')

    for tag in soup.find_all('a', href=True):
        href = (tag.get('href') or '').strip()
        anchor = tag.get_text(separator=' ', strip=True)
        anchor = re.sub(r'\s+', ' ', anchor)

        # Skip empty anchors, images-only, javascript links
        if not href or not anchor or len(anchor) < 2:
            continue
        if href.startswith('javascript:'):
            continue

        resolved = urljoin(base_url, href)

        # Skip non-http links (mailto:, tel:, etc.)
        if not resolved.startswith('http'):
            continue

        # Skip image/asset URLs
        if re.search(r'\.(png|jpg|jpeg|gif|svg|ico|webp|css|js)(\?|$)', resolved, re.I):
            continue

        canon = resolved.rstrip('/')
        if canon in seen:
            continue
        seen.add(canon)

        parent_text = ""
        if tag.parent:
            parent_text = tag.parent.get_text(separator=' ', strip=True)
        context = re.sub(r'\s+', ' ', parent_text).strip()[:200]

        links.append({"url": resolved, "anchor": anchor, "context": context})

    return links


def fetch_web_page(url: str) -> dict:
    """Fetch a generic web page and extract references with anchor context."""
    result = {
        "url": url,
        "type": "web_page",
        "title": "",
        "body": "",
        "state": None,
        "labels": [],
        "comments": [],
        "refs": [],
        "links": [],  # enriched links with anchor_text + context
        "metadata": {},
    }

    try:
        req = Request(url, method="GET", headers={
            "User-Agent": "Mozilla/5.0 (compatible; fetch-thread/1.0)"
        })
        with _safe_urlopen(req, timeout=_bounded_timeout(20)) as resp:
            raw_html = resp.read(MAX_WEB_BODY_BYTES + 1)
        html_truncated = len(raw_html) > MAX_WEB_BODY_BYTES
        html = raw_html[:MAX_WEB_BODY_BYTES].decode("utf-8", errors="replace")
        if html_truncated:
            _mark_truncated(result, "html_body_byte_limit_reached")

        # Extract title
        title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
        if title_match:
            result["title"] = title_match.group(1).strip()

        # Extract enriched links BEFORE stripping tags
        result["links"] = _extract_links_from_html(html, base_url=url)

        body_text = ""

        # Layer 1: trafilatura extraction (preferred)
        try:
            import trafilatura
            extracted = trafilatura.extract(
                html,
                include_links=True,
                include_comments=False,
            )
            if extracted:
                body_text = re.sub(r'\s+', ' ', extracted).strip()
        except Exception:
            pass

        # Layer 2: BeautifulSoup fallback when extraction is missing/too short
        if len(body_text) < 200:
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, 'lxml')
                bs_text = soup.get_text(separator=' ')
                bs_text = re.sub(r'\s+', ' ', bs_text).strip()
                if bs_text:
                    body_text = bs_text
            except Exception:
                pass

        # Layer 3: legacy regex fallback
        if not body_text:
            text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            body_text = re.sub(r'\s+', ' ', text).strip()

        # Truncate to reasonable size
        if len(body_text) > 10000:
            _mark_truncated(result, "text_body_character_limit_reached")
        result["body"] = body_text[:10000]

        # Extract refs only from cleaned body text (reduce HTML noise)
        result["refs"] = extract_refs(result["body"])

    except Exception as e:
        result["error"] = f"Failed to fetch: {e}"

    return result


# ---------------------------------------------------------------------------
# Markdown output formatter
# ---------------------------------------------------------------------------
def format_markdown(data: dict) -> str:
    """Format fetch result as readable markdown."""
    lines = []
    lines.append(f"# {data.get('title', 'Untitled')}")
    lines.append(f"URL: {data['url']}")

    meta = data.get("metadata", {})
    if meta:
        parts = []
        if meta.get("author"):
            parts.append(f"Author: @{meta['author']}")
        if data.get("state"):
            parts.append(f"State: {data['state']}")
        if meta.get("created"):
            parts.append(f"Created: {meta['created']}")
        if meta.get("comment_count"):
            parts.append(f"Comments: {meta['comment_count']}")
        if parts:
            lines.append(" | ".join(parts))

    if data.get("labels"):
        lines.append(f"Labels: {', '.join(data['labels'])}")

    lines.append("")

    if data.get("truncated"):
        reasons = data.get("truncation_reasons") or ["unspecified_limit"]
        lines.append(f"⚠️ Content truncated: {', '.join(reasons)}")
        lines.append("")

    if data.get("body"):
        lines.append("## Body")
        lines.append(data["body"][:5000])
        lines.append("")

    if data.get("comments"):
        lines.append(f"## Comments ({len(data['comments'])})")
        for i, c in enumerate(data["comments"], 1):
            lines.append(f"### Comment {i} — @{c.get('author', '?')} ({c.get('date', '?')})")
            body = c.get("body", "")
            # Truncate very long comments
            if len(body) > 2000:
                body = body[:2000] + "\n... (truncated)"
            lines.append(body)
            if c.get("reactions"):
                lines.append(f"Reactions: {c['reactions']}")
            lines.append("")

    if data.get("refs"):
        lines.append(f"## References ({len(data['refs'])})")
        for ref in data["refs"]:
            ctx = f" — {ref['context']}" if ref.get("context") else ""
            lines.append(f"- [{ref['type']}] {ref['url']}{ctx}")
        lines.append("")

    if data.get("error"):
        lines.append(f"## Error\n{data['error']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def fetch_thread_url(url: str, max_comments: int = 100) -> dict:
    """Public API: fetch any URL and return structured data.

    Automatically detects platform (github/v2ex/hn/web) and routes accordingly.
    """
    try:
        _validate_http_url(url)
    except ValueError as exc:
        return {
            "url": url, "type": "web_page", "title": "", "body": "",
            "state": None, "labels": [], "comments": [], "refs": [],
            "links": [], "metadata": {}, "error": str(exc),
        }
    max_comments = max(1, min(MAX_COMMENTS, int(max_comments)))
    platform = _detect_platform(url)
    token = _find_github_token()

    if platform == "github":
        gh = _parse_github_url(url)
        if gh and gh["type"] in ("issue", "pr"):
            return fetch_github_issue(gh["owner"], gh["repo"], gh["number"],
                                      token, max_comments)
        elif gh and gh["type"] == "discussion":
            data = fetch_web_page(url)
            data["type"] = "github_discussion"
            return data
        else:
            return fetch_web_page(url)
    elif platform == "v2ex":
        return fetch_v2ex(url, max_comments)
    elif platform == "hn":
        return fetch_hn(url, max_comments)
    elif platform == "reddit":
        return fetch_reddit(url, max_comments)
    else:
        return fetch_web_page(url)


def main():
    ap = argparse.ArgumentParser(
        description="Fetch full discussion thread + extract references from a URL")
    ap.add_argument("url", help="URL to fetch (GitHub issue/PR/discussion or any web page)")
    ap.add_argument("--max-comments", type=int, default=100,
                    help="Max comments to fetch (default 100)")
    ap.add_argument("--timeout", type=float, default=105,
                    help="Overall request budget in seconds (default 105)")
    ap.add_argument("--extract-refs-only", action="store_true",
                    help="Only output the extracted references, not full thread")
    ap.add_argument("--format", choices=["json", "markdown"], default="json",
                    help="Output format (default: json)")
    args = ap.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        ap.error("--timeout must be a finite positive number")
    set_request_deadline(time.monotonic() + args.timeout)

    data = fetch_thread_url(args.url, args.max_comments)

    if args.extract_refs_only:
        output = {
            "url": data["url"],
            "type": data["type"],
            "refs": data.get("refs", []),
            "ref_count": len(data.get("refs", [])),
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(format_markdown(data))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
