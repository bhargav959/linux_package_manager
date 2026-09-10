#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Create the venv using the currently installed Python (3.14 is supported).
python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate

python -m pip install --upgrade pip
# Avoid falling back to a source build of pydantic-core.
python -m pip install --only-binary=pydantic-core -r requirements.txt

exec uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
