#!/usr/bin/env bash
# dev-setup.sh — create the free-threaded venv and install all dependencies.
# Run from the project root:  bash scripts/dev-setup.sh
#
# Requires:  python3.14t (CPython free-threaded build, ABI tag cp314t).
# Does NOT require uv — uses the stdlib venv + pip.
#
# C-extension packages (pydantic-core, rpds-py, cffi) ship separate cp314t
# wheels on PyPI.  We download them with pip before installing so pip won't
# fall back to a cp314 wheel that will silently fail at import time.

set -euo pipefail

PYTHON="${PYTHON:-python3.14t}"
VENV=".venv"
PIP="$VENV/bin/python -m pip"

if ! command -v "$PYTHON" &>/dev/null; then
    echo "[dev-setup] ERROR: $PYTHON not found. Install the CPython free-threaded build." >&2
    exit 1
fi

echo "[dev-setup] Using Python: $("$PYTHON" --version)"

# --- create venv ---
if [[ ! -d "$VENV" ]]; then
    "$PYTHON" -m venv "$VENV"
    echo "[dev-setup] venv created at $VENV"
else
    echo "[dev-setup] venv already exists at $VENV"
fi

# --- upgrade pip itself first ---
$PIP install --upgrade pip --quiet

# --- download cp314t wheels for C-extension packages BEFORE the big install ---
# pip picks the wheel that matches the running interpreter ABI.  We invoke pip
# under the venv python (cp314t) so it selects the right wheel automatically.
echo "[dev-setup] Downloading cp314t wheels for C extensions…"
WHEEL_DIR="$VENV/cp314t-wheels"
mkdir -p "$WHEEL_DIR"
$PIP download pydantic-core rpds-py cffi \
    --only-binary=:all: \
    --no-deps \
    -d "$WHEEL_DIR" \
    --quiet

# --- install main dependencies (will skip C exts already in wheel-dir) ---
echo "[dev-setup] Installing project dependencies…"
$PIP install mcp[cli] pyusb pyserial colorlog --quiet

# --- force-install the cp314t C-extension wheels (replace any cp314 ones) ---
echo "[dev-setup] Installing cp314t C-extension wheels…"
$PIP install --force-reinstall --no-deps "$WHEEL_DIR"/*.whl --quiet

# --- install project in editable mode (no network needed for pure Python) ---
echo "[dev-setup] Installing project in editable mode…"
$PIP install --no-build-isolation --no-deps -e . --quiet

echo ""
echo "[dev-setup] Done. Activate with:  source $VENV/bin/activate"
echo "[dev-setup] Run tests with:       PYTHONPATH=/usr/lib/python3.14/site-packages $PYTHON test_harness.py -v"
echo "[dev-setup] Start MCP server:     $VENV/bin/python mcp_server.py"
