"""Spotify Ads Skipper - tray application.

The point of this app is that audio ads never play. Not muted, not skipped
after the fact - never fetched, so the next track starts immediately.

Audio ads are requested by the client's native core from /ads/v3/ads and a
handful of sibling paths on the same host as everything else. ad_proxy answers
those paths with a 404 and forwards the rest untouched, so the client treats
the slot as empty and moves straight on. That is the whole product; there is no
second, weaker mode to fall back to.

Display ads (home banners, takeovers, video overlays) travel a different route
entirely - they ship inside the client's own UI bundle - so xpui_patch hides
them there. Two ad delivery paths, two mechanisms, both always on.

Reading request paths inside HTTPS means terminating TLS, which needs a
certificate authority in the user's trust store. That CA is generated per
installation and never shipped: a CA baked into the executable would put one
private key on every user's machine, letting anyone who downloaded the app
impersonate Spotify for all the others. Because the app cannot work without it,
consent is asked once on first run - and refusing simply exits.
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

tray_icon = None
proxy = None
pac_server = None

def _config_path():
    """Where the consent record lives - deliberately NOT the roaming profile.

    This file is how the app knows the user has agreed to a root CA. In
    %APPDATA% it roams: sign in to a second machine with a roaming or
    container profile and the consent arrives ahead of the user, so the notice
    is skipped and a CA is generated and trusted there without anyone being
    asked. A revocation roams too, which is the same problem pointing the other
    way. %LOCALAPPDATA% does not roam - it is where the CA key already lives,
    for the same reason.
    """
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or os.path.expanduser("~"))
    return os.path.join(base, "SpotifyAdsSkipper", "settings.json")


CONFIG_PATH = _config_path()

# Where releases up to 3.0 kept it. Read once, for migration, then removed.
LEGACY_CONFIG_PATH = (
    os.path.join(os.environ.get("APPDATA", ""), "SpotifyAdsSkipper", "settings.json")
    if os.environ.get("APPDATA") else ""
)

FIRST_RUN_NOTICE = (
    "Spotify Ads Skipper blocks Spotify's ad requests outright, so tracks run "
    "back-to-back with no ad and no silence.\n\n"
    "To read those requests it has to decrypt Spotify's HTTPS traffic, which "
    "means installing a local certificate authority into your user "
    "certificate store. This is how the app works - there is no version of it "
    "that skips this step.\n\n"
    "What that means:\n"
    "- The CA is generated on THIS machine and never leaves it.\n"
    "- It can only vouch for WEBSITE certificates, and only for spotify.com, "
    "scdn.co and spotifycdn.com. Windows and OpenSSL both reject anything it "
    "signs for another domain, for a bare IP address, or for any other "
    "purpose - e-mail signing and code signing included.\n"
    "- Traffic to those Spotify domains is decrypted on this machine while "
    "the app runs. That includes Spotify pages open in your browser, so your "
    "Spotify login travels through it too.\n"
    "- Everything else connects directly and is never touched.\n"
    "- The CA stays installed until you remove it from the tray menu or "
    "uninstall the app - not just while the app is running.\n\n"
    "A certificate authority is a sensitive thing to install. If you are not "
    "comfortable with that, cancel: nothing is installed and the app "
    "closes.\n\n"
    "Press OK to set it up and start blocking ads."
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

    # Control characters never survive into the log. Most messages here are
    # built from constants, but some carry a hostname or an exception string
    # that a local process chose - and a newline in one of those would let it
    # forge a timestamped line, while a NUL turns the whole file into
    # something grep calls binary. Both were observed. Escaping rather than
    # dropping keeps the evidence visible in a bug report.
    return "".join(
        ch if ch == "\t" or ch >= " " else "\\x%02x" % ord(ch)
        for ch in text
    )


# The proxy answers requests on a thread each, and they all log. Without this
# their writes interleave: observed in a real run as four entries whose tails
# landed on a line of their own ("config"), leaving the log to be read as if
# something had gone wrong when nothing had. It is the only diagnostic there
# is now that a failure means ads simply play, so it has to be trustworthy.
_log_lock = threading.Lock()


def log_debug(message):
    try:
        from datetime import datetime

        # Under the user's data directory, not next to the executable: a copy
        # installed somewhere read-only would otherwise log nowhere at all.
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, "debug_log.txt")
        line = "[%s] %s\n" % (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), redact(message),
        )
        with _log_lock:
            # Rotate rather than grow without bound: this runs for as long as
            # the machine is on, and a stale multi-megabyte log is useless for
            # support. Inside the lock so two threads cannot both rotate and
            # lose the file one of them was about to append to.
            try:
                if os.path.getsize(path) > LOG_MAX_BYTES:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception:
        pass


def load_config():
    for path in (CONFIG_PATH, LEGACY_CONFIG_PATH):
        if not path:
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                config = json.load(handle)
        except (OSError, ValueError):
            continue
        # A JSON document does not have to be an object. A bare list or string
        # would sail through json.load and then raise on the first .get(),
        # out of a function every caller treats as total.
        if isinstance(config, dict):
            return config
    return {}


def save_config(config):
    """Write the config. Returns whether it actually landed.

    The result matters for one caller: withdrawing consent. Swallowing a
    failure there means the app reports the CA removed while the file still
    says the user agreed, so the next launch silently installs a new one.
    """
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
    except OSError:
        return False

    # Once the non-roaming copy exists, drop the roaming one so it cannot
    # travel to another machine and answer the consent question there.
    if LEGACY_CONFIG_PATH and LEGACY_CONFIG_PATH != CONFIG_PATH:
        try:
            os.remove(LEGACY_CONFIG_PATH)
        except OSError:
            pass
    return True


def has_consented():
    """Whether the user has already agreed to the local CA.

    "seamless" is the key an earlier build used for the same agreement back
    when blocking was an optional mode, so an existing yes is honoured rather
    than asked for a second time. It is consulted only in the absence of an
    answer to the question actually being asked now - falling back to it
    whenever "consented" is falsy would let a stale legacy yes override a
    deliberate no.
    """
    config = load_config()
    if "consented" in config:
        return bool(config["consented"])
    return bool(config.get("seamless"))


def record_consent(given):
    """Record the answer. Returns whether it was persisted."""
    config = load_config()
    config["consented"] = bool(given)
    # Drop the superseded key so the two cannot drift apart later.
    config.pop("seamless", None)
    return save_config(config)


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


MB_OKCANCEL = 0x01
MB_YESNO = 0x04
MB_ICONWARNING = 0x30
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000
IDOK = 1
IDYES = 6

# SETFOREGROUND and TOPMOST are not cosmetic on either of these: launched from
# a tray app with no owner window, a dialog can otherwise come up behind
# everything and without focus, which looks exactly like one that ignores
# clicks.
_DIALOG_FLAGS = MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST


def confirm(text, title="Spotify Skipper"):
    """Ask a yes/no question."""
    return ctypes.windll.user32.MessageBoxW(
        0, text, title, MB_YESNO | _DIALOG_FLAGS
    ) == IDYES


def accepted(text, title="Spotify Skipper"):
    """Ask for OK/Cancel on something the user is about to have done to them.

    Separate from confirm() because the two questions are not the same shape.
    Yes/No suits a choice; OK/Cancel suits a gate, where Cancel means "stop,
    change nothing" rather than "no thanks, carry on without it".
    """
    return ctypes.windll.user32.MessageBoxW(
        0, text, title, MB_OKCANCEL | _DIALOG_FLAGS
    ) == IDOK


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


# -- audio ads: the proxy --------------------------------------------------

def start_blocking():
    """Bring up the proxy, PAC server and routing. Returns (ok, message).

    Every failure path tears down what it already built, CA trust included.
    Half-finished state here is the worst outcome available: a trusted root
    certificate installed for a proxy that never came up, with no obvious way
    for the user to get rid of it again.

    untrust_ca is deliberately conditional on trusted_now. A CA that was
    already in the store belongs to a working install whose proxy merely lost a
    port race this time; pulling it out would turn a retryable failure into a
    fresh certutil run - and another chance to fail - on the next start.
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

        # Wait for the bind before building the PAC. The port is allocated by
        # Windows now, and the PAC file has to name the real one - reading it
        # too early would publish 0.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if proxy.listening or proxy.bind_error:
                break
            time.sleep(0.05)

        if not proxy.listening:
            raise RuntimeError(
                "the ad proxy could not take a port (%s)"
                % (proxy.bind_error or "unknown")
            )

        pac_server = proxy_config.PacServer(proxy.port, log=log_debug)
        pac_server.start()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if pac_server.alive or pac_server.bind_error:
                break
            time.sleep(0.05)
        if not pac_server.alive:
            raise RuntimeError(
                "the routing server could not take port %d (%s)"
                % (pac_server.port, pac_server.bind_error or "unknown")
            )
        # Confirm we are the ones answering on that port before telling Windows
        # to fetch its proxy configuration from it.
        #
        # A failure here is logged, not fatal. It cannot actually catch a
        # squatter - the port was just bound with SO_EXCLUSIVEADDRUSE, so
        # anything else that had it would have made that bind fail - and the
        # check has a false-negative mode of its own. Treating it as fatal took
        # the whole start-up down and, through the except below, deleted the
        # root certificate installed seconds earlier, regenerating a fresh
        # 3072-bit CA on the next logon. Every logon.
        if not pac_server.serves_our_pac():
            log_debug(
                "PAC self-check did not match on port %d; continuing, since the "
                "exclusive bind already establishes ownership of the port."
                % pac_server.port
            )

        # Spotify's own prefs proxy keys would fight the PAC; clear them.
        proxy_config.clear_spotify_prefs()
        ok, message = proxy_config.enable(pac_server.url)
        if not ok:
            raise RuntimeError(message)
    except Exception as exc:
        log_debug("Ad blocking failed to start: %s" % exc)
        stop_blocking(untrust_ca=trusted_now)
        return False, "Ads are NOT being blocked: %s" % exc

    log_debug("Blocking ads (%s)" % pac_server.url)
    return True, "Blocking ads - they will not play at all."


