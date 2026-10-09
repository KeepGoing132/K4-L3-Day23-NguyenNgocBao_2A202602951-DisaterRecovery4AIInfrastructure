#!/usr/bin/env bash
# Complete the prescribed Linux bare --mock drill and refresh its reports.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(uname -s)" != "Linux" ]; then
  echo "Run this script inside Linux/Ubuntu/WSL, not Git Bash on Windows." >&2
  exit 1
fi
mkdir -p run
if ! run/venv-linux/bin/python -m pip --version >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then
  if ! command -v git >/dev/null 2>&1 || ! python3 -m venv run/venv-linux; then
    if [ "$(id -u)" = "0" ]; then
      apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-venv git
    else
      sudo apt-get update
      sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-venv git
    fi
    python3 -m venv run/venv-linux
  fi
fi
source run/venv-linux/bin/activate
python -m pip install -r requirements.txt
python dr/drill.py
python dr/write_reports.py
python -m pytest tests/ dr/test_workflow.py -v | tee run/validation-linux.log
python dr/package_submission.py
echo "Linux bare --mock drill, reports, tests, and submission bundle completed."
