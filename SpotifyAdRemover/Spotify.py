"""Spotify Ads Skipper - tray application.

Spotify delivers three kinds of advertising through three different paths, so
there are three mechanisms:

  Display ads (home banners, takeovers, video overlays) ship inside the client's
  own UI bundle -> xpui_patch hides them there.

  Audio ads are fetched by the native core from /ads/v3/ads on the same host as
  everything else. Blocking that request makes the client treat the slot as
  empty and continue straight to the next track, with no gap -> ad_proxy, but
  only in Seamless mode, which the user must switch on deliberately.

  With Seamless mode off, audio ads still play and are merely muted -> ad_watch.

Seamless mode is opt-in because it needs a local certificate authority in the
user's trust store. That CA is generated per installation and never shipped: a
CA baked into the executable would put one private key on every user's machine,
letting anyone who downloaded the app impersonate any HTTPS site for all the
others.
"""

import atexit
import json
import os
import sys
import ctypes
import threading

from pystray import Icon, MenuItem as item, Menu
from PIL import Image, ImageDraw

import spotify_env
import xpui_patch
from ad_watch import AdWatcher

tray_icon = None
watcher = None
proxy = None
pac_server = None

CONFIG_PATH = os.path.join(
    os.environ.get("APPDATA", ""), "SpotifyAdsSkipper", "settings.json"
)

SEAMLESS_WARNING = (
    "Seamless mode blocks Spotify's ad requests outright, so tracks run "
    "back-to-back with no silence.\n\n"
    "To read those requests it must decrypt Spotify's HTTPS traffic, which "
    "requires installing a local certificate authority into your user "
    "certificate store.\n\n"
    "What that means:\n"
    "- The CA is generated on THIS machine and never leaves it.\n"
    "- It can only vouch for spotify.com, scdn.co and spotifycdn.com. It "
    "cannot be used against your bank, your email or any other site.\n"
    "- Traffic to those Spotify domains is decrypted on this machine while "
    "the mode is on. That includes Spotify pages open in your browser, so "
    "your Spotify login travels through it too.\n"
    "- Everything else connects directly and is never touched.\n"
    "- The CA stays installed until you turn this mode off or uninstall the "
    "app - not just while the app is running.\n\n"
    "A certificate authority is still a sensitive thing to install. If you are "
    "not comfortable with that, leave this off - ads will simply be muted "
    "instead.\n\n"
    "Enable Seamless mode?"
)


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


LOG_MAX_BYTES = 512 * 1024

DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", "") or os.environ.get("APPDATA", ""),
    "SpotifyAdsSkipper",
)

_HOME = os.environ.get("USERPROFILE", "")


def redact(text):
    """Swap the user's profile path for %USERPROFILE% in anything logged.

    These files are what people attach to bug reports, and the exception
    strings Windows produces are full of absolute paths - which means full of
    the person's account name.
    """
    text = str(text)
    if _HOME:
        text = text.replace(_HOME, "%USERPROFILE%")
        text = text.replace(_HOME.replace("\\", "\\\\"), "%USERPROFILE%")
    return text


def log_debug(message):
    try:
        from datetime import datetime

        # Under the user's data directory, not next to the executable: a copy
        # installed somewhere read-only would otherwise log nowhere at all.
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, "debug_log.txt")
        message = redact(message)
        # Rotate rather than grow without bound: this runs for as long as the
        # machine is on, and a stale multi-megabyte log is useless for support.
        try:
            if os.path.getsize(path) > LOG_MAX_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_config(config):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
    except OSError:
        pass


def seamless_enabled():
    return bool(load_config().get("seamless", False))


def create_image():
    try:
        return Image.open(resource_path("cat.ico"))
    except Exception:
        image = Image.new("RGB", (64, 64), "black")
        ImageDraw.Draw(image).ellipse((10, 10, 54, 54), fill="#1DB954")
        return image


def notify(message, title="Spotify Skipper"):
    if tray_icon:
        try:
            tray_icon.notify(message, title)
        except Exception:
            pass


