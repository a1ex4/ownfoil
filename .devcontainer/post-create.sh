#!/usr/bin/env bash
# Prepare the development environment: uv, Python, virtualenv and dependencies.
# Runs once when the dev container / Codespace is created.
set -euo pipefail

PYTHON_VERSION="3.14"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${REPO_DIR}/.venv"

# uv installs itself in ~/.local/bin
export PATH="${HOME}/.local/bin:${PATH}"

# 1. Install uv (the base image may already provide it)
if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
uv --version

# 2. Install the Python version used by the Ownfoil image (see Dockerfile)
echo "Installing Python ${PYTHON_VERSION}..."
uv python install "${PYTHON_VERSION}"

# 3. Create the virtualenv
echo "Creating virtualenv in ${VENV_DIR}..."
uv venv --python "${PYTHON_VERSION}" --allow-existing "${VENV_DIR}"

# 4. Install the application requirements, plus pytest to run the test suite
echo "Installing requirements..."
uv pip install --python "${VENV_DIR}/bin/python" --requirement "${REPO_DIR}/requirements.txt" pytest

# 5. Activate the virtualenv in interactive shells
for rc in "${HOME}/.bashrc" "${HOME}/.zshrc"; do
    [ -f "${rc}" ] || continue
    grep -qxF "source ${VENV_DIR}/bin/activate" "${rc}" || {
        printf '\n# Activate the Ownfoil virtualenv\nsource %s/bin/activate\n' "${VENV_DIR}" >> "${rc}"
    }
done

echo "Environment ready: $(${VENV_DIR}/bin/python --version)"
