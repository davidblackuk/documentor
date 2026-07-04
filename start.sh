#!/bin/bash
#
# start.sh — activate the venv and launch the DocuMentor web server.
#
# The server binds to http://127.0.0.1:8000 — open that in your browser.
# Ctrl+C to stop.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "$SCRIPT_DIR/bin/activate"

echo "DocuMentor starting → http://127.0.0.1:8000"
uvicorn server:app --app-dir "$SCRIPT_DIR" --host 127.0.0.1 --port 8000