MB_YESNO = 0x04
MB_ICONWARNING = 0x30
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000
IDYES = 6


def confirm(text, title="Spotify Skipper"):
    """Ask a yes/no question.

    SETFOREGROUND and TOPMOST are not cosmetic here: launched from a tray app
    with no owner window, the dialog can otherwise come up behind everything and
    without focus, which looks exactly like a dialog that ignores clicks.
    """
    flags = MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST
    return ctypes.windll.user32.MessageBoxW(0, text, title, flags) == IDYES


# -- display ads -----------------------------------------------------------

def sync_patch():
    """Re-apply the UI patch when Spotify has replaced its bundle."""
    if not spotify_env.is_spotify_installed():
        return False, "Spotify not found."
    if not xpui_patch.needs_repatch():
        return True, "Display ads already blocked."

    was_running = spotify_env.is_spotify_running()
    if was_running:
        notify("Updating ad block - Spotify will restart.")
        if not spotify_env.stop_spotify():
            return False, "Close Spotify to block display ads."

    # try/finally, because anything raising between here and the relaunch would
    # otherwise leave the user's Spotify closed with no explanation and no way
    # back except starting it by hand.
    try:
        ok, message = xpui_patch.apply_patch()
        log_debug("Patch: %s (%s)" % (ok, message))
        return ok, message
    finally:
        if was_running:
            spotify_env.start_spotify()


# -- seamless mode ---------------------------------------------------------

def start_seamless():
    """Bring up the proxy, PAC server and routing. Returns (ok, message).

    Every failure path tears down what it already built, CA trust included.
    Half-finished state here is the worst outcome available: a trusted root
    certificate installed for a mode that never came up, with no obvious way
    for the user to get rid of it again.
    """
    global proxy, pac_server

    import time

    import ad_proxy
    import proxy_ca
    import proxy_config

    trusted_now = False
    try:
        proxy_ca.load_ca()
        if not proxy_ca.is_trusted():
            ok, message = proxy_ca.trust()
            if not ok:
                return False, message
            trusted_now = True

        proxy = ad_proxy.AdProxy(port=ad_proxy.DEFAULT_PORT, log=log_debug)
        proxy.start()

        pac_server = proxy_config.PacServer(ad_proxy.DEFAULT_PORT, log=log_debug)
        pac_server.start()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if pac_server.alive or pac_server.bind_error:
                break
            time.sleep(0.05)

        if not proxy.listening:
            raise RuntimeError(
                "the ad proxy could not take port %d (%s)"
                % (ad_proxy.DEFAULT_PORT, proxy.bind_error or "unknown")
            )
        if not pac_server.alive:
            raise RuntimeError(
                "the routing server could not take port %d (%s)"
                % (pac_server.port, pac_server.bind_error or "unknown")
            )
        # Confirm we are the ones answering on that port before telling Windows
        # to fetch its proxy configuration from it.
        if not pac_server.serves_our_pac():
            raise RuntimeError(
                "another program is answering on port %d" % pac_server.port
            )

        # Spotify's own prefs proxy keys would fight the PAC; clear them.
        proxy_config.clear_spotify_prefs()
        ok, message = proxy_config.enable(pac_server.url)
        if not ok:
            raise RuntimeError(message)
    except Exception as exc:
        log_debug("Seamless startup failed: %s" % exc)
        stop_seamless(untrust_ca=trusted_now)
        return False, "Seamless mode could not start: %s" % exc

    log_debug("Seamless mode on (%s)" % pac_server.url)
    return True, "Seamless mode on - ads are dropped, not muted."