def reconcile_routing():
    """Clear routing left behind by a previous run that ended badly.

    Windows shutting down, a kill from Task Manager or a crash all skip the
    teardown, so AutoConfigURL can outlive the app. A stale entry points every
    application that reads a PAC at a port nobody is listening on - and worse,
    at a port anything on the machine is then free to claim.

    Run unconditionally, before setting the routing up again. It looks like
    wasted work now that blocking is always on, but it is what keeps a PAC that
    belongs to someone else - a corporate one, a VPN client's - correctly
    parked and restored: disable() puts it back and forgets its own copy, then
    enable() saves it again from a clean slate. Skipping this leaves whatever a
    crashed run happened to record as "the previous PAC".
    """
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

    log_debug("Could not restart Spotify; its traffic is not routed yet.")
    return False


def stop_blocking(untrust_ca=True):
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

    log_debug("Blocking stopped")


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
    # Off the callback thread: while a tray menu is up Windows holds a mouse
    # capture for it, so anything slow - or any dialog - blocks with the menu
    # still owning the mouse. Stopping and relaunching Spotify takes seconds.
    threading.Thread(target=_restore_worker, daemon=True).start()


def on_quit(icon, _item):
    shutdown()
    icon.stop()


_shut_down = threading.Event()


def shutdown():
    """Leave no routing behind - but only routing this process put there.

    Registered with atexit, so it runs on every exit path there is, including
    `--selftest`. That flag starts nothing, and tearing down regardless meant a
    diagnostic run reached into the live instance's registry and cleared its
    AutoConfigURL: ads came back silently, and the app that was actually
    running had no idea. Measured, not theorised - a planted AutoConfigURL was
    gone after one --selftest.

    proxy and pac_server are the honest record of what this process owns. Both
    are None here when blocking never started, and start_blocking() has already
    cleaned up after itself when it started and then failed.

    The CA deliberately stays either way: it is what the next launch reuses,
    and pulling a root certificate in and out of the store on every logon is
    both slow and a good way to end up with it half-removed. Uninstalling, or
    "Remove local certificate", is what gets rid of it.
    """
    if _shut_down.is_set():
        return
    _shut_down.set()
    if proxy is None and pac_server is None:
        return
    stop_blocking(untrust_ca=False)


