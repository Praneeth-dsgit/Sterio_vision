#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUNBUFFERED=1
if [[ -d env_stream ]]; then
  # shellcheck disable=SC1091
  source env_stream/bin/activate
elif [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
elif [[ -d venv ]]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi
python -m server