def reconcile_routing():
    """Clear routing left behind by a previous run that ended badly.

    Windows shutting down, a kill from Task Manager or a crash all skip the
    teardown, so AutoConfigURL can outlive the app. A stale entry points every
    application that reads a PAC at a port nobody is listening on - and worse,
    at a port anything on the machine is then free to claim.
    """
    if seamless_enabled():
        return  # About to be set up again properly.
    try:
        import proxy_config

        if proxy_config.is_enabled():
            ok, message = proxy_config.disable()
            log_debug("Cleared stale proxy routing: %s (%s)" % (ok, message))
    except Exception as exc:
        log_debug("Could not check for stale routing: %s" % exc)


def ensure_spotify_routed():
    """Restart Spotify if it was already running when routing came up.

    This app and Spotify both start at login, in no guaranteed order. When
    Spotify wins it asks for the PAC before our server is listening, fails, and
    settles on direct connections it never revisits.

    Deciding by observation turned out to be a trap. "Is a Spotify connection
    pointed at our port" says yes as soon as one fresh connection goes through
    the new PAC, while the established ones - the ones carrying the ad requests
    - stay direct. Counting served requests has the same hole. The reliable
    fact is simply whether Spotify predates the routing: if it does, some of its
    sockets are already outside the proxy, and only a restart fixes that. If it
    starts afterwards it reads the PAC itself and needs nothing.
    """
    if proxy is None:
        return False

    if not spotify_env.is_spotify_running():
        return True

    log_debug("Spotify predates the proxy - restarting it so all traffic is routed.")
    if spotify_env.stop_spotify():
        spotify_env.start_spotify()
        return True

    log_debug("Could not restart Spotify; seamless mode will not apply yet.")
    return False


def stop_seamless(untrust_ca=True):
    """Tear everything down. Must run on every exit path."""
    global proxy, pac_server

    try:
        import proxy_config

        proxy_config.disable()
    except Exception as exc:
        log_debug("Could not clear PAC: %s" % exc)

    if pac_server:
        pac_server.stop()
        pac_server = None
    if proxy:
        proxy.stop()
        proxy = None

    if untrust_ca:
        try:
            import proxy_ca

            proxy_ca.untrust()
        except Exception as exc:
            log_debug("Could not remove CA: %s" % exc)

    log_debug("Seamless mode off")


def guarded(name):
    """Run a worker so a failure surfaces instead of killing its thread.

    Every long job here runs on a daemon thread, and an escaping exception used
    to end that thread in complete silence - no log, no dialog, and with
    stderr absent in a windowed build, nothing anywhere. The user was left with
    a menu item that appeared to do nothing at all.
    """
    def decorate(fn):
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                log_debug("%s failed: %r" % (name, exc))
                notify("%s failed: %s" % (name, exc))
        wrapper.__name__ = getattr(fn, "__name__", name)
        return wrapper
    return decorate


@guarded("Seamless mode")
def _toggle_seamless_worker(icon):
    config = load_config()
    if config.get("seamless"):
        stop_seamless()
        config["seamless"] = False
        save_config(config)
        notify("Seamless mode off. Audio ads will be muted instead.")
    else:
        if not confirm(SEAMLESS_WARNING):
            return
        ok, message = start_seamless()
        config["seamless"] = bool(ok)
        save_config(config)
        notify(message)
        if ok and spotify_env.is_spotify_running():
            spotify_env.stop_spotify()
            spotify_env.start_spotify()
    icon.update_menu()


def on_toggle_seamless(icon, _item):
    """Hand the work to a thread and return at once.

    While a tray menu is up, Windows holds a mouse capture for it. A modal
    dialog opened straight from the callback appears but never receives the
    clicks - it just sits there. Returning immediately lets the menu close and
    release the capture, and the dialog then behaves normally on its own thread.
    The same return also keeps the tray responsive during the slow parts (key
    generation, certutil, restarting Spotify).
    """
    threading.Thread(target=_toggle_seamless_worker, args=(icon,), daemon=True).start()


# -- tray ------------------------------------------------------------------

@guarded("Restore")
def _restore_worker():
    was_running = spotify_env.is_spotify_running()
    if was_running and not spotify_env.stop_spotify():
        notify("Close Spotify first, then try again.")
        return
    try:
        ok, message = xpui_patch.restore()
        log_debug("Restore: %s (%s)" % (ok, message))
        notify(message)
    finally:
        if was_running:
            spotify_env.start_spotify()


