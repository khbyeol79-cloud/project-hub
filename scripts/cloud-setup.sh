#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if command -v python3.11 >/dev/null; then
  python3.11 -m venv .venv
elif command -v uv >/dev/null; then
  uv venv --seed --python 3.11 .venv
else
  echo 'Python 3.11 (with venv), or uv, is required.' >&2
  exit 1
fi
.venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 11)'
.venv/bin/python -m pip install -r requirements.txt -r organizer/requirements-web.txt
echo 'Ready. Run: bash scripts/check.sh'
