#!/usr/bin/env python3
"""Tastebuds as an app: start the local server, open a native window.

Run directly (python3 app/launch.py) or via the installed Tastebuds.app.
The app is a thin wrapper around this file, so code changes apply on the next
launch — no reinstall needed. Your data lives in ~/Tastebuds (override with
TASTEBUDS_HOME) — or next to the code if a library already sits there.
Phone pairing is off until switched on in "Pair a phone" (TASTEBUDS_LAN=1
turns it on from the start, as before).

Windows and Linux: the close button quits (normal manners there).
`--selftest` runs a headless health check of the build and exits (CI uses it).

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

# A windowed build (Windows/macOS) has no console: give stray prints somewhere
# harmless to go instead of None.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w"))

SELFTEST = "--selftest" in sys.argv
if SELFTEST:   # build check: throwaway data folder, recommender in-process, no window
    import tempfile
    os.environ["TASTEBUDS_HOME"] = tempfile.mkdtemp(prefix="tastebuds-selftest-")
    os.environ["TASTEBUDS_INPROC"] = "1"

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


def port_in_use(port):
    """Fast local probe (Windows takes ~2 s to report a refused connection)."""
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
    finally:
        s.close()


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


def selftest():
    """Health check of a build — the release workflow runs it on every
    download it makes: server, page, icons, API, NumPy, and the recommender,
    all in-process, no window. Exit code 0 = healthy; a report is printed and,
    if TASTEBUDS_SELFTEST_REPORT names a file, written there too."""
    import json
    import urllib.request
    server, url = tastebuds.create_server(lan=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def page():
        with urllib.request.urlopen(url, timeout=10) as r:
            assert b"Tastebuds" in r.read(), "page has no title"

    def icon():
        with urllib.request.urlopen(url + "icon-192.png", timeout=10) as r:
            assert r.read(8) == b"\x89PNG\r\n\x1a\n", "icon is not a PNG"

    def api():
        req = urllib.request.Request(url + "api/state", headers={"X-Token": tastebuds.TOKEN})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert "needs_onboarding" in json.loads(r.read().decode("utf-8"))

    def numpy_ok():
        import numpy
        assert float(numpy.ones(3).sum()) == 3.0

    def recommender():
        ok, data = tastebuds.run_recommender(["--offline", "--train-only"])
        assert ok, data

    def window_lib():
        if getattr(sys, "frozen", False):   # the built app must carry pywebview
            import webview  # noqa: F401

    lines, failed = ["Tastebuds %s selftest · python %s · %s" % (
        tastebuds.VERSION, sys.version.split()[0], sys.platform)], 0
    for name, fn in (("page", page), ("icon", icon), ("api", api), ("numpy", numpy_ok),
                     ("recommender", recommender), ("window", window_lib)):
        try:
            fn()
            lines.append("  ok    " + name)
        except Exception as e:
            failed += 1
            lines.append("  FAIL  %s: %r" % (name, e))
    server.shutdown()
    report = "\n".join(lines + ["%d check(s) failed" % failed if failed else "all checks passed"])
    print(report)
    out = os.environ.get("TASTEBUDS_SELFTEST_REPORT")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
    return 1 if failed else 0


def main():
    if SELFTEST:
        sys.exit(selftest())
    try:
        open(LOG_PATH, "w").close()   # fresh log per launch
    except Exception:
        pass
    log("launcher start · tastebuds %s · python %s · %s" % (
        tastebuds.VERSION, sys.version.split()[0], sys.platform))

    # One window per computer: if Tastebuds is already running, show that one.
    # (Checked before binding — on Windows a second bind can quietly succeed.)
    port = tastebuds.LAN_PORT_DEFAULT
    running = existing_tastebuds(port) if port_in_use(port) else None
    if running:
        log("already running at %s — opening it and exiting" % running)
        webbrowser.open(running)
        return
    # The window always lives on 127.0.0.1:8765 (a stable address keeps the
    # page's saved preferences). Pairing adds a LAN listener only when asked.
    lan = os.environ.get("TASTEBUDS_LAN", "0") == "1"
    try:
        server, url = tastebuds.create_server(lan=lan, port=port)
    except OSError:
        log("port %d busy (not Tastebuds) — using a free port" % port)
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

    width, height = 940, 1000
    if sys.platform == "darwin":
        try:                          # fit the visible screen, don't overflow it
            from AppKit import NSScreen
            vf = NSScreen.mainScreen().visibleFrame()
            height = min(1080, int(vf.size.height) - 20)
            width = min(940, int(vf.size.width) - 60)
        except Exception as e:
            log("screen size lookup failed: %s" % e)
    window = webview.create_window("Tastebuds", url, width=width, height=height,
                                   min_size=(680, 620))
    log("window %dx%d" % (width, height))
    state = {"dock": False, "hidden": False, "quitting": False,
             "observer": None, "monitor": None, "term_obs": None, "watchdog": False}

    def arm_quit_watchdog():
        """Once quitting starts, the process WILL exit — politely if Cocoa
        cooperates, hard if it doesn't. No more force-quitting, ever."""
        if state["watchdog"]:
            return
        state["watchdog"] = True

        def hard_exit():
            log("watchdog: still alive — hard exit")
            os._exit(0)               # atomic writes are already on disk

        t = threading.Timer(5.0, hard_exit)   # room for the fullscreen animation
        t.daemon = True
        t.start()

    def fullscreen_nswindows():
        if sys.platform != "darwin":
            return []
        try:
            from AppKit import NSApplication
            return [w for w in (NSApplication.sharedApplication().windows() or [])
                    if int(w.styleMask()) & (1 << 14)]    # NSWindowStyleMaskFullScreen
        except Exception:
            return []

    def exit_fullscreen_then(then, label):
        """Quitting or hiding straight out of a fullscreen Space leaves the
        screen black — leave fullscreen first (main thread), act afterwards."""
        fs = fullscreen_nswindows()
        delay = 0.05
        if fs:
            log("leaving fullscreen before %s" % label)
            delay = 1.0               # let the Space animation finish
            try:
                from Foundation import NSOperationQueue

                def toggle():
                    for w in fs:
                        try:
                            w.toggleFullScreen_(None)
                        except Exception as e:
                            log("toggleFullScreen failed: %s" % e)

                NSOperationQueue.mainQueue().addOperationWithBlock_(toggle)
            except Exception as e:
                log("fullscreen exit dispatch failed: %s" % e)
        t = threading.Timer(delay, then)      # always off the event-handler stack
        t.daemon = True
        t.start()

    def really_quit(source):
        log("quit requested via %s" % source)
        state["quitting"] = True
        arm_quit_watchdog()

        def teardown():
            try:
                window.destroy()
                log("window destroyed")
            except Exception as e:
                log("destroy failed: %s" % e)
            def terminate():
                try:
                    from AppKit import NSApplication
                    NSApplication.sharedApplication().terminate_(None)
                except Exception as e:
                    log("terminate failed: %s" % e)
            t2 = threading.Timer(1.2, terminate)
            t2.daemon = True
            t2.start()

        exit_fullscreen_then(teardown, "quit")

    def bind_mac_lifecycle():
        """Mac manners without touching pywebview's internals:
        - 'did become active' observer re-shows a hidden window (Dock click);
        - a local Cmd+Q key monitor quits FOR REAL (sets the quitting flag so
          the closing handler lets go);
        - a will-terminate observer marks any other quit path as real, too.
        Hide-on-close only activates once the observer is bound, so a failed
        hook can never strand a hidden window."""
        if sys.platform != "darwin":
            log("not macOS — close quits")
            return
        try:
            from AppKit import NSEvent, NSApplication
            from Foundation import NSNotificationCenter, NSOperationQueue

            def on_activate(_note):
                if state["hidden"]:
                    log("dock click — showing window")
                    state["hidden"] = False
                    try:
                        window.show()
                    except Exception as e:
                        log("show failed: %s" % e)

            center = NSNotificationCenter.defaultCenter()
            state["observer"] = center.addObserverForName_object_queue_usingBlock_(
                "NSApplicationDidBecomeActiveNotification", None,
                NSOperationQueue.mainQueue(), on_activate)

            def on_terminate(_note):
                log("app will terminate")
                state["quitting"] = True
                arm_quit_watchdog()

            state["term_obs"] = center.addObserverForName_object_queue_usingBlock_(
                "NSApplicationWillTerminateNotification", None,
                NSOperationQueue.mainQueue(), on_terminate)

            def key_handler(event):
                try:                  # Cmd+Q = 1<<20 command flag, 'q'
                    if (event.modifierFlags() & (1 << 20)) and \
                       (event.charactersIgnoringModifiers() or "").lower() == "q":
                        really_quit("cmd+q")   # async — never from inside the event stack
                        return None            # swallow the keystroke
                except Exception as e:
                    log("key handler error: %s" % e)
                return event

            def install_monitor():
                state["monitor"] = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                    1 << 10, key_handler)          # key-down events
                log("cmd+q monitor installed")

            NSOperationQueue.mainQueue().addOperationWithBlock_(install_monitor)
            state["dock"] = True
            log("dock hook bound — close hides, Dock reopens, Cmd+Q quits")
        except Exception as e:
            log("dock hook FAILED (%s) — close quits, as before" % e)

    def on_closing():
        if state["quitting"]:
            log("closing allowed — quit in progress")
            return True
        if state["hidden"]:
            # a hidden window has no close button: this is app termination
            log("close requested while hidden — allowing quit")
            state["quitting"] = True
            arm_quit_watchdog()
            return True
        if state["dock"]:
            log("close button — hiding to Dock, server keeps serving")
            state["hidden"] = True

            def do_hide():
                try:
                    window.hide()
                except Exception as e:
                    log("hide failed: %s" % e)

            exit_fullscreen_then(do_hide, "hide")   # windowed first, then hide
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
