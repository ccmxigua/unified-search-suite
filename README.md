# Unified Search Suite

This skill is now a **two-layer search suite**:

## Installation

```bash
clawhub install unified-search-suite
```

For a local checkout, create the Python runtime and install the declared dependencies with:

```bash
bash scripts/setup-venv.sh
```

## Usage

See [SKILL.md](./SKILL.md) for full documentation.

## Tests

After installing the runtime, run the offline regression suite:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover CLI routing, multi-query ranking, failure statuses, MCP stdio initialization/tool calls,
and runtime/cache configuration. They use temporary fixtures and mocked providers, and require
no API keys or network access. GitHub Actions runs them on Linux with Python 3.10 and 3.14,
and on macOS with Python 3.14. Live provider checks are separate and require configured credentials.

The MCP server currently uses the SDK's 1.x `FastMCP` API; `requirements.txt` limits `mcp` to
the compatible major version. Setup validates both the selected Python and any existing `.venv`.

## License

MIT
