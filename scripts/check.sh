#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
PY="${PROJECT_HUB_TEST_PYTHON:-.venv/bin/python}"
"$PY" -c 'import sys; assert sys.version_info[:2] == (3, 11)'
"$PY" -m unittest discover -s tests
"$PY" -m unittest discover -s deploy/raspberry-pi -p 'test_*.py'
git diff --check
