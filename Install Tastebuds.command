#!/bin/bash
# Install Tastebuds — double-click me (macOS).
#
# What this does, in plain sight:
#   1. checks Python 3 (macOS offers to install its command line tools if missing)
#   2. installs NumPy for the recommender (and tries pywebview for a native window)
#   3. creates your data folder at ~/Tastebuds
#   4. builds the app icon and assembles Tastebuds.app in ~/Applications
#   5. opens the app
# Everything stays on your machine. Run it again anytime — it only refreshes.

cd "$(dirname "$0")"
REPO="$(pwd)"
echo ""
echo "  Installing Tastebuds from: $REPO"
echo ""

# --- 1. Python 3 -------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "  Python 3 is not available yet."
  echo "  macOS will now offer to install its Command Line Tools (a standard"
  echo "  Apple download that includes Python). Accept it, wait for it to"
  echo "  finish, then double-click this installer again."
  xcode-select --install >/dev/null 2>&1
  read -n 1 -s -r -p "  Press any key to close this window..."
  exit 1
fi
PY="$(command -v python3)"
if ! "$PY" -c "import sys" >/dev/null 2>&1; then
  echo "  Python 3 needs its tools installed — accept the macOS dialog that just"
  echo "  appeared (or run 'xcode-select --install'), then run this again."
  read -n 1 -s -r -p "  Press any key to close this window..."
  exit 1
fi
echo "  Python 3: $PY"

# --- 2. Python packages (best effort; the rater itself needs none) -----------
if [ "${TASTEBUDS_INSTALL_NO_PIP:-0}" != "1" ]; then
  echo "  Installing NumPy (powers the recommender)..."
  "$PY" -m pip install --user --quiet numpy 2>/dev/null \
    || "$PY" -m pip install --user --quiet --break-system-packages numpy 2>/dev/null \
    || echo "  (couldn't install NumPy — the rater still works; the Train/Recommend panel will guide you)"
  echo "  Trying pywebview (native window; optional)..."
  "$PY" -m pip install --user --quiet pywebview 2>/dev/null \
    || "$PY" -m pip install --user --quiet --break-system-packages pywebview 2>/dev/null \
    || echo "  (no pywebview — Tastebuds will open in your browser instead, which is fine)"
fi

# --- 3. Your data folder ------------------------------------------------------
mkdir -p "$HOME/Tastebuds"
echo "  Data folder: $HOME/Tastebuds  (your ratings live here, as plain files)"

# --- 4. App icon (iconutil ships with macOS) ----------------------------------
if command -v iconutil >/dev/null 2>&1 && [ -d "$REPO/assets/icon.iconset" ]; then
  iconutil -c icns "$REPO/assets/icon.iconset" -o "$REPO/assets/icon.icns" 2>/dev/null || true
fi

# --- 5. Assemble Tastebuds.app in ~/Applications ------------------------------
APP="$HOME/Applications/Tastebuds.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Tastebuds</string>
  <key>CFBundleDisplayName</key><string>Tastebuds</string>
  <key>CFBundleIdentifier</key><string>com.vladgiurgiu.tastebuds</string>
  <key>CFBundleExecutable</key><string>Tastebuds</string>
  <key>CFBundleIconFile</key><string>icon.icns</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSLocalNetworkUsageDescription</key>
  <string>Tastebuds serves its page to your own phone and tablet over your Wi-Fi.</string>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/Tastebuds" <<LAUNCH
#!/bin/bash
exec "$PY" "$REPO/app/launch.py"
LAUNCH
chmod +x "$APP/Contents/MacOS/Tastebuds"
[ -f "$REPO/assets/icon.icns" ] && cp "$REPO/assets/icon.icns" "$APP/Contents/Resources/icon.icns"
echo "  Installed: $APP"

# --- 6. Off we go --------------------------------------------------------------
echo ""
echo "  Done. Tastebuds is in ~/Applications (find it with Spotlight or Launchpad,"
echo "  and drag it to the Dock if you like). Starting it now..."
echo ""
if [ "${TASTEBUDS_INSTALL_NO_OPEN:-0}" != "1" ]; then
  open "$APP" 2>/dev/null || true
fi
