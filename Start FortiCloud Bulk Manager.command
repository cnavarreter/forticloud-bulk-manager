#!/bin/bash
# Double-click to start the FortiCloud Asset Bulk Manager (macOS).
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Install it from https://www.python.org/downloads/ (or run: xcode-select --install)"
  read -r -p "Press Enter to close..."
  exit 1
fi
python3 app.py "$@"
