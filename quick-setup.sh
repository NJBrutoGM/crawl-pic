#!/usr/bin/env bash
#
# quick-setup.sh - install crawl-pic's dependencies.
#
#   ./quick-setup.sh          install into the current Python environment
#   ./quick-setup.sh --venv   create and use a local .venv instead
#
# Set PYTHON=/path/to/python3 to use a specific interpreter.

set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
USE_VENV=0

usage() {
  cat <<'EOF'
Usage: ./quick-setup.sh [--venv]

Installs the packages listed in requirements.txt.

  --venv    Create a .venv virtual environment and install into it.
  -h, --help  Show this help.

Environment:
  PYTHON    Interpreter to use (default: python3)
EOF
}

for arg in "$@"; do
  case "$arg" in
    --venv) USE_VENV=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown option '$arg'" >&2; usage >&2; exit 2 ;;
  esac
done

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: '$PYTHON' not found. Install Python 3 or set PYTHON=/path/to/python3." >&2
  exit 1
fi

if [ ! -f requirements.txt ]; then
  echo "error: requirements.txt not found in $(pwd)" >&2
  exit 1
fi

make_venv() {
  echo "==> Creating virtual environment in .venv"
  "$PYTHON" -m venv .venv
  # shellcheck disable=SC1091
  . .venv/bin/activate
  PYTHON=python
}

# Quiet, non-interactive install. --no-input keeps pip from ever hanging on a
# prompt; --disable-pip-version-check skips the self-update lookup.
pip_install() {
  "$PYTHON" -m pip install \
    --quiet --no-input --disable-pip-version-check \
    -r requirements.txt
}

if [ "$USE_VENV" -eq 1 ]; then
  make_venv
fi

echo "==> Installing dependencies with $($PYTHON --version 2>&1)"
if ! pip_install; then
  # Most likely PEP 668 ("externally managed environment"). Retry in a venv.
  echo "==> Direct install failed; retrying inside a virtual environment"
  make_venv
  pip_install
fi

echo "==> Verifying imports"
"$PYTHON" - <<'PY'
import sys

import bs4
import requests

print(f"    requests {requests.__version__}")
print(f"    beautifulsoup4 {bs4.__version__}")
print(f"    python {sys.version.split()[0]}")
PY

cat <<'EOF'

Setup complete. Try it out:

    python crawl_pic.py "red pandas" -n 5
    python crawl_pic.py https://example.com/gallery -n 20
EOF

if [ "$USE_VENV" -eq 1 ]; then
  echo "(Activate the environment first with: source .venv/bin/activate)"
fi
