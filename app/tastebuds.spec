# -*- mode: python -*-
# PyInstaller spec for the Tastebuds desktop app (macOS and Windows).
#
# Built automatically by .github/workflows/release.yml on every version tag;
# by hand on a Mac with `make app` (see Makefile), or on Windows from app\:
#     pyinstaller --noconfirm tastebuds.spec --distpath ..\dist --workpath ..\build
#
# The bundle carries the recommender sources and icons as data; tastebuds.py
# resolves them relative to its own (bundled) location, and user data lives in
# ~/Tastebuds via TASTEBUDS_HOME (set by app/launch.py). The recommender runs
# in-process when frozen (sys.frozen), so no Python subprocess is needed.
# Check a finished build with:  <app executable> --selftest

import os
import re
import sys

block_cipher = None
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"

with open(os.path.join("..", "tastebuds.py"), encoding="utf-8") as f:
    VERSION = re.search(r'^VERSION = "([^"]+)"', f.read(), re.M).group(1)

# Only the recommender's code travels with the app (no caches, no WIP files).
datas = [(os.path.join("..", "ml-recommender", n), "ml-recommender")
         for n in ("recommend.py", "tmdb_features.py", "requirements.txt")]
datas += [(os.path.join("..", "assets", n), "assets")
          for n in ("icon-64.png", "icon-180.png", "icon-192.png", "icon-512.png", "tmdb-logo.svg")
          if os.path.exists(os.path.join("..", "assets", n))]

a = Analysis(
    ["launch.py"],
    pathex=[".."],
    datas=datas,
    # recommend.py is loaded as a file at runtime, so name the modules it
    # needs that nothing else imports.
    hiddenimports=["numpy", "webview", "argparse", "hashlib", "certifi"],
    excludes=["matplotlib", "pandas", "PIL", "cv2", "tkinter"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Tastebuds",
    console=False,
    icon=os.path.join("..", "assets", "icon.ico") if IS_WIN else None,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="Tastebuds")

if IS_MAC:
    app = BUNDLE(
        coll,
        name="Tastebuds.app",
        icon=os.path.join("..", "assets", "icon.icns"),
        bundle_identifier="com.vladgiurgiu.tastebuds",
        version=VERSION,
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "NSLocalNetworkUsageDescription":
                "Tastebuds serves its page to your own phone and tablet over your Wi-Fi.",
        },
    )
