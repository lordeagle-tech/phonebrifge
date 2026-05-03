#!/usr/bin/env bash
# Phone Bridge — one-command setup
# Run from inside the phone-bridge directory:  bash setup.sh

set -e

PYTHON=python3

echo ""
echo "======================================================"
echo "  Phone Bridge — Setup"
echo "======================================================"
echo ""

# ── Python check ──────────────────────────────────────────
if ! command -v "$PYTHON" &>/dev/null; then
  echo "ERROR: python3 not found. Install it first:"
  echo "  sudo apt install python3 python3-venv python3-pip"
  exit 1
fi

PY_VER=$("$PYTHON" -c "import sys; print(sys.version_info[:2])")
echo "  Python : $("$PYTHON" --version)"

# ── Virtual environment ───────────────────────────────────
if [ ! -d ".venv" ]; then
  echo "  Creating virtual environment (.venv)…"
  "$PYTHON" -m venv .venv
fi

echo "  Activating .venv…"
# shellcheck disable=SC1091
source .venv/bin/activate

echo "  Installing Python dependencies…"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# ── System packages hint ──────────────────────────────────
echo ""
echo "  Optional system packages (install if needed):"
echo "    sudo apt install xclip adb scrcpy python3-xlib"
echo ""
echo "======================================================"
echo "  Setup complete!"
echo ""
echo "  Start the bridge:"
echo "    source .venv/bin/activate"
echo "    python run.py"
echo ""
echo "  Or with a fixed PIN:"
echo "    python run.py --pin 123456"
echo "======================================================"
echo ""
