#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Virtualenv .venv not found. Run ./scripts/install_requirements.sh first."
  exit 1
fi

exec python main.py
