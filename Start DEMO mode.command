#!/bin/bash
# Double-click to start the app against a built-in FAKE FortiCloud (nothing is sent to Fortinet).
cd "$(dirname "$0")" || exit 1
python3 app.py --demo --port 8790