def on_restore(icon, _item):
    # Off the callback thread for the same reason as the seamless toggle:
    # stopping and relaunching Spotify takes seconds and would hang the tray.
    threading.Thread(target=_restore_worker, daemon=True).start()


def on_quit(icon, _item):
    shutdown()
    icon.stop()


_shut_down = threading.Event()


def shutdown():
    """Leave nothing behind: no routing, no mute, no CA."""
    if _shut_down.is_set():
        return
    _shut_down.set()
    if watcher:
        watcher.stop()
        watcher.join(timeout=3)
    if load_config().get("seamless"):
        # Routing must go even though the setting stays on for next launch.
        stop_seamless(untrust_ca=False)


def shutdown_quietly():
    """Teardown for paths where nothing is left to report a failure to."""
    try:
        shutdown()
    except Exception:
        pass


def start_status_refresher(icon):
    """Keep the tray menu's status line current.

    pystray's Windows backend builds the popup menu once and caches the handle,
    so a callable item's text is only re-evaluated when update_menu() is called.
    Without this the status is frozen at whatever was true the instant the icon
    was created - which is before the startup thread has brought seamless mode
    up, and therefore reads "FAILED" forever even though everything works.
    """

    def loop():
        import time

        while True:
            time.sleep(5)
            try:
                icon.update_menu()
            except Exception:
                return

    threading.Thread(target=loop, daemon=True, name="StatusRefresher").start()


def status_text(_item=None):
    if seamless_enabled():
        # Report the parts separately. A dead PAC server leaves Windows unable
        # to fetch the routing rules, so Spotify connects directly and the proxy
        # sits idle - which otherwise reads as a healthy "0 dropped" and hides
        # the failure completely.
        if proxy is None:
            return "Status: seamless FAILED - proxy not running"
        if not proxy.listening:
            # The thread can be alive while owning no port at all.
            return "Status: seamless FAILED - port %d is taken" % proxy.port
        if pac_server is None or not pac_server.alive:
            return "Status: seamless BROKEN - routing server down, restart the app"
        if not proxy.blocked and not spotify_env.is_spotify_running():
            # A zero here means "nothing has needed blocking yet", which reads
            # exactly like a failure. Say why it is zero instead.
            return "Status: seamless ready - Spotify is not running"
        return "Status: seamless (%d ad requests dropped)" % (proxy.blocked,)
    if watcher and watcher.ad_playing:
        return "Status: muting an ad"
    return "Status: muting mode (%d ads muted)" % (watcher.ads_muted if watcher else 0,)


def ca_is_installed():
    try:
        import proxy_ca

        return proxy_ca.is_trusted()
    except Exception:
        return False


@guarded("Remove local CA")
def _remove_ca_worker():
    import proxy_ca

    if seamless_enabled():
        stop_seamless(untrust_ca=False)
        config = load_config()
        config["seamless"] = False
        save_config(config)
    ok, message = proxy_ca.untrust()
    log_debug("Remove CA: %s (%s)" % (ok, message))
    notify(message)


def on_remove_ca(icon, _item):
    threading.Thread(target=_remove_ca_worker, daemon=True).start()


def construct_menu():
    return Menu(
        item(status_text, None, enabled=False),
        Menu.SEPARATOR,
        item("Seamless mode (advanced)", on_toggle_seamless, checked=lambda _i: seamless_enabled()),
        # Always reachable, not just while the mode is on. A CA can be left
        # behind by a crash or a failed start-up, and without this the only way
        # to get rid of it is certmgr.msc.
        item("Remove local certificate", on_remove_ca, visible=lambda _i: ca_is_installed()),
        item("Restore original Spotify UI", on_restore),
        item("Close", on_quit),
    )


