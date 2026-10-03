#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "[ERROR] Python executable not found: $PYTHON_BIN" >&2
  exit 1
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "[ERROR] Python 3.10 or newer is required" >&2
  exit 1
fi

VENV_PY="$SKILL_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  "$PYTHON_BIN" -m venv "$SKILL_DIR/.venv"
fi
if ! "$VENV_PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "[ERROR] Existing .venv requires Python 3.10 or newer; move it aside and rerun setup with a supported PYTHON_BIN" >&2
  exit 1
fi
"$VENV_PY" -m pip install -r "$SKILL_DIR/requirements.txt"
echo "[OK] Environment ready at $SKILL_DIR/.venv"
