#!/bin/bash
# unified-search.sh — unified entrypoint for legacy merged search + vendored deep search suite
#
# Default text queries use the deep search-layer route. The explicit legacy
# route is retained as a compatibility entrypoint when its script is installed.
# New subcommands:
#   search-layer            -> vendored multi-source deep search
#   fetch-thread            -> vendored issue/PR/thread fetcher
#   content-extract         -> vendored URL -> Markdown extractor
#   mineru-extract          -> vendored MinerU single-URL parser
#   mineru-parse-documents  -> vendored MinerU MCP-style wrapper
#
# Auto-routing:
#   - GitHub issue/PR/discussion and similar thread URLs -> fetch-thread
#   - Document URLs (.pdf/.docx/...) -> mineru-extract
#   - Generic URLs -> content-extract
#   - Comparison / status / research style natural-language queries -> search-layer
#   - Plain text query -> search-layer deep search

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LEGACY_SCRIPT="$SCRIPT_DIR/unified-search-legacy.sh"
SEARCH_LAYER_WRAPPER="$SCRIPT_DIR/run-search-layer.sh"
FETCH_THREAD_WRAPPER="$SCRIPT_DIR/run-fetch-thread.sh"
CONTENT_EXTRACT_WRAPPER="$SCRIPT_DIR/run-content-extract.sh"
MINERU_EXTRACT_WRAPPER="$SCRIPT_DIR/run-mineru-extract.sh"
MINERU_PARSE_DOCS_WRAPPER="$SCRIPT_DIR/run-mineru-parse-documents.sh"

usage() {
  cat <<'EOF'
Usage:
  bash unified-search.sh "<query>" [search-layer flags]
  bash unified-search.sh --deep "<query>" [deep-search flags]
  bash unified-search.sh search-layer <args...>
  bash unified-search.sh fetch-thread <url> [args...]
  bash unified-search.sh content-extract --url <url> [args...]
  bash unified-search.sh mineru-extract <url> [args...]
  bash unified-search.sh mineru-parse-documents <args...>

Wrapper option:
  --save-run DIR

Search-layer flags (use the search-layer subcommand for the full option set):
  --deep   (alias of --mode deep)
  --fast   (alias of --mode fast)
  --answer (alias of --mode answer)
  --mode --intent --freshness --queries --source --num --timeout --extract-refs --extract-refs-urls --domain-boost
  --include-domains --exclude-domains --start-date --end-date
  --read-top --content-timeout --content-max-chars --content-fallback

Legacy compatibility options (require scripts/unified-search-legacy.sh, not bundled here):
  --topic general|news --days N --json --legacy

Automatic routing:
  - discussion/thread URLs             -> fetch-thread
  - document/file URLs                 -> mineru-extract
  - generic content URLs               -> content-extract
  - comparison/status/research queries -> search-layer
  - ordinary text queries              -> search-layer deep search
EOF
}

has_arg() {
  local needle="$1"
  shift || true
  local arg
  for arg in "$@"; do
    [[ "$arg" == "--" ]] && break
    [[ "$arg" == "$needle" ]] && return 0
  done
  return 1
}

normalize_mode_aliases() {
  local explicit_mode=0
  local arg
  for arg in "$@"; do
    [[ "$arg" == "--" ]] && break
    if [[ "$arg" == "--mode" || "$arg" == --mode=* ]]; then
      explicit_mode=1
      break
    fi
  done

  NORMALIZED_ARGS=()
  local literal=false
  for arg in "$@"; do
    if $literal; then
      NORMALIZED_ARGS+=("$arg")
      continue
    fi
    case "$arg" in
      --)
        literal=true
        NORMALIZED_ARGS+=("$arg")
        ;;
      --deep)
        if [[ "$explicit_mode" == "0" ]]; then
          NORMALIZED_ARGS+=(--mode deep)
        fi
        ;;
      --fast)
        if [[ "$explicit_mode" == "0" ]]; then
          NORMALIZED_ARGS+=(--mode fast)
        fi
        ;;
      --answer)
        if [[ "$explicit_mode" == "0" ]]; then
          NORMALIZED_ARGS+=(--mode answer)
        fi
        ;;
      *)
        NORMALIZED_ARGS+=("$arg")
        ;;
    esac
  done
}

lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

