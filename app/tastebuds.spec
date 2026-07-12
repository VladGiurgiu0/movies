# -*- mode: python -*-
# PyInstaller spec for the Tastebuds macOS app.
# Build on a Mac, from the repo root:   make app     (see Makefile)
#
# The bundle carries the recommender sources and icons as data; tastebuds.py
# resolves them relative to its own (bundled) location, and user data lives in
# ~/Tastebuds via TASTEBUDS_HOME (set by app/launch.py). The recommender runs
# in-process when frozen (sys.frozen), so no Python subprocess is needed.

block_cipher = None

a = Analysis(
    ["launch.py"],
    pathex=[".."],
    datas=[("../ml-recommender", "ml-recommender"),
           ("../assets", "assets")],
    hiddenimports=["numpy", "webview"],
    excludes=["matplotlib", "pandas", "PIL", "cv2"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Tastebuds",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="Tastebuds")
app = BUNDLE(
    coll,
    name="Tastebuds.app",
    icon="../assets/icon.icns",
    bundle_identifier="com.vladgiurgiu.tastebuds",
    info_plist={
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "NSLocalNetworkUsageDescription":
            "Tastebuds serves its page to your own phone and tablet over your Wi-Fi.",
    },
)
