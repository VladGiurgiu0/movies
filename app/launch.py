#!/usr/bin/env python3
"""Tastebuds as an app: start the local server, open a native window.

Run directly (python3 app/launch.py) or via the installed Tastebuds.app.
The app is a thin wrapper around this file, so code changes apply on the next
launch — no reinstall needed. Your data lives in ~/Tastebuds (override with
TASTEBUDS_HOME) — or next to the code if a library already sits there. LAN
pairing is on by default (TASTEBUDS_LAN=0 turns it off).

macOS lifecycle: the window's close button HIDES the window — the app stays in
the Dock and the server keeps serving your paired phone or tablet. Click the
Dock icon to bring it back; quit for real with Cmd+Q or the Dock icon's
right-click menu. A short log of what the launcher did lands in app.log next
to your data, for easy debugging.
"""
import os
import sys
import time
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Data folder: an explicit TASTEBUDS_HOME wins; otherwise, if this clone already
# holds a library next to the code (the classic run-from-the-repo setup), keep
# using it; only a fresh install adopts ~/Tastebuds.
if "TASTEBUDS_HOME" not in os.environ and not os.path.exists(os.path.join(ROOT, "movies.md")):
    os.environ["TASTEBUDS_HOME"] = os.path.expanduser("~/Tastebuds")

import tastebuds  # noqa: E402

LOG_PATH = os.path.join(tastebuds.DATA_DIR, "app.log")


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def existing_tastebuds(port):
    """Is another Tastebuds already serving this port? Its local URL, or None."""
    import urllib.request
    url = "http://127.0.0.1:%d/" % port
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            if b"Tastebuds" in r.read(4096):
                return url
    except Exception:
        pass
    return None


def main():
    try:
        open(LOG_PATH, "w").close()   # fresh log per launch
    except Exception:
        pass
    log("launcher start · python %s" % sys.version.split()[0])

    lan = os.environ.get("TASTEBUDS_LAN", "1") == "1"
    try:
        server, url = tastebuds.create_server(lan=lan)
    except OSError:
        running = existing_tastebuds(tastebuds.LAN_PORT_DEFAULT)
        if running:                    # another Tastebuds is live — just show it
            log("already running at %s — opening it and exiting" % running)
            webbrowser.open(running)
            return
        server, url = tastebuds.create_server(lan=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log("server up at %s · lan=%s" % (url, tastebuds.LAN_MODE))

    try:
        import webview
        log("pywebview %s" % getattr(webview, "__version__", "?"))
    except Exception as e:
        webview = None
        log("no pywebview (%s) — browser fallback" % e)
    if webview is None:
        webbrowser.open(url)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        server.shutdown()
        return

    window = webview.create_window("Tastebuds", url, width=940, height=1000)
    state = {"dock": False, "hidden": False, "observer": None}

    def bind_mac_lifecycle():
        """Dock-reopen without touching pywebview's internals: observe the app's
        'did become active' notification (fires when the Dock icon is clicked or
        the app is Cmd+Tabbed to) and re-show the window if we hid it. Only when
        this observer is in place does the close button switch to hide-mode —
        so a failed hook can never strand a hidden window."""
        if sys.platform != "darwin":
            log("not macOS — close quits")
            return
        try:
            from Foundation import NSNotificationCenter, NSOperationQueue

            def on_activate(_note):
                if state["hidden"]:
                    log("dock click — showing window")
                    state["hidden"] = False
                    try:
                        window.show()
                    except Exception as e:
                        log("show failed: %s" % e)

            state["observer"] = NSNotificationCenter.defaultCenter() \
                .addObserverForName_object_queue_usingBlock_(
                    "NSApplicationDidBecomeActiveNotification", None,
                    NSOperationQueue.mainQueue(), on_activate)
            state["dock"] = True
            log("dock hook bound — close hides, Dock reopens")
        except Exception as e:
            log("dock hook FAILED (%s) — close quits, as before" % e)

    def on_closing():
        if state["dock"]:
            log("close button — hiding to Dock, server keeps serving")
            state["hidden"] = True
            try:
                window.hide()
            except Exception as e:
                log("hide failed: %s" % e)
            return False              # cancel the real close
        log("close button — quitting (no dock hook)")
        return True

    try:
        window.events.closing += on_closing
    except Exception as e:
        log("closing handler not attachable: %s" % e)
    webview.start(bind_mac_lifecycle)
    log("window loop ended — shutting down")
    server.shutdown()


if __name__ == "__main__":
    main()
