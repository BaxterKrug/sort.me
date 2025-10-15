#!/usr/bin/env bash
set -euo pipefail

# Creates a virtualenv in .venv and installs core requirements.
VENV_DIR=".venv"
REQ_FILE="requirements.txt"
OPT_REQ_FILE="requirements-optional.txt"

python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip setuptools wheel
pip install -r "$REQ_FILE"

if [ -f "$OPT_REQ_FILE" ]; then
  echo "To install optional heavy packages, run: pip install -r $OPT_REQ_FILE"
fi

echo "Installed core requirements into $VENV_DIR"
