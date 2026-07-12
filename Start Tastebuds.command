#!/bin/bash
# Start Tastebuds — double-click me (macOS).
# Runs the app straight from this folder: native window if pywebview is
# installed, otherwise your browser. Data lives in ~/Tastebuds.
cd "$(dirname "$0")"
exec python3 app/launch.py
