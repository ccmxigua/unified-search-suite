#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import importlib.util
import subprocess
import ipaddress
import math
import os
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit

import requests
import trafilatura
from bs4 import BeautifulSoup


def fallback_markdown(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()
    title = (soup.title.string or '').strip() if soup.title and soup.title.string else ''
    parts = []
    if title:
        parts.append(f'# {title}')
    texts = []
    for node in soup.find_all(['h1', 'h2', 'h3', 'p', 'li']):
        text = node.get_text(' ', strip=True)
        text = re.sub(r'\s+', ' ', text).strip()
        if not text:
            continue
        if node.name in ('h1', 'h2', 'h3'):
            level = {'h1': '#', 'h2': '##', 'h3': '###'}[node.name]
            texts.append(f'{level} {text}')
        elif node.name == 'li':
            texts.append(f'- {text}')
        else:
            texts.append(text)
    if texts:
        parts.append('\n\n'.join(texts[:400]))
    return '\n\n'.join([p for p in parts if p]).strip()


MAX_BODY_BYTES = 5 * 1024 * 1024
MIN_CONTENT_CHARS = 200


class UnsafeURL(ValueError):
    pass


def _validate_url(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError as exc:
        raise UnsafeURL("Invalid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise UnsafeURL("Expected an HTTP(S) URL without embedded credentials")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise UnsafeURL("Local hostnames are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise UnsafeURL("Non-public IP addresses are not allowed")


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("Content extraction deadline exhausted")
    return seconds


def _fetch_html(url, deadline):
    current = url
    for _ in range(6):
        _validate_url(current)
        with requests.get(current, headers={"User-Agent": "Mozilla/5.0 unified-search"},
                          timeout=_remaining(deadline), stream=True, allow_redirects=False) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("Redirect has no Location header")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type and content_type not in {"text/html", "text/plain", "application/xhtml+xml", "application/pdf", "application/octet-stream"}:
                raise ValueError(f"Unsupported local content type: {content_type}")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                _remaining(deadline)
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise ValueError("Page exceeds the 5 MiB extraction limit")
                chunks.append(chunk)
            body = b"".join(chunks)
            artifacts = {"status_code": response.status_code, "content_type": content_type, "final_url": current}
            if body.lstrip().startswith(b"%PDF-") or content_type == "application/pdf":
                return body, artifacts
            if content_type == "application/octet-stream":
                raise ValueError("Unsupported binary document; expected a PDF")
            return body.decode(response.encoding or "utf-8", errors="replace"), artifacts
    raise ValueError("Too many redirects")


def _extract_pdf(body, deadline, max_chars):
    # A subprocess makes CPU-heavy parsing interruptible, including decompression.
    worker = Path(__file__).with_name("pdf-text-extract.py")
    try:
        result = subprocess.run([sys.executable, str(worker), str(max_chars)], input=body,
                                capture_output=True, timeout=_remaining(deadline))
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("PDF text extraction deadline exhausted") from exc
    if result.returncode:
        raise ValueError("PDF text extraction failed: " + result.stderr.decode("utf-8", "replace")[-300:])
    data = json.loads(result.stdout)
    return data["text"], data["artifacts"]


def _mineru_extract(url, timeout, max_chars, *, is_pdf=False):
    path = Path(__file__).resolve().parents[1] / "vendor/openclaw-search-skills/mineru-extract/scripts/mineru_parse_documents.py"
    spec = importlib.util.spec_from_file_location("mineru_content_fallback", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._bootstrap_env()
    token = os.environ.get("MINERU_TOKEN")
    if not token:
        raise ValueError("MinerU fallback requires MINERU_TOKEN")
    model = "pipeline" if is_pdf or urlsplit(url).path.lower().endswith((".pdf", ".doc", ".docx", ".ppt", ".pptx")) else "MinerU-HTML"
    meta = module.parse_one_url(
        api_base=os.environ.get("MINERU_API_BASE", "https://mineru.net"), token=token,
        source_url=url, enable_ocr=False, language="ch", page_ranges=None, model_version=model,
        enable_table=None, enable_formula=None, extra_formats=None,
        timeout_sec=timeout, poll_interval=2, cache=True, force=False)
    with Path(meta["markdown_path"]).open(encoding="utf-8", errors="replace") as source:
        markdown = source.read(max(max_chars, MIN_CONTENT_CHARS) + 1)
    return markdown, {key: meta.get(key) for key in ("markdown_path", "zip_path", "cache_key", "cached")}


def extract_content(url, timeout=30, max_chars=20000, fallback="none"):
    if not math.isfinite(timeout) or timeout <= 0 or not 1 <= max_chars <= 200000 or fallback not in {"none", "mineru"}:
        raise ValueError("Invalid extraction timeout, character limit or fallback")
    deadline = time.monotonic() + timeout
    out = {"ok": False, "source_url": url, "engine": "trafilatura", "markdown": None,
           "artifacts": {}, "sources": [url], "notes": [], "attempts": []}
    try:
        _validate_url(url)
    except ValueError as exc:
        out.update(error=str(exc), quality="empty", status="error")
        return out
    best = ""
    is_pdf = False
    try:
        html, artifacts = _fetch_html(url, deadline)
        out["artifacts"] = artifacts
        if artifacts["final_url"] != url:
            out["sources"].append(artifacts["final_url"])
        is_pdf = isinstance(html, bytes)
        if is_pdf:
            out["engine"] = "pypdf"
            best, pdf_artifacts = _extract_pdf(html, deadline, max_chars)
            out["artifacts"].update(pdf_artifacts)
            out["attempts"].append({"engine": "pypdf", "ok": bool(best), "chars": len(best)})
        else:
            try:
                best = (trafilatura.extract(html, url=artifacts["final_url"], output_format="markdown",
                        include_links=True, include_formatting=True, favor_precision=True, deduplicate=True) or "").strip()
                out["attempts"].append({"engine": "trafilatura", "ok": bool(best), "chars": len(best)})
            except Exception as exc:
                out["attempts"].append({"engine": "trafilatura", "ok": False, "error": str(exc)[:300]})
            if len(best) < MIN_CONTENT_CHARS:
                alternative = fallback_markdown(html)
                out["attempts"].append({"engine": "beautifulsoup", "ok": bool(alternative), "chars": len(alternative)})
                if len(alternative) > len(best):
                    best, out["engine"] = alternative, "beautifulsoup"
    except UnsafeURL as exc:
        out.update(error=str(exc), quality="empty", status="error")
        out["attempts"].append({"engine": "local_fetch", "ok": False, "error": str(exc)})
        return out
    except Exception as exc:
        out["attempts"].append({"engine": "local_fetch", "ok": False, "error": str(exc)[:300]})
    needs_fallback = (not best or out["artifacts"].get("pdf_text_incomplete")
                      or (not is_pdf and len(best) < MIN_CONTENT_CHARS))
    if needs_fallback and fallback == "mineru":
        try:
            markdown, artifacts = _mineru_extract(url, _remaining(deadline), max_chars, is_pdf=is_pdf)
            out["attempts"].append({"engine": "mineru", "ok": bool(markdown), "chars": len(markdown)})
            if len(markdown) > len(best):
                best, out["engine"], out["artifacts"] = markdown, "mineru", artifacts
        except Exception as exc:
            out["attempts"].append({"engine": "mineru", "ok": False, "error": str(exc)[:300]})
            if isinstance(getattr(exc, "task", None), dict):
                out["mineru_task"] = exc.task
    out["notes"] = [a["error"] for a in out["attempts"] if a.get("error")]
    pdf_usable = out["engine"] == "pypdf" and bool(best) and not out["artifacts"].get("pdf_text_incomplete")
    sufficient = pdf_usable or (len(best) >= MIN_CONTENT_CHARS and not out["artifacts"].get("pdf_text_incomplete"))
    out["quality"] = "sufficient" if sufficient else "low" if best else "empty"
    out["ok"] = bool(best)
    out["status"] = "success" if out["quality"] == "sufficient" else "partial" if best else "error"
    out["truncated"] = len(best) > max_chars or bool(out["artifacts"].get("pdf_truncated"))
    if out["truncated"]:
        best = best[:max_chars]
        out["notes"].append(f"truncated to {max_chars} chars")
    if out["engine"] == "pypdf":
        out["notes"].append("PDF text layer only; no OCR or table/formula layout reconstruction.")
    out["markdown"] = best or None
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', required=True)
    ap.add_argument('--timeout', type=float, default=30)
    ap.add_argument('--max-chars', type=int, default=20000)
    ap.add_argument('--fallback', choices=['none', 'mineru'], default='none')
    args = ap.parse_args()
    try:
        out = extract_content(args.url, args.timeout, args.max_chars, args.fallback)
    except ValueError as exc:
        ap.error(str(exc))
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


if __name__ == '__main__':
    raise SystemExit(main())