def shutdown_quietly():
    """Teardown for paths where nothing is left to report a failure to."""
    try:
        shutdown()
    except Exception:
        pass


def start_shutdown_watcher():
    """Clear the routing when Windows is logging off or shutting down.

    atexit does not run when the session manager ends the process, and pystray
    installs no WM_ENDSESSION handler - so a normal shutdown left AutoConfigURL
    in the registry pointing at a port nobody would hold until the next launch.
    That is the ordinary post-reboot state, not an exotic crash, and the port
    is claimable by anything on the machine while it lasts.

    SetConsoleCtrlHandler is what is available to a windowed process without a
    message loop of its own: Windows delivers CTRL_LOGOFF_EVENT and
    CTRL_SHUTDOWN_EVENT through it. A best-effort belt to reconcile_routing()'s
    braces at the next start, not a replacement for it.
    """
    CTRL_CLOSE_EVENT = 2
    CTRL_LOGOFF_EVENT = 5
    CTRL_SHUTDOWN_EVENT = 6
    HANDLER = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def on_event(event):
        if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            shutdown_quietly()
        return 0  # Fall through to the default handler either way.

    handler = HANDLER(on_event)
    # Held on the module, or it is garbage collected and Windows calls into
    # freed memory the next time the machine shuts down.
    _ctrl_handlers.append(handler)
    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
    except Exception as exc:
        log_debug("Could not install the shutdown handler: %s" % exc)


