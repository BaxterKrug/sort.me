#!/usr/bin/env bash
set -euo pipefail

# Safe bootstrap for macOS: creates .venv and installs a small core set of packages
# Run from project root: ./scripts/bootstrap.sh

PYENV=.venv
PYTHON=${PYTHON:-python3}

if [ ! -x "$(command -v $PYTHON)" ]; then
	echo "Python not found: $PYTHON"
	exit 1
fi

# Create venv
$PYTHON -m venv $PYENV
source ${PYENV}/bin/activate
pip install --upgrade pip

# Install a core, lightweight set of packages first
pip install -r requirements-core.txt

echo "Bootstrap complete. Activate with: source ${PYENV}/bin/activate"
