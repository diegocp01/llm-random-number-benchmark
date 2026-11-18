#!/bin/bash
# Double-click to start the benchmark UI. First launch installs the two API SDKs
# into a private .venv folder next to this file.
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ and try again."
  read -r -p "Press Return to close."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "First launch: creating a private Python environment..."
  "$PY" -m venv .venv || { read -r -p "Could not create .venv. Press Return to close."; exit 1; }
fi
if ! .venv/bin/python -c 'import anthropic, openai' >/dev/null 2>&1; then
  echo "Installing the OpenAI and Anthropic SDKs (one time)..."
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt || { read -r -p "Install failed. Press Return to close."; exit 1; }
fi

# caffeinate keeps the Mac awake while the benchmark window is open
exec caffeinate -i .venv/bin/python server.py