_ctrl_handlers = []


def start_status_refresher(icon):
    """Keep the tray menu's status line current.

    pystray's Windows backend builds the popup menu once and caches the handle,
    so a callable item's text is only re-evaluated when update_menu() is called.
    Without this the status is frozen at whatever was true the instant the icon
    was created - which is before the startup thread has brought the proxy up,
    and therefore reads "NOT BLOCKING" forever even though everything works.
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
    """The one line that says whether the app is doing its job.

    It carries more weight than it used to. There is no muting fallback any
    more, so a failure here means ads play in full - and the parts are reported
    separately because they fail separately. A dead PAC server in particular
    leaves Windows unable to fetch the routing rules, so Spotify connects
    directly and the proxy sits idle, which would otherwise read as a healthy
    "0 dropped" and hide the failure completely.
    """
    if proxy is None:
        return "Status: NOT BLOCKING - proxy not running"
    if not proxy.listening:
        # The thread can be alive while owning no port at all.
        return "Status: NOT BLOCKING - port %d is taken" % proxy.port
    if pac_server is None or not pac_server.alive:
        return "Status: NOT BLOCKING - routing server down, restart the app"
    if not proxy.blocked and not spotify_env.is_spotify_running():
        # A zero here means "nothing has needed blocking yet", which reads
        # exactly like a failure. Say why it is zero instead.
        return "Status: ready - Spotify is not running"
    return "Status: blocking (%d ad requests dropped)" % (proxy.blocked,)


def ca_is_installed():
    try:
        import proxy_ca

        return proxy_ca.is_trusted()
    except Exception:
        return False


REMOVE_CA_WARNING = (
    "This removes the local certificate authority from your certificate "
    "store.\n\n"
    "Blocking ads depends on it, so the app will close. Nothing else is "
    "undone: Spotify's interface stays patched, and starting the app again "
    "asks for permission and sets up a fresh certificate.\n\n"
    "Remove it and close?"
)


@guarded("Remove local CA")
def _remove_ca_worker(icon):
    import proxy_ca

    if not confirm(REMOVE_CA_WARNING):
        return

    stop_blocking(untrust_ca=False)
    # Consent is withdrawn along with the certificate, so the next launch asks
    # again rather than quietly reinstalling what was just deliberately removed.
    if not record_consent(False):
        log_debug("Could not persist consent withdrawal; refusing to remove the CA.")
        notify(
            "Could not save the setting, so the certificate was left in place. "
            "Removing it now would mean the app silently reinstalls one next "
            "time you start it."
        )
        return

    ok, message = proxy_ca.untrust()
    log_debug("Remove CA: %s (%s)" % (ok, message))
    if not ok:
        notify(message)
        return

    shutdown()
    icon.stop()


def on_remove_ca(icon, _item):
    threading.Thread(target=_remove_ca_worker, args=(icon,), daemon=True).start()


def construct_menu():
    return Menu(
        item(status_text, None, enabled=False),
        Menu.SEPARATOR,
        # Shown whenever a certificate is installed, including after a failed
        # start-up that left one behind. Without it the only way to get rid of
        # one is certmgr.msc, which is not a thing to ask of anybody.
        item("Remove local certificate", on_remove_ca, visible=lambda _i: ca_is_installed()),
        item("Restore original Spotify UI", on_restore),
        item("Close", on_quit),
    )


def run_selftest():
    """Report whether each mechanism can actually run here.

    Frozen builds are the reason this exists: a dependency that imports fine
    from source can still be missing from the bundle, and the proxy would then
    fail at the point where it is the only thing standing between the user and
    a full-length ad. Users can run it to diagnose too.
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

    def proxy_imports():
        import ad_proxy  # noqa: F401
        import proxy_config  # noqa: F401

        return "proxy modules import"

    probe("Proxy modules", proxy_imports)

    def crypto():
        import proxy_ca

        # Never load_ca() here. That GENERATES a CA when none exists, so a
        # diagnostic run would write RSA key material onto a machine whose
        # user has not been asked yet - and --selftest is reachable before the
        # consent gate by design, so it can be run at any time.
        if not proxy_ca.ca_exists():
            return "no CA yet (nothing generated - run the app to set one up)"

        cert, key = proxy_ca.load_ca()
        proxy_ca.make_leaf("gew4-spclient.spotify.com", cert, key)
        return "CA + leaf signing works (%d-bit), constrained: %s, key ACL: %s, key sealed: %s" % (
            key.key_size,
            proxy_ca.is_constrained(cert),
            "restricted" if proxy_ca.key_permissions_ok else "COULD NOT RESTRICT",
            "yes" if proxy_ca.key_is_sealed else "NO - DPAPI UNAVAILABLE, KEY IS PLAINTEXT",
        )

    probe("CA and leaf signing", crypto)

    def upstream_anchors():
        import ad_proxy

        # The check with no symptom. If the bundle lost certifi, or the context
        # ever fell back to the Windows store, our own CA becomes a valid
        # anchor for the connection that exists to detect forged Spotify
        # certificates - and nothing would look wrong until someone used it.
        count, ours = ad_proxy.upstream_anchor_count()
        if ours:
            raise RuntimeError(
                "our own CA is trusted for upstream connections (%d anchors)" % count
            )
        return "%d anchors, ours correctly absent" % count

    probe("Upstream trust anchors", upstream_anchors)

    def routing():
        import proxy_ca
        import proxy_config

        return "CA trusted: %s, routing active: %s" % (
            proxy_ca.is_trusted(), proxy_config.is_enabled(),
        )

    probe("Current install state", routing)

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
    ca_ok, ca_message = True, ""
    try:
        # Not stop_blocking(): its untrust() result is discarded, and this is
        # the one place where a failed removal has to be surfaced. Inno's
        # [UninstallRun] cannot read an exit code, and it deletes the data
        # directory - including the certificate file that identifies our entry
        # in the store - immediately afterwards. A silent failure here is a
        # trusted root left behind permanently with nothing left to find it by.
        stop_blocking(untrust_ca=False)
    except Exception as exc:
        log_debug("Cleanup could not stop blocking: %s" % exc)

    try:
        import proxy_ca

        ca_ok, ca_message = proxy_ca.untrust()
        log_debug("Cleanup CA removal: %s (%s)" % (ca_ok, ca_message))
    except Exception as exc:
        ca_ok, ca_message = False, str(exc)
        log_debug("Cleanup CA removal raised: %s" % exc)

    if not ca_ok:
        try:
            ctypes.windll.user32.MessageBoxW(
                0, ca_message, "Spotify Skipper - certificate not removed",
                0x10 | MB_SETFOREGROUND | MB_TOPMOST,
            )
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
        LEGACY_CONFIG_PATH,
        # Taken the first time the proxy stripped proxy keys out of Spotify's
        # prefs; nothing else ever removes it.
        os.path.join(os.environ.get("APPDATA", ""), "Spotify", "prefs.skipper-backup"),
    ]
    for path in leftovers:
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    return 0 if (ok and ca_ok) else 1