is_thread_url() {
  local url
  url="$(lower "$1")"
  [[ "$url" =~ github\.com/.+/(issues|pull|pulls|discussions)/[0-9]+ ]] && return 0
  [[ "$url" =~ news\.ycombinator\.com/item\?id= ]] && return 0
  [[ "$url" =~ reddit\.com/r/.+/comments/ ]] && return 0
  [[ "$url" =~ stackoverflow\.com/questions/[0-9]+ ]] && return 0
  [[ "$url" =~ stackexchange\.com/questions/[0-9]+ ]] && return 0
  [[ "$url" =~ /t/[^/]+/[0-9]+ ]] && return 0
  [[ "$url" =~ x\.com/.+/status/[0-9]+ ]] && return 0
  [[ "$url" =~ twitter\.com/.+/status/[0-9]+ ]] && return 0
  return 1
}

is_document_url() {
  local url
  url="$(lower "$1")"
  [[ "$url" =~ \.(pdf|doc|docx|ppt|pptx|xls|xlsx|csv|epub|mobi|rtf|odt|ods|odp)([?#].*)?$ ]] && return 0
  [[ "$url" =~ /download([/?#].*)?$ ]] && return 0
  return 1
}

query_text() {
  local joined=""
  local skip_value=false
  local literal=false
  local arg
  for arg in "$@"; do
    if $literal; then
      joined+="$arg "
      continue
    fi
    if [[ "$arg" == "--" ]]; then
      literal=true
      continue
    fi
    if $skip_value; then
      skip_value=false
      continue
    fi
    case "$arg" in
      --include-domains|--exclude-domains|--start-date|--end-date|--read-top|--content-timeout|--content-max-chars|--content-fallback|--num|--timeout|--topic|--days|--mode|--intent|--freshness|--source|--domain-boost|--extract-refs-urls)
        skip_value=true
        continue
        ;;
      --*) continue ;;
    esac
    joined+="$arg "
  done
  printf '%s' "${joined% }"
}

expand_query_variants() {
  local query="$1"
  if [[ "${UNIFIED_SEARCH_DISABLE_TRANSLATION:-0}" =~ ^(1|true|yes)$ ]]; then
    printf '%s\n' "$query"
    return 0
  fi
  python3 - "$query" <<'PY'
import json
import re
import sys
import urllib.parse
import urllib.request

query = sys.argv[1].strip()
if not query:
    raise SystemExit(0)

variants = [query]
if re.search(r'[\u3400-\u9fff]', query):
    translated = ""
    try:
        url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode({
            "client": "gtx",
            "sl": "auto",
            "tl": "en",
            "dt": "t",
            "q": query,
        })
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if isinstance(payload, list) and payload and isinstance(payload[0], list):
            translated = "".join(
                segment[0]
                for segment in payload[0]
                if isinstance(segment, list) and segment and isinstance(segment[0], str)
            )
    except Exception:
        translated = ""
    translated = " ".join((translated or "").split()).strip()
    if translated and translated.casefold() != query.casefold():
        variants.append(translated)

seen = set()
for item in variants:
    key = item.casefold()
    if not item or key in seen:
        continue
    seen.add(key)
    print(item)
PY
}

infer_intent() {
  local q
  q="$(lower "$1")"
  if [[ "$q" =~ (^|[[:space:]])(vs|versus)([[:space:]]|$) ]] || [[ "$q" == *"对比"* ]] || [[ "$q" == *"区别"* ]] || [[ "$q" == *"比较"* ]] || [[ "$q" == *"哪个好"* ]]; then
    echo comparison
  elif [[ "$q" == *"教程"* ]] || [[ "$q" == *"guide"* ]] || [[ "$q" == *"how to"* ]] || [[ "$q" == *"怎么"* ]] || [[ "$q" == *"步骤"* ]]; then
    echo tutorial
  elif [[ "$q" == *"新闻"* ]] || [[ "$q" == *"news"* ]] || [[ "$q" == *"发布"* ]] || [[ "$q" == *"breaking"* ]]; then
    echo news
  elif [[ "$q" == *"资源"* ]] || [[ "$q" == *"合集"* ]] || [[ "$q" == *"清单"* ]] || [[ "$q" == *"list"* ]] || [[ "$q" == *"awesome"* ]]; then
    echo resource
  elif [[ "$q" == *"最新"* ]] || [[ "$q" == *"最近"* ]] || [[ "$q" == *"现状"* ]] || [[ "$q" == *"状态"* ]] || [[ "$q" == *"目前"* ]] || [[ "$q" == *"进展"* ]] || [[ "$q" == *"status"* ]] || [[ "$q" == *"current"* ]] || [[ "$q" == *"latest"* ]]; then
    echo status
  elif [[ "$q" == *"研究"* ]] || [[ "$q" == *"追踪"* ]] || [[ "$q" == *"汇总"* ]] || [[ "$q" == *"综述"* ]] || [[ "$q" == *"调研"* ]] || [[ "$q" == *"explore"* ]] || [[ "$q" == *"survey"* ]]; then
    echo exploratory
  else
    echo factual
  fi
}

should_use_search_layer() {
  local q
  q="$(lower "$1")"
  [[ "$q" == *"对比"* ]] && return 0
  [[ "$q" == *"区别"* ]] && return 0
  [[ "$q" == *"比较"* ]] && return 0
  [[ "$q" == *"哪个好"* ]] && return 0
  [[ "$q" == *"最新"* ]] && return 0
  [[ "$q" == *"最近"* ]] && return 0
  [[ "$q" == *"状态"* ]] && return 0
  [[ "$q" == *"现状"* ]] && return 0
  [[ "$q" == *"目前"* ]] && return 0
  [[ "$q" == *"research"* ]] && return 0
  [[ "$q" == *"compare"* ]] && return 0
  [[ "$q" == *"status"* ]] && return 0
  [[ "$q" == *"latest"* ]] && return 0
  [[ "$q" == *"current"* ]] && return 0
  return 1
}

run_search_layer_auto() {
  local query="$1"
  local intent
  local variants=()
  local variant
  intent="$(infer_intent "$query")"
  while IFS= read -r variant; do
    [[ -n "$variant" ]] && variants+=("$variant")
  done < <(expand_query_variants "$query")
  if [[ ${#variants[@]} -gt 1 ]]; then
    run_with_save "$SEARCH_LAYER_WRAPPER" --queries "${variants[@]}" --intent "$intent" --mode deep --source exa,tavily,grok,tinyfish
    return
  fi
  run_with_save "$SEARCH_LAYER_WRAPPER" --intent "$intent" --mode deep --source exa,tavily,grok,tinyfish -- "$query"
}

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
  usage
  exit 0
fi

if [[ $# -eq 0 ]]; then
  usage >&2
  exit 1
fi

normalize_mode_aliases "$@"
set -- "${NORMALIZED_ARGS[@]}"

# Parse --save-run early (before routing) and strip it from args
SAVE_RUN_DIR=""
FILTERED=()
SKIP_SAVE=false
LITERAL_ARGS=false
for arg in "$@"; do
  if $LITERAL_ARGS; then
    FILTERED+=("$arg")
    continue
  fi
  if $SKIP_SAVE; then
    if [[ -z "$arg" || "$arg" == -* ]]; then
      echo "[ERROR] --save-run requires a directory argument (prefix option-like paths with ./)" >&2
      exit 2
    fi
    SAVE_RUN_DIR="$arg"
    SKIP_SAVE=false
    continue
  fi
  if [[ "$arg" == "--save-run" ]]; then
    SKIP_SAVE=true
    continue
  fi
  if [[ "$arg" == "--" ]]; then
    LITERAL_ARGS=true
  fi
  FILTERED+=("$arg")
done
if $SKIP_SAVE; then
  echo "[ERROR] --save-run requires a directory argument" >&2
  exit 2
fi
set -- ${FILTERED[@]+"${FILTERED[@]}"}

# Fallback to env var if --save-run not explicitly passed on command line
if [[ -z "$SAVE_RUN_DIR" && -n "${UNIFIED_SEARCH_SAVE_DIR:-}" ]]; then
  SAVE_RUN_DIR="$UNIFIED_SEARCH_SAVE_DIR"
fi

# Prepare legacy passthrough args
LEGACY_SAVE=()
if [[ -n "$SAVE_RUN_DIR" ]]; then
  LEGACY_SAVE=(--save-run "$SAVE_RUN_DIR")
fi

# Helper: run a sub-command wrapper, optionally saving stdout to a file.
# When --save-run is not set, behaviour is identical to exec (replaces process).
run_with_save() {
  local wrapper="$1"
  shift
  if [[ ! -f "$wrapper" ]]; then
    echo "[ERROR] Search route is unavailable; missing implementation: $wrapper" >&2
    return 127
  fi
  if [[ -n "$SAVE_RUN_DIR" ]]; then
    mkdir -p "$SAVE_RUN_DIR"
    local ts mode run_file suffix
    ts="$(date +%Y%m%dT%H%M%S)"
    mode="$(basename "$wrapper" .sh | sed 's/^run-//;s/^unified-search-legacy$/legacy/')"
    run_file="$SAVE_RUN_DIR/${mode}-${ts}.json"
    suffix=0
    while :; do
      if (set -o noclobber; : > "$run_file") 2>/dev/null; then
        break
      fi
      suffix=$((suffix + 1))
      run_file="$SAVE_RUN_DIR/${mode}-${ts}-${suffix}.json"
    done
    echo "[save-run] $mode → $run_file" >&2
    bash "$wrapper" "$@" | tee "$run_file"
    exit "${PIPESTATUS[0]}"
  else
    exec bash "$wrapper" "$@"
  fi
}

cmd="${1:-}"
case "$cmd" in
  search-layer)
    shift
    run_with_save "$SEARCH_LAYER_WRAPPER" "$@"
    ;;
  fetch-thread)
    shift
    run_with_save "$FETCH_THREAD_WRAPPER" "$@"
    ;;
  content-extract)
    shift
    run_with_save "$CONTENT_EXTRACT_WRAPPER" "$@"
    ;;
  mineru-extract)
    shift
    run_with_save "$MINERU_EXTRACT_WRAPPER" "$@"
    ;;
  mineru-parse-documents)
    shift
    run_with_save "$MINERU_PARSE_DOCS_WRAPPER" "$@"
    ;;
  legacy)
    shift
    run_with_save "$LEGACY_SCRIPT" ${LEGACY_SAVE[@]+"${LEGACY_SAVE[@]}"} "$@"
    ;;
  *)
    ;;
esac

force_legacy=0
if has_arg --legacy "$@"; then
  force_legacy=1
fi
if has_arg --topic "$@" || has_arg --days "$@" || has_arg --json "$@"; then
  force_legacy=1
fi

# A leading URL selects a content route; its native parser owns all remaining
# options. URLs used as search option values must not change the route.
url="${1:-}"
if [[ "$url" =~ ^https?:// && "$force_legacy" == "0" ]]; then
  if is_thread_url "$url"; then
    run_with_save "$FETCH_THREAD_WRAPPER" "$@"
  elif is_document_url "$url"; then
    run_with_save "$MINERU_EXTRACT_WRAPPER" "$@"
  else
    run_with_save "$CONTENT_EXTRACT_WRAPPER" --url "$@"
  fi
fi

# Explicit subcommands and URL routes validate their own options. This check
# protects only automatic text routing from silently dropping unknown options.
for arg in "$@"; do
  [[ "$arg" == "--" ]] && break
  case "$arg" in
    --include-domains|--exclude-domains|--start-date|--end-date|--read-top|--content-timeout|--content-max-chars|--content-fallback|--include-domains=*|--exclude-domains=*|--start-date=*|--end-date=*|--read-top=*|--content-timeout=*|--content-max-chars=*|--content-fallback=*|--legacy|--json|--mode|--intent|--freshness|--queries|--source|--extract-refs|--extract-refs-urls|--domain-boost|--num|--timeout|--verify-urls|--topic|--days|--mode=*|--intent=*|--freshness=*|--queries=*|--source=*|--domain-boost=*|--num=*|--timeout=*|--topic=*|--days=*) ;;
    --*)
      echo "[ERROR] Unknown option: $arg (use -- before literal query text beginning with --)" >&2
      exit 2
      ;;
  esac
  case "$arg" in --topic=*|--days=*) force_legacy=1 ;; esac
done

if [[ "$force_legacy" == "0" ]]; then
  local_like_flags=0
  for arg in "$@"; do
    [[ "$arg" == "--" ]] && break
    case "$arg" in
      --include-domains|--exclude-domains|--start-date|--end-date|--read-top|--content-timeout|--content-max-chars|--content-fallback|--include-domains=*|--exclude-domains=*|--start-date=*|--end-date=*|--read-top=*|--content-timeout=*|--content-max-chars=*|--content-fallback=*|--mode|--intent|--freshness|--queries|--source|--extract-refs|--extract-refs-urls|--domain-boost|--num|--timeout|--verify-urls|--mode=*|--intent=*|--freshness=*|--source=*|--num=*|--timeout=*|--domain-boost=*)
        local_like_flags=1
        ;;
    esac
  done
  if [[ "$local_like_flags" == "1" ]]; then
    run_with_save "$SEARCH_LAYER_WRAPPER" "$@"
  fi
fi

query="$(query_text "$@")"
if [[ -n "$query" && "$force_legacy" == "0" ]]; then
  # Default chat /unified_search behavior: route ordinary lookups to deep search-layer.
  # Use --legacy or the explicit "legacy" subcommand to keep the old Tavily + Exa + Google path.
  run_search_layer_auto "$query"
fi

run_with_save "$LEGACY_SCRIPT" ${LEGACY_SAVE[@]+"${LEGACY_SAVE[@]}"} "$@"
