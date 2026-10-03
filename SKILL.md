---
name: unified-search
description: Unified web search + deep research suite. Default ordinary /unified_search queries route to deep search-layer mode. The legacy merged three-engine script is not included in this repository snapshot, so --legacy requires that script to be supplied separately. Use when user asks for “综合搜索”, “三搜索”, “三引擎”, “/unified_search”, deep search, issue/PR thread tracing, content extraction, or URL→Markdown conversion.
---

# Unified Search

This skill is now a **two-layer search suite**:

1. **Vendored deep-research stack** — now the default for ordinary `/unified_search <query>` calls
   - `search-layer`: Exa + Tavily + Grok multi-source search with intent-aware scoring
   - `fetch-thread`: deep thread / issue / PR / forum context extraction
   - `content-extract`: local URL → Markdown extraction (MinerU is a separate explicit route)
   - `mineru-extract`: official MinerU parsing wrapper
2. **Legacy merged search** — compatibility route via `--legacy` or the explicit `legacy` subcommand, provided `scripts/unified-search-legacy.sh` is installed
   - Engines: **Tavily + Exa + Google**
   - Best for: quick fact-checks, troubleshooting, product/doc lookup, fast aggregated evidence

## Recommended usage policy

### A. Everyday search: default to deep search-layer

Run:

```bash
bash scripts/unified-search.sh "<query>"
```

This routes ordinary lookup queries to `search-layer` deep mode by default. In this repository snapshot, the legacy script is absent; `--legacy` and `legacy` return an explicit missing-implementation error until that script is supplied.

Examples:

```bash
bash scripts/unified-search.sh "tavily language filter"
bash scripts/unified-search.sh "OpenClaw cron run docs"
```

Chat trigger examples:
- `/unified_search tavily docs language filter`
- `/unified_search OpenClaw cron run docs`

### B. Explicit deep research: route to vendored search-layer

Run:

```bash
bash scripts/unified-search.sh search-layer "<query>" --mode deep --intent status --num 5
```

Examples:

```bash
bash scripts/unified-search.sh search-layer "OpenClaw config validation bug" --mode deep --intent status --extract-refs
bash scripts/unified-search.sh search-layer --queries "Bun vs Deno" "Bun advantages" "Deno advantages" --mode deep --intent comparison --num 5
bash scripts/unified-search.sh "RAG framework comparison" --mode deep --intent comparison --num 5
```

If `--mode / --intent / --freshness / --source / --extract-refs / --extract-refs-urls / --domain-boost` appears, the unified wrapper auto-routes to deep-search mode.

### C. Thread / issue / PR deep fetch

Run:

```bash
bash scripts/unified-search.sh fetch-thread "https://github.com/owner/repo/issues/123"
```

Examples:

```bash
bash scripts/unified-search.sh fetch-thread "https://github.com/owner/repo/issues/123" --format markdown
bash scripts/unified-search.sh fetch-thread "https://news.ycombinator.com/item?id=43197966" --timeout 60
```

### D. URL → Markdown extraction

Run:

```bash
bash scripts/unified-search.sh content-extract --url "https://mp.weixin.qq.com/s/example"
```

### E. MinerU direct parsing

Run:

```bash
bash scripts/unified-search.sh mineru-extract "https://example.com/file.pdf"
bash scripts/unified-search.sh mineru-parse-documents --file-sources "https://example.com/file.pdf"
```

## Request and content limits

- Search-layer requests have a 105-second default deadline. Set `UNIFIED_SEARCH_TIMEOUT_SECONDS` or pass `--timeout` to use a different positive budget; the MCP wrapper's outer timeout remains 120 seconds by default.
- `fetch-thread` accepts only HTTP(S) URLs, blocks localhost and non-public literal IP addresses (including on redirects), caps API responses at 10 MiB and generic HTML at 5 MiB, and limits fetched comments to 500 (HN comment trees to 200). Its CLI `--timeout` sets one request deadline. Results include `truncated` and `truncation_reasons` when a known limit or incomplete page affects returned content. Hostname DNS answers are not pinned, so this is not a complete DNS-rebinding/SSRF boundary.
- `--max-comments` is honored by GitHub issue/PR, Reddit, HN, and V2EX routes; the HN budget counts all nested replies, with a maximum of 200.
- Search JSON reports source/query outcomes through `status`, `provider_status`, `provider_errors`, and optional `query_errors`. Reference extraction adds `refs_status`; an item-level fetch error or truncation changes the overall status to `partial` (or `error` when every explicit-URL extraction fails).
- Search exits non-zero when its overall status is `error`; valid empty searches and partial results retain exit code 0. Multi-query intent scoring uses the best match across all queries, so later queries and translated variants are not penalized by the first query's wording.
- The standalone relevance gate fails closed when the scorer is unavailable or returns malformed/incomplete scores; it emits a structured failure object and non-zero exit status in those cases.
- MinerU downloads are capped at 100 MiB. ZIP extraction is bounded to 5,000 members, 128 MiB per member, 512 MiB expanded total, and a 500:1 compression ratio; unsafe paths and symlinks are rejected. Request deadlines also bound polling and network calls.
- The bundled `content-extract` route uses its local extractor: `--timeout` controls the HTTP request (default 30 seconds), and `--max-chars` controls the returned text. The vendored MinerU fallback wrapper is a separate implementation.

## Wrapper subcommands

### 1) Default deep search-layer

```bash
bash scripts/unified-search.sh "<query>"
```

