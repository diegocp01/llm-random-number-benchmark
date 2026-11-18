#!/bin/bash
# Double-click to try the UI with simulated answers. Makes no API calls, costs nothing,
# and keeps its data in data_demo/ so it never mixes with real results.
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
exec python3 server.py --demo --port 8766
