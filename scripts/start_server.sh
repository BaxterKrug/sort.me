#!/usr/bin/env bash
set -euo pipefail

# Start the FastAPI server using the project's venv and main:app
VENV_DIR=".venv"
if [ ! -d "$VENV_DIR" ]; then
  echo "Virtualenv $VENV_DIR not found. Run ./scripts/install_requirements.sh first."
  exit 1
fi

source "$VENV_DIR/bin/activate"

# Run uvicorn importing main from repo root (no --app-dir)
exec uvicorn main:app --host 0.0.0.0 --port 8000