def run_selftest():
    """Report whether each mechanism can actually run here.

    Frozen builds are the reason this exists: a dependency that imports fine
    from source can still be missing from the bundle, and the watcher would
    then fail silently rather than loudly. Users can run it to diagnose too.
    """
    lines = []
    ok = True

    def probe(label, fn):
        nonlocal ok
        try:
            lines.append("  OK    %-26s %s" % (label, fn()))
        except Exception as exc:
            ok = False
            lines.append("  FAIL  %-26s %s: %s" % (label, type(exc).__name__, exc))

    lines.append("Spotify Ads Skipper - self test")
    lines.append("frozen: %s" % bool(getattr(sys, "frozen", False)))
    lines.append("")

    probe("Spotify installed", lambda: spotify_env.is_spotify_installed())
    probe("Spotify version", lambda: spotify_env.spotify_version() or "not found")
    probe("UI patch state", lambda: "patched" if xpui_patch.is_patched() else "not patched")

    def audio():
        import comtypes

        comtypes.CoInitialize()
        from ad_watch import _spotify_sessions
        from pycaw.pycaw import AudioUtilities

        total = len(AudioUtilities.GetAllSessions())
        return "%d audio sessions visible (%d Spotify)" % (total, len(_spotify_sessions()))

    probe("Audio muting (pycaw)", audio)

    def window():
        from ad_watch import spotify_window_title

        # Deliberately not the title itself: this file gets attached to bug
        # reports, and the title is whatever the user is listening to.
        title = spotify_window_title()
        if title is None:
            return "no visible Spotify window"
        return "readable, %d chars, music-shaped: %s" % (
            len(title), " - " in title,
        )

    probe("Window title read", window)

    def seamless_imports():
        import ad_proxy  # noqa: F401
        import proxy_config  # noqa: F401

        return "proxy modules import"

    probe("Seamless mode modules", seamless_imports)

    def crypto():
        import proxy_ca

        cert, key = proxy_ca.load_ca()
        proxy_ca.make_leaf("gew4-spclient.spotify.com", cert, key)
        return "CA + leaf signing works (%d-bit), constrained: %s, key ACL: %s" % (
            key.key_size,
            proxy_ca.is_constrained(cert),
            "restricted" if proxy_ca.key_permissions_ok else "COULD NOT RESTRICT",
        )

    probe("Seamless mode crypto", crypto)

    lines.append("")
    lines.append("RESULT: %s" % ("all mechanisms available" if ok else "SOMETHING IS BROKEN"))
    report = "\n".join(lines)

    print(report)
    if getattr(sys, "frozen", False):
        # No console when frozen, so surface it somewhere visible.
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(os.path.join(DATA_DIR, "selftest.txt"), "w", encoding="utf-8") as handle:
                handle.write(redact(report) + "\n")
        except OSError:
            pass
        ctypes.windll.user32.MessageBoxW(0, report, "Spotify Skipper - self test", 0x40)
    return 0 if ok else 1


def run_cleanup():
    """Uninstall hook: undo everything this app ever changed."""
    try:
        stop_seamless()
    except Exception:
        pass

    # Spotify holds xpui.spa open, so without this the restore fails with a
    # permission error and the uninstaller - which ignores our exit code -
    # cheerfully reports success while leaving Spotify patched for good.
    try:
        if spotify_env.is_spotify_running():
            spotify_env.stop_spotify()
    except Exception as exc:
        log_debug("Cleanup could not stop Spotify: %s" % exc)

    ok, message = xpui_patch.restore()
    log_debug("Cleanup: %s (%s)" % (ok, message))

    leftovers = [
        CONFIG_PATH,
        # Taken the first time seamless mode stripped proxy keys out of
        # Spotify's prefs; nothing else ever removes it.
        os.path.join(os.environ.get("APPDATA", ""), "Spotify", "prefs.skipper-backup"),
    ]
    for path in leftovers:
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    return 0 if ok else 1


