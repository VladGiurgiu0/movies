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
