#!/usr/bin/env python3
"""Isolated, resource-bounded text-layer PDF worker; input is PDF bytes on stdin."""

import io
import json
import sys


def extract(body, max_chars):
    # Linux can enforce an address-space limit before importing/parsing. macOS
    # rejects these rlimits; there we retain the stream cap and parent deadline.
    if sys.platform.startswith("linux"):
        import resource
        budget = 512 * 1024 * 1024
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (min(budget, hard) if hard != resource.RLIM_INFINITY else budget, hard))

    from pypdf import PdfReader, filters
    for name in ("ZLIB_MAX_OUTPUT_LENGTH", "LZW_MAX_OUTPUT_LENGTH", "RUN_LENGTH_MAX_OUTPUT_LENGTH",
                 "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH", "MAX_DECLARED_STREAM_LENGTH",
                 "FLATE_MAX_BUFFER_SIZE", "JBIG2_MAX_OUTPUT_LENGTH"):
        setattr(filters, name, 16 * 1024 * 1024)
    reader = PdfReader(io.BytesIO(body))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("Password-protected PDF cannot be extracted locally")
    total = len(reader.pages)
    if total > 200:
        raise ValueError("PDF exceeds the 200-page local extraction limit")
    parts, length, read, empty, truncated = [], 0, 0, 0, False
    limit = max(max_chars, 200) + 1
    for page in reader.pages:
        contents = page.get_contents()
        if contents is not None and len(contents.get_data()) > 16 * 1024 * 1024:
            raise ValueError("PDF page content stream exceeds 16 MiB")
        text = (page.extract_text() or "").strip()
        read += 1
        if not text:
            empty += 1
            continue
        remaining = max(0, limit - length)
        parts.append(text[:remaining])
        length += len(parts[-1]) + 2
        if len(text) > remaining or length >= limit:
            truncated = len(text) > remaining or read < total
            break
    return {"text": "\n\n".join(parts), "artifacts": {
        "pdf_pages": total, "pdf_pages_read": read, "pdf_empty_pages": empty,
        "pdf_text_incomplete": bool(empty), "pdf_truncated": truncated or read < total}}


if __name__ == "__main__":
    try:
        body = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
        if len(body) > 5 * 1024 * 1024:
            raise ValueError("PDF exceeds the 5 MiB extraction limit")
        print(json.dumps(extract(body, int(sys.argv[1]))))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
