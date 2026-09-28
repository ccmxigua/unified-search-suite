#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PY="$SKILL_DIR/.venv/bin/python"
VENDOR_DIR="$SKILL_DIR/vendor/openclaw-search-skills/search-layer"
TARGET="$VENDOR_DIR/scripts/search.py"
ENV_LOADER="$SCRIPT_DIR/load-search-env.sh"

if [[ ! -x "$VENV_PY" ]]; then
  echo "[ERROR] unified-search venv python not found: $VENV_PY" >&2
  exit 1
fi
if [[ ! -f "$TARGET" ]]; then
  echo "[ERROR] vendored search-layer script not found: $TARGET" >&2
  exit 1
fi

[[ -f "$ENV_LOADER" ]] && source "$ENV_LOADER"

# The search layer currently exits successfully with an empty result set when
# no selected provider has credentials. Detect that case before starting it so
# callers can distinguish "not configured" from a valid no-hit search.
search_mode="deep"
requested_sources=""
has_query=false
skip_value=""
skip_ref_urls=false
for arg in "$@"; do
  if [[ "$skip_ref_urls" == true ]]; then
    if [[ "$arg" == --* ]]; then
      skip_ref_urls=false
    else
      continue
    fi
  fi
  if [[ -n "$skip_value" ]]; then
    case "$skip_value" in
      mode) search_mode="$arg" ;;
      source) requested_sources="$arg" ;;
    esac
    skip_value=""
    continue
  fi
  case "$arg" in
    --mode) skip_value="mode" ;;
    --mode=*) search_mode="${arg#*=}" ;;
    --source) skip_value="source" ;;
    --source=*) requested_sources="${arg#*=}" ;;
    --extract-refs-urls) skip_ref_urls=true ;;
    --num|--timeout|--intent|--freshness|--domain-boost) skip_value="other" ;;
    --queries) has_query=true ;;
    --*) ;;
    *) has_query=true ;;
  esac
done

if $has_query; then
  has_exa=false
  has_tavily=false
  has_grok=false
  has_tinyfish=false
  if [[ -n "${EXA_API_KEY:-}" ]]; then has_exa=true; fi
  if [[ -n "${TAVILY_API_KEY:-}" ]]; then has_tavily=true; fi
  if [[ -n "${GROK_API_KEY:-}" && -n "${GROK_API_URL:-}" ]]; then has_grok=true; fi
  if [[ -n "${TINYFISH_API_KEY:-}" ]]; then has_tinyfish=true; fi

  use_exa=false
  use_tavily=false
  use_grok=false
  use_tinyfish=false
  if [[ -z "$requested_sources" ]]; then
    use_exa=$has_exa
    use_tavily=$has_tavily
    use_grok=$has_grok
    use_tinyfish=$has_tinyfish
  else
    IFS=',' read -r -a source_list <<< "$requested_sources"
    for source in "${source_list[@]}"; do
      source="${source//[[:space:]]/}"
      case "$source" in
        exa) use_exa=$has_exa ;;
        tavily) use_tavily=$has_tavily ;;
        grok) use_grok=$has_grok ;;
        tinyfish) use_tinyfish=$has_tinyfish ;;
      esac
    done
  fi

  has_provider=false
  case "$search_mode" in
    fast)
      if [[ "$use_exa" == true || "$use_grok" == true ]]; then has_provider=true; fi
      ;;
    answer)
      if [[ "$use_tavily" == true ]]; then has_provider=true; fi
      ;;
    deep)
      if [[ "$use_exa" == true || "$use_tavily" == true || "$use_grok" == true || "$use_tinyfish" == true ]]; then has_provider=true; fi
      ;;
    *) has_provider=true ;; # Let argparse report an invalid mode.
  esac

  if [[ "$has_provider" != true ]]; then
    printf '%s\n' '{"status":"error","error":{"code":"no_search_provider","message":"No configured search provider is available for this mode/source filter."},"count":0,"results":[]}'
    exit 2
  fi
fi

exec "$VENV_PY" "$TARGET" "$@"
