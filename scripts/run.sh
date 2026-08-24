#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
if [[ -d .venv ]]; then
  source .venv/bin/activate
fi
python3 -m server