# Local\, not Global\. Creating a Global object needs SeCreateGlobalPrivilege,
# which a standard (non-administrator) account does not have - CreateMutexW
# would return NULL there and the guard would quietly stop guarding anything.
# This is a per-user tray app, so the per-session namespace is the right scope.
SINGLE_INSTANCE_MUTEX = "Local\\SpotifyAdsSkipper.SingleInstance"
_instance_handle = []


def claim_single_instance():
    """Take a named mutex. False when another copy already holds it.

    Two copies fight over everything that matters: they race for the proxy and
    PAC ports, and whichever loses tears down the routing the winner just set
    up on its way out.
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
    global tray_icon

    # The mutex comes first, ahead of the flag handlers. --cleanup used to run
    # before it and called stop_blocking() directly, so running the exe with
    # that flag against a live instance cleared the routing, deleted the CA and
    # its key, and unpatched the UI - while the tray survived with
    # proxy.listening still True and went on reporting "Status: blocking".
    single = claim_single_instance()

    if "--cleanup" in sys.argv:
        if not single:
            ctypes.windll.user32.MessageBoxW(
                0,
                "Spotify Ads Skipper is still running.\n\n"
                "Close it from the tray first, then try again - otherwise the "
                "running copy would keep working with its certificate already "
                "deleted.",
                "Spotify Skipper", 0x30,
            )
            sys.exit(1)
        sys.exit(run_cleanup())

    if "--selftest" in sys.argv:
        sys.exit(run_selftest())

    if not single:
        ctypes.windll.user32.MessageBoxW(
            0,
            "Spotify Ads Skipper is already running.\n\n"
            "Look for the cat icon in the notification area.",
            "Spotify Skipper", 0x40,
        )
        sys.exit(0)

    # Ahead of the Spotify-missing exit below. Routing outlives the process on
    # any hard stop, and if Spotify is later uninstalled or moved this function
    # would never run again on any subsequent launch - stranding AutoConfigURL
    # on a dead, and therefore claimable, port indefinitely.
    reconcile_routing()

    if not spotify_env.is_spotify_installed():
        ctypes.windll.user32.MessageBoxW(
            0,
            "Spotify was not found in %APPDATA%\\Spotify.\n\n"
            "Install the Spotify desktop app first, then run this again.",
            "Spotify Skipper", 0x10,
        )
        sys.exit(1)

    # Asked before anything is created or installed, and only once. Declining
    # exits with the machine exactly as it was found - there is no reduced mode
    # to fall back to, so carrying on regardless would mean an app that sits in
    # the tray doing nothing.
    if not has_consented():
        if not accepted(FIRST_RUN_NOTICE):
            log_debug("First-run consent declined; exiting without changes.")
            sys.exit(0)
        record_consent(True)

    tray_icon = Icon("SpotifySkipper", create_image(), "Spotify Skipper", construct_menu())

    @guarded("Startup")
    def startup():
        # Audio first. These two are independent mechanisms, and the UI patch
        # is the fragile one - it opens a zip Spotify wrote. An entry with an
        # unsupported compression method raises RuntimeError out of
        # sync_patch(), and with the old ordering that exception unwound
        # through @guarded and start_blocking() never ran at all: a cosmetic
        # banner failure took down the audio-ad blocking that is the whole
        # point of the app. Its own try/except below is belt and braces.
        ok, message = start_blocking()

        try:
            patched, patch_message = sync_patch()
        except Exception as exc:
            patched, patch_message = False, str(exc)
        if not patched:
            log_debug("Display ads not blocked: %s" % patch_message)

        if ok:
            # Spotify may have launched ahead of us and already given up on the
            # proxy; this puts it right.
            ensure_spotify_routed()
            if not patched:
                # Mentioned only once the audio side is known to be working, so
                # the two halves of the message cannot contradict each other.
                message = "%s Display ads may still show: %s" % (message, patch_message)
        else:
            # The one failure worth interrupting for. Everything else degrades
            # into "some ads got through"; this is the app not working at all,
            # and a balloon that vanishes after five seconds is not enough to
            # say so.
            log_debug("Blocking failed to start: %s" % message)
            ctypes.windll.user32.MessageBoxW(
                0,
                "%s\n\nAds will play normally until this is fixed. The most "
                "common cause is another program holding the port, which a "
                "restart usually clears." % message,
                "Spotify Skipper", 0x10 | MB_SETFOREGROUND | MB_TOPMOST,
            )
        notify(message)
        # The menu was built before any of this ran, so its status line still
        # describes a not-yet-started app until it is rebuilt.
        try:
            tray_icon.update_menu()
        except Exception:
            pass

    threading.Thread(target=startup, daemon=True).start()
    start_status_refresher(tray_icon)
    start_shutdown_watcher()
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