Ordinary queries now default to search-layer deep mode. Use the explicit `search-layer` subcommand for search-layer options such as `--num`, `--mode`, and `--intent`. `--save-run DIR` applies to wrapper routes and uses a unique filename if multiple runs start in the same second. `--topic`, `--days`, and `--json` select the legacy interface, which currently returns a missing-implementation error because its script is absent.

Explicit subcommands forward their native options unchanged. A leading URL selects the corresponding
thread/document/content route and forwards remaining options, including `--timeout` and `--format`
where supported. Use `search-layer "<URL>"` to search for a URL instead of fetching it. Use `--` before
literal text that resembles a wrapper option or subcommand; the MCP tool always treats its query as
literal search text while retaining automatic intent detection and query expansion.

Chinese query expansion and optional English-to-Chinese result summaries use Google Translate by default. Set `UNIFIED_SEARCH_DISABLE_TRANSLATION=1` to keep both the query and result text local to the configured search providers.

### 2) search-layer

```bash
bash scripts/unified-search.sh search-layer ...
```

Important parameters:
- `--mode fast|deep|answer`
- `--intent factual|status|comparison|tutorial|exploratory|news|resource`
- `--freshness pd|pw|pm|py`
- `--queries ...`
- `--domain-boost github.com,stackoverflow.com`
- `--source exa,tavily,grok,tinyfish`
- `--extract-refs`
- `--extract-refs-urls`

### 3) fetch-thread

```bash
bash scripts/unified-search.sh fetch-thread <url> [--format json|markdown] [--extract-refs-only]
```

### 4) content-extract

```bash
bash scripts/unified-search.sh content-extract --url <url>
```

### 5) mineru-extract / mineru-parse-documents

```bash
bash scripts/unified-search.sh mineru-extract <url> [--model MinerU-HTML]
bash scripts/unified-search.sh mineru-parse-documents --file-sources "<URL1>\n<URL2>"
```

## Environment / dependency notes

### Legacy search route (not bundled)
This repository snapshot has no `scripts/unified-search-legacy.sh`, so its legacy engine setup cannot be run from this checkout.

### Vendored deep-search stack
Preferred credentials file:

```json
{
  "exa": "your-exa-key",
  "tavily": "your-tavily-key",
  "grok": {
    "apiUrl": "https://api.x.ai/v1",
    "apiKey": "your-grok-key",
    "model": "grok-4.20-multi-agent-xhigh"
  },
  "tinyfish": {
    "apiKey": "your-tinyfish-key",
    "apiUrl": "https://api.search.tinyfish.ai"
  }
}
```

Location:

```bash
~/.openclaw/credentials/search.json
```

Optional env overrides:

```bash
export EXA_API_KEY="..."
export TAVILY_API_KEY="..."
export GROK_API_URL="https://api.x.ai/v1"
export GROK_API_KEY="..."
export GROK_MODEL="grok-4.20-multi-agent-0309"
export TINYFISH_API_KEY="..."
export TINYFISH_API_URL="https://api.search.tinyfish.ai"
export GITHUB_TOKEN="..."   # improves GitHub issue/PR thread fetch limits
export MINERU_TOKEN="..."   # required for MinerU parsing
```

### Local Python runtime
This skill uses a dedicated venv. For a local checkout, create it and install the packages in `requirements.txt` with:

```bash
bash scripts/setup-venv.sh
```

The setup script requires Python 3.10 or newer. It installs the content extraction, search, and MCP server dependencies into `.venv`.

MinerU document parsing resolves `OPENCLAW_WORKSPACE` after loading its skill `.env` files. An existing
process environment value takes precedence. Its cache lives under `<workspace>/mineru-cache`.

## Important constraints

- The vendored `search-layer` script can directly use **Exa + Tavily + Grok + TinyFish**.
- The original upstream README also references **Brave via OpenClaw built-in `web_search`**, but shell scripts themselves cannot call agent-only tools. So in pure CLI mode, Brave is not auto-executed by the wrapper.
- The current default query route is the vendored deep `search-layer`; there is no bundled Google-backed legacy implementation in this snapshot.
- If no configured provider matches the selected mode/source filter, `search-layer` emits a JSON `no_search_provider` error and exits non-zero instead of returning an indistinguishable successful empty result.
- `content-extract` performs local extraction with its Python dependencies; it does not currently fall back to MinerU. `mineru-*` routes require external accessibility and a valid `MINERU_TOKEN`.
- The MCP wrapper defaults to a 120-second timeout. Set `UNIFIED_SEARCH_MCP_TIMEOUT_SECONDS` to a positive number to change that limit; timeouts and non-zero exits return JSON containing status and captured output. Successful runs preserve stderr diagnostics separately; only explicit provider failures mark the result `partial`.

## Vendored source snapshot

The upstream implementation is vendored here:

```bash
vendor/openclaw-search-skills/
```

Key vendored modules:
- `search-layer/`
- `content-extract/`
- `mineru-extract/`

## Output pattern

### Legacy merged search (when the implementation is supplied)
1. Keep raw engine blocks (Tavily / Exa / Google)
2. Deduplicate overlapping links
3. Report:
   - consensus findings
   - disagreements / uncertainty
   - actionable next step

### Deep-search / extraction flows
Return the native structured JSON / markdown contract from the vendored tool whenever possible, then summarize with:
- direct conclusion
- strongest supporting sources
- uncertainty / conflicts
- next recommended trace or extraction step

For concise reporting template, read `references/report-template.md`.
