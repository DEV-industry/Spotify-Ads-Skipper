"""Locating the Spotify installation and inspecting its runtime state.

Everything here is read-only. Nothing in this module needs administrator
rights - the Spotify client installs into %APPDATA%, which the user owns.
"""

import os
import subprocess


APPDATA = os.environ.get("APPDATA", "")
SPOTIFY_DIR = os.path.join(APPDATA, "Spotify")
APPS_DIR = os.path.join(SPOTIFY_DIR, "Apps")

XPUI_PATH = os.path.join(APPS_DIR, "xpui.spa")
XPUI_BACKUP = os.path.join(APPS_DIR, "xpui.spa.orig")

SPOTIFY_EXE = os.path.join(SPOTIFY_DIR, "Spotify.exe")

# Written next to the backup so we can tell whether Spotify has updated
# itself out from under our patch.
STAMP_PATH = os.path.join(APPS_DIR, "xpui.spa.skipper-stamp")


def is_spotify_installed():
    return os.path.isfile(XPUI_PATH) and os.path.isfile(SPOTIFY_EXE)


def spotify_version():
    """Read the client version off Spotify.exe, or None if unavailable."""
    if not os.path.isfile(SPOTIFY_EXE):
        return None
    try:
        import win32api

        info = win32api.GetFileVersionInfo(SPOTIFY_EXE, "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:
        return None


def spotify_pids():
    """PIDs of every running Spotify process (the client is multi-process)."""
    try:
        import psutil
    except ImportError:
        return []

    pids = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info["name"] or "").lower() == "spotify.exe":
                pids.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def is_spotify_running():
    return len(spotify_pids()) > 0


WM_CLOSE = 0x0010


def _ask_windows_to_close(pids):
    """Post WM_CLOSE to Spotify's windows. Returns True if anything was sent."""
    try:
        import win32gui
        import win32process
    except ImportError:
        return False

    sent = []

    def callback(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if pid in pids:
            try:
                win32gui.PostMessage(hwnd, WM_CLOSE, 0, 0)
                sent.append(hwnd)
            except Exception:
                pass
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        return False
    return bool(sent)


def stop_spotify(timeout=10, grace=6):
    """Close Spotify so its files can be rewritten.

    Returns True once no Spotify process remains.

    Asks the window to close first and gives it a few seconds. This matters
    more than it looks: psutil's terminate() is an alias for kill() on Windows
    - a bare TerminateProcess - so the previous "polite terminate" was nothing
    of the sort, and Spotify never got to flush its prefs, its offline index or
    the playback position. Only stragglers are killed, including the case where
    the user has Spotify set to close to the tray and it therefore ignores the
    request entirely.
    """
    try:
        import psutil
    except ImportError:
        return False

    def live_procs():
        found = []
        for pid in spotify_pids():
            try:
                found.append(psutil.Process(pid))
            except psutil.NoSuchProcess:
                continue
        return found

    procs = live_procs()
    if not procs:
        return True

    if _ask_windows_to_close({p.pid for p in procs}):
        gone, procs = psutil.wait_procs(procs, timeout=grace)
        if not procs:
            return not is_spotify_running()

    for proc in procs:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    gone, alive = psutil.wait_procs(procs, timeout=timeout)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    psutil.wait_procs(alive, timeout=3)

    return not is_spotify_running()


def start_spotify():
    """Relaunch the client after patching. Never elevates."""
    if not os.path.isfile(SPOTIFY_EXE):
        return False
    try:
        subprocess.Popen(
            [SPOTIFY_EXE],
            creationflags=0x00000008,  # DETACHED_PROCESS
            close_fds=True,
        )
        return True
    except Exception:
        return False
