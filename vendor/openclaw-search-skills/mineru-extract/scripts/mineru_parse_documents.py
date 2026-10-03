#!/usr/bin/env python3
"""MCP-aligned wrapper for MinerU official API.

Goal: expose a stable, workflow-friendly interface similar to MinerU MCP's `parse_documents`.

- Accept `file_sources` (comma/newline-separated URLs or local file paths).
- For URLs: use MinerU /api/v4/extract/task (single) with model_version auto/default.
- Download result zip + extract main Markdown.
- Return a JSON result contract on stdout.

Notes
- This is NOT an MCP server. It's a script meant to be called by OpenClaw skills via exec.
- Secrets loaded from .env (skill root) or environment.

Env
- MINERU_TOKEN (required): bearer token
- MINERU_API_BASE (optional): default https://mineru.net

"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


def _default_workspace() -> pathlib.Path:
    """Return workspace root, preferring env override."""
    if v := os.environ.get("OPENCLAW_WORKSPACE"):
        return pathlib.Path(v).expanduser()
    return pathlib.Path.home() / ".openclaw" / "workspace"


def _cache_root() -> pathlib.Path:
    # Resolve at use time, after main() has loaded the skill's .env files.
    return _default_workspace() / "mineru-cache"


MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ZIP_MEMBERS = 5000
MAX_ZIP_MEMBER_BYTES = 128 * 1024 * 1024
MAX_ZIP_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 500
_REQUEST_DEADLINE = None


class MinerUTaskError(RuntimeError):
    """A submitted task failed locally or remotely, with a durable resume handle."""

    def __init__(self, message, *, task_id, state, checkpoint_path):
        super().__init__(message)
        self.task = {"task_id": task_id, "state": state,
                     "checkpoint_path": str(checkpoint_path), "resumable": state != "failed"}


def _write_json_atomic(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".mineru-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _set_request_deadline(deadline: float | None) -> None:
    global _REQUEST_DEADLINE
    _REQUEST_DEADLINE = deadline


def _bounded_timeout(timeout: float) -> float:
    if _REQUEST_DEADLINE is None:
        return timeout
    remaining = _REQUEST_DEADLINE - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("MinerU request deadline exhausted")
    return min(timeout, remaining)


def _load_dotenv(path: pathlib.Path) -> None:
    if not path.exists() or not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _bootstrap_env() -> None:
    here = pathlib.Path(__file__).resolve()
    _load_dotenv(here.parent / ".env")
    _load_dotenv(here.parent.parent / ".env")


def _http_json(method: str, url: str, *, headers: dict[str, str] | None = None, payload: dict | None = None, timeout: int = 60) -> dict:
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "openclaw-mineru"}
    if headers:
        hdrs.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url=url, data=data, method=method.upper(), headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=_bounded_timeout(timeout)) as resp:
            raw = resp.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise RuntimeError(f"JSON response exceeds {MAX_JSON_BYTES} bytes")
            return json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} for {url}: {body[:800]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error for {url}: {e}") from e


def _http_bytes(url: str, *, headers: dict[str, str] | None = None, timeout: int = 180) -> bytes:
    hdrs = {"User-Agent": "openclaw-mineru"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url=url, method="GET", headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=_bounded_timeout(timeout)) as resp:
            content_length = resp.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError(f"MinerU archive exceeds {MAX_DOWNLOAD_BYTES} bytes")
                except ValueError:
                    pass
            chunks = []
            total = 0
            while True:
                chunk = resp.read(min(1024 * 1024, MAX_DOWNLOAD_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(f"MinerU archive exceeds {MAX_DOWNLOAD_BYTES} bytes")
                chunks.append(chunk)
            return b"".join(chunks)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} for {url}: {body[:800]}") from e


def _is_url(s: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(s)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def _split_sources(s: str) -> list[str]:
    parts = re.split(r"[\n,]+", s)
    out = []
    for p in parts:
        x = p.strip()
        if x:
            out.append(x)
    return out


def _sanitize(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s.strip())
    return s[:120] if len(s) > 120 else s


def _cache_key(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _pick_model_version(source: str, model_version: str | None) -> str:
    if model_version:
        return model_version
    lower = source.lower()
    if lower.endswith((".pdf", ".doc", ".docx", ".ppt", ".pptx", ".png", ".jpg", ".jpeg")):
        return "pipeline"
    return "MinerU-HTML"


def create_task(*, api_base: str, token: str, payload: dict) -> str:
    endpoint = api_base.rstrip("/") + "/api/v4/extract/task"
    res = _http_json("POST", endpoint, headers={"Authorization": f"Bearer {token}"}, payload=payload, timeout=90)
    if res.get("code") != 0:
        raise RuntimeError(f"MinerU create_task failed: {res}")
    task_id = (res.get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"MinerU create_task missing task_id: {res}")
    return str(task_id)


def poll_task(*, api_base: str, token: str, task_id: str, timeout_sec: int, poll_interval: float, on_update=None) -> dict:
    endpoint = api_base.rstrip("/") + f"/api/v4/extract/task/{task_id}"
    start = time.monotonic()
    last_state = None
    while True:
        res = _http_json("GET", endpoint, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        if res.get("code") != 0:
            raise RuntimeError(f"MinerU poll failed: {res}")
        data = res.get("data") or {}
        state = data.get("state")
        if on_update is not None:
            on_update(data)
        if state and state != last_state:
            print(f"state={state}", file=sys.stderr)
            last_state = state
        if state == "done":
            return data
        if state == "failed":
            raise RuntimeError(f"MinerU task failed: {data.get('err_msg') or '(no err_msg)'}")
        elapsed = time.monotonic() - start
        if elapsed > timeout_sec:
            raise RuntimeError(f"MinerU poll timeout after {timeout_sec}s (last state={state})")
        remaining = timeout_sec - elapsed
        if _REQUEST_DEADLINE is not None:
            remaining = min(remaining, _REQUEST_DEADLINE - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("MinerU request deadline exhausted while polling")
        time.sleep(min(poll_interval, remaining))


def extract_main_markdown(zip_bytes: bytes, out_dir: pathlib.Path) -> pathlib.Path | None:
    out_dir.mkdir(parents=True, exist_ok=True)
    root = out_dir.resolve()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        members = z.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise ValueError(f"MinerU archive contains too many members ({len(members)})")
        expanded_bytes = 0
        for member in members:
            relative = pathlib.PurePosixPath(member.filename)
            if relative.is_absolute() or ".." in relative.parts or "\\" in member.filename:
                raise ValueError(f"Unsafe path in MinerU archive: {member.filename!r}")
            target = root.joinpath(*relative.parts).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"Unsafe path in MinerU archive: {member.filename!r}")
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError(f"Symlink not allowed in MinerU archive: {member.filename!r}")
            if member.file_size > MAX_ZIP_MEMBER_BYTES:
                raise ValueError(f"MinerU archive member exceeds {MAX_ZIP_MEMBER_BYTES} bytes")
            expanded_bytes += member.file_size
            if expanded_bytes > MAX_ZIP_EXPANDED_BYTES:
                raise ValueError(f"MinerU archive expands beyond {MAX_ZIP_EXPANDED_BYTES} bytes")
            if member.file_size and (
                member.compress_size == 0 or
                member.file_size / member.compress_size > MAX_ZIP_COMPRESSION_RATIO
            ):
                raise ValueError(f"Suspicious compression ratio in MinerU archive member: {member.filename!r}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with z.open(member) as source, target.open("wb") as destination:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > member.file_size or written > MAX_ZIP_MEMBER_BYTES:
                        raise ValueError(f"Invalid expanded size for MinerU archive member: {member.filename!r}")
                    destination.write(chunk)
            if written != member.file_size:
                raise ValueError(f"Truncated MinerU archive member: {member.filename!r}")

    md_files = [p for p in out_dir.rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".markdown")]
    if not md_files:
        return None

    def score(p: pathlib.Path) -> tuple[int, int]:
        name = p.name.lower()
        penalty = 0
        if "readme" in name:
            penalty += 2
        if "layout" in name or "span" in name or "debug" in name:
            penalty += 3
        return (-penalty, p.stat().st_size)

    md_files_sorted = sorted(md_files, key=score, reverse=True)
    return md_files_sorted[0]


def _cache_file(meta: dict, field: str, out_dir: pathlib.Path, *, suffixes: tuple[str, ...] = ()) -> pathlib.Path | None:
    raw_path = meta.get(field)
    if not isinstance(raw_path, str) or not raw_path:
        return None
    root = out_dir.resolve()
    path = pathlib.Path(raw_path)
    if not path.is_absolute():
        path = root / path
    try:
        path = path.resolve(strict=True)
    except OSError:
        return None
    if root not in path.parents or not path.is_file() or path.stat().st_size <= 0:
        return None
    if suffixes and path.suffix.lower() not in suffixes:
        return None
    return path


def _file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_cache(meta: dict, *, key: str, source_url: str, model_version: str, out_dir: pathlib.Path) -> bool:
    if meta.get("cache_key") != key or meta.get("source") != source_url:
        return False
    if meta.get("model_version") != model_version:
        return False
    markdown_path = _cache_file(meta, "markdown_path", out_dir, suffixes=(".md", ".markdown"))
    zip_path = _cache_file(meta, "zip_path", out_dir, suffixes=(".zip",))
    if not markdown_path or not zip_path:
        return False
    try:
        if meta.get("markdown_sha256") and _file_sha256(markdown_path) != meta["markdown_sha256"]:
            return False
        if meta.get("zip_sha256") and _file_sha256(zip_path) != meta["zip_sha256"]:
            return False
        with zipfile.ZipFile(zip_path) as z:
            if z.testzip() is not None:
                return False
    except (OSError, zipfile.BadZipFile):
        return False
    return True


def parse_one_url(*, api_base: str, token: str, source_url: str, enable_ocr: bool, language: str, page_ranges: str | None, model_version: str | None, enable_table: bool | None, enable_formula: bool | None, extra_formats: list[str] | None, timeout_sec: int, poll_interval: float, cache: bool, force: bool) -> dict:
    if timeout_sec <= 0:
        raise ValueError("timeout_sec must be positive")
    if not _is_url(source_url):
        raise ValueError("source must be an HTTP(S) URL with a hostname and no embedded credentials")
    _set_request_deadline(time.monotonic() + timeout_sec)
    mv = _pick_model_version(source_url, model_version)

    payload: dict = {
        "url": source_url,
        "model_version": mv,
        "language": language,
    }
    payload["is_ocr"] = bool(enable_ocr)
    if page_ranges:
        payload["page_ranges"] = page_ranges
    if enable_table is not None:
        payload["enable_table"] = bool(enable_table)
    if enable_formula is not None:
        payload["enable_formula"] = bool(enable_formula)
    if extra_formats:
        payload["extra_formats"] = extra_formats

    # Keep the original key for the official endpoint so upgrades can reuse
    # completed caches. Custom endpoints get a separate namespace from now on.
    key = _cache_key(payload if api_base.rstrip("/") == "https://mineru.net"
                     else {"api_base": api_base.rstrip("/"), "payload": payload})
    out_dir = _cache_root() / key
    meta_path = out_dir / "meta.json"
    checkpoint_path = out_dir / "task.json"

    if cache and (not force) and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict) and _valid_cache(
                meta,
                key=key,
                source_url=source_url,
                model_version=mv,
                out_dir=out_dir,
            ):
                meta["cached"] = True
                return meta
        except Exception:
            pass

    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = None
    if checkpoint_path.exists() and not force:
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if (not isinstance(checkpoint, dict) or checkpoint.get("cache_key") != key
                    or checkpoint.get("api_base") != api_base.rstrip("/")
                    or not isinstance(checkpoint.get("task_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", checkpoint["task_id"])):
                raise ValueError("Invalid task checkpoint")
        except (OSError, ValueError) as exc:
            raise ValueError(f"Unreadable MinerU task checkpoint {checkpoint_path}; use --force to submit a new task") from exc
    resumed = checkpoint is not None
    if checkpoint is None:
        task_id = create_task(api_base=api_base, token=token, payload=payload)
        checkpoint = {"cache_key": key, "api_base": api_base.rstrip("/"),
                      "task_id": task_id, "state": "submitted", "created_at": int(time.time())}
    task_id = checkpoint["task_id"]

    def save_progress(data):
        checkpoint.update(state=data.get("state") or checkpoint["state"],
                          updated_at=int(time.time()), error=data.get("err_msg") or "")
        _write_json_atomic(checkpoint_path, checkpoint)

    try:
        _write_json_atomic(checkpoint_path, checkpoint)
        if checkpoint.get("state") == "failed":
            raise RuntimeError(checkpoint.get("error") or "MinerU task failed; use --force to retry")
        print(f"task_id={task_id} resumed={str(resumed).lower()}", file=sys.stderr)
        data = poll_task(api_base=api_base, token=token, task_id=task_id,
                         timeout_sec=timeout_sec, poll_interval=poll_interval, on_update=save_progress)
        save_progress(data)

        full_zip_url = data.get("full_zip_url")
        if not full_zip_url:
            raise RuntimeError("Completed MinerU task has no full_zip_url")
        if not _is_url(full_zip_url):
            raise ValueError("MinerU returned an invalid archive URL")
        zip_bytes = _http_bytes(full_zip_url, timeout=180)
        zip_path = out_dir / f"{_sanitize(task_id)}.zip"
        zip_path.write_bytes(zip_bytes)

        md_path = extract_main_markdown(zip_bytes, out_dir / f"extract-{_sanitize(task_id)}")
        if not md_path or not md_path.is_file() or md_path.stat().st_size <= 0:
            raise RuntimeError("MinerU result archive contains no non-empty Markdown file")
    except Exception as exc:
        raise MinerUTaskError(str(exc), task_id=task_id, state=checkpoint.get("state", "unknown"),
                              checkpoint_path=checkpoint_path) from exc

    result = {
        "ok": True,
        "source": source_url,
        "task_id": task_id,
        "state": data.get("state"),
        "model_version": mv,
        "language": language,
        "enable_ocr": bool(enable_ocr),
        "page_ranges": page_ranges,
        "full_zip_url": full_zip_url,
        "out_dir": str(out_dir),
        "zip_path": str(zip_path),
        "markdown_path": str(md_path) if md_path else None,
        "cached": False,
        "resumed": resumed,
        "cache_key": key,
        "zip_size": zip_path.stat().st_size,
        "zip_sha256": _file_sha256(zip_path),
        "markdown_size": md_path.stat().st_size,
        "markdown_sha256": _file_sha256(md_path),
        "fetched_at": int(time.time()),
    }
    _write_json_atomic(meta_path, result)
    return result


def main() -> int:
    _bootstrap_env()

    ap = argparse.ArgumentParser()
    ap.add_argument("--file-sources", required=True, help="Comma/newline separated URLs or local paths (MCP-style).")
    ap.add_argument("--enable-ocr", action="store_true", help="Enable OCR (maps to MinerU is_ocr).")
    ap.add_argument("--language", default="ch")
    ap.add_argument("--page-ranges", default=None)
    ap.add_argument("--model-version", default=None, help="pipeline | vlm | MinerU-HTML")
    ap.add_argument("--enable-table", default=None, choices=["true", "false"])
    ap.add_argument("--enable-formula", default=None, choices=["true", "false"])
    ap.add_argument("--extra-formats", default=None, help="Comma-separated: docx,html,latex")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--poll-interval", type=float, default=3.0)
    ap.add_argument("--cache", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--emit-markdown", action="store_true", help="Include markdown text in JSON (can be large).")
    ap.add_argument("--max-chars", type=int, default=20000, help="When --emit-markdown, truncate markdown to this many chars.")

    args = ap.parse_args()
    if args.timeout <= 0 or args.poll_interval <= 0:
        ap.error("--timeout and --poll-interval must be positive")

    token = os.environ.get("MINERU_TOKEN")
    if not token:
        print(json.dumps({
            "ok": False,
            "error": "Missing MINERU_TOKEN. Set env or put it in skill .env file.",
            "items": [],
        }, ensure_ascii=False))
        return 2

    api_base = os.environ.get("MINERU_API_BASE", "https://mineru.net")

    sources = _split_sources(args.file_sources)
    items: list[dict] = []
    errors: list[dict] = []

    enable_table = None
    if args.enable_table is not None:
        enable_table = args.enable_table == "true"

    enable_formula = None
    if args.enable_formula is not None:
        enable_formula = args.enable_formula == "true"

    extra_formats = None
    if args.extra_formats:
        extra_formats = [s.strip() for s in args.extra_formats.split(",") if s.strip()]

    for src in sources:
        if not _is_url(src):
            errors.append({
                "source": src,
                "error": "Local file paths not supported in this workflow yet.",
                "next_step": "Provide a public URL or ask to add MinerU batch upload support.",
            })
            continue

        try:
            meta = parse_one_url(
                api_base=api_base,
                token=token,
                source_url=src,
                enable_ocr=args.enable_ocr,
                language=args.language,
                page_ranges=args.page_ranges,
                model_version=args.model_version,
                enable_table=enable_table,
                enable_formula=enable_formula,
                extra_formats=extra_formats,
                timeout_sec=args.timeout,
                poll_interval=args.poll_interval,
                cache=args.cache,
                force=args.force,
            )
            if args.emit_markdown and meta.get("markdown_path"):
                p = pathlib.Path(meta["markdown_path"])
                if p.exists():
                    txt = p.read_text(encoding="utf-8", errors="replace")
                    if args.max_chars and len(txt) > args.max_chars:
                        txt = txt[: args.max_chars] + "\n\n[TRUNCATED]"
                    meta["markdown"] = txt
            items.append(meta)
        except Exception as e:
            error = {
                "source": src,
                "error": str(e),
                "next_step": "If this is a protected page, try another accessible mirror URL.",
            }
            if isinstance(e, MinerUTaskError):
                error["task"] = e.task
                error["next_step"] = ("Retry the same command to resume the saved task." if e.task["resumable"]
                                      else "The service reported failure; use --force to submit a new task.")
            errors.append(error)

    ok = len(errors) == 0
    out = {"ok": ok, "items": items, "errors": errors}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