# Local\, not Global\. Creating a Global object needs SeCreateGlobalPrivilege,
# which a standard (non-administrator) account does not have - CreateMutexW
# would return NULL there and the guard would quietly stop guarding anything.
# This is a per-user tray app, so the per-session namespace is the right scope.
SINGLE_INSTANCE_MUTEX = "Local\\SpotifyAdsSkipper.SingleInstance"
_instance_handle = []


def claim_single_instance():
    """Take a named mutex. False when another copy already holds it.

    Two copies fight over everything that matters: both race for the proxy and
    PAC ports, and both run a mute watcher that undoes the other's work.
    """
    try:
        kernel32 = ctypes.windll.kernel32
        # Declared, because the default restype is c_int and would truncate the
        # 64-bit handle - leaving a valid mutex looking like a failure.
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p,
        ]
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
        last_error = kernel32.GetLastError()
        if not handle:
            return True  # Cannot tell; do not block the user over it.
        ERROR_ALREADY_EXISTS = 183
        if last_error == ERROR_ALREADY_EXISTS:
            return False
        # Held for the life of the process: the handle must stay open, or the
        # mutex is released and the guard stops guarding anything.
        _instance_handle.append(handle)
        return True
    except Exception:
        return True


def main():
    global tray_icon, watcher

    if "--cleanup" in sys.argv:
        sys.exit(run_cleanup())

    if "--selftest" in sys.argv:
        sys.exit(run_selftest())

    if not claim_single_instance():
        ctypes.windll.user32.MessageBoxW(
            0,
            "Spotify Ads Skipper is already running.\n\n"
            "Look for the cat icon in the notification area.",
            "Spotify Skipper", 0x40,
        )
        sys.exit(0)

    if not spotify_env.is_spotify_installed():
        ctypes.windll.user32.MessageBoxW(
            0,
            "Spotify was not found in %APPDATA%\\Spotify.\n\n"
            "Install the Spotify desktop app first, then run this again.",
            "Spotify Skipper", 0x10,
        )
        sys.exit(1)

    tray_icon = Icon("SpotifySkipper", create_image(), "Spotify Skipper", construct_menu())

    # The muting watcher is the fallback for when seamless mode is off; it
    # costs nothing while no ad is playing, and in seamless mode no ad ever
    # reaches playback for it to react to.
    watcher = AdWatcher(log=log_debug)
    watcher.start()

    @guarded("Startup")
    def startup():
        reconcile_routing()
        ok, message = sync_patch()
        if seamless_enabled():
            ok2, message2 = start_seamless()
            if ok2:
                # Spotify may have launched ahead of us and already given up on
                # the proxy; this puts it right.
                ensure_spotify_routed()
            else:
                log_debug("Seamless failed to start: %s" % message2)
                message = message2
        notify(message)
        # The menu was built before any of this ran, so its status line still
        # describes a not-yet-started app until it is rebuilt.
        try:
            tray_icon.update_menu()
        except Exception:
            pass

    threading.Thread(target=startup, daemon=True).start()
    start_status_refresher(tray_icon)
    tray_icon.run()


if __name__ == "__main__":
    # Windows logging off, a Task Manager kill and an OS shutdown all skip the
    # tray's own exit path, and each one used to leave the machine's proxy
    # routing pointing at a port that dies with this process.
    atexit.register(lambda: shutdown_quietly())

    try:
        main()
    except Exception as exc:
        log_debug("Fatal: %s" % exc)
        shutdown_quietly()
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(os.path.join(DATA_DIR, "crash_log.txt"), "w", encoding="utf-8") as handle:
                handle.write("Crash Error: %s\n" % redact(exc))
        except Exception:
            pass
        # Its own try: when the log cannot be written - a read-only install
        # directory, a full disk - the dialog is the only thing the user would
        # ever see, and sharing a try with the write meant they saw nothing.
        try:
            ctypes.windll.user32.MessageBoxW(
                0, "Application encountered an error: %s" % redact(exc),
                "Spotify Skipper Error", 0x10,
            )
        except Exception:
            pass
