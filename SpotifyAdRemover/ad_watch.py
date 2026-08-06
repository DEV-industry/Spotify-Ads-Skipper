"""Detecting Spotify audio ads and silencing them.

Audio ads cannot be removed from the client. The server injects them into the
playback queue as ordinary tracks carrying their own `manifest_urls_audio_ad`,
and the client simply plays whatever it is handed - there is no client-side
gate to switch off. What we can do is notice one starting and mute Spotify (and
only Spotify) for its duration.

Detection leans on two signals, neither of which depends on Spotify's UI
internals, so both survive client updates:

  1. Spotify owns an audio session in the Active state - audio is really
     being rendered, as opposed to paused or stopped.
  2. The window title is not in "Artist - Track" form. Spotify titles the
     window after the current track while music plays; during an ad it falls
     back to a bare label such as "Advertisement" or "Spotify Free".

Keying on the separator rather than on a list of ad captions keeps this working
in every UI language, since the separator is present for music in all of them.
"""

import threading
import time

MUSIC_SEPARATOR = " - "

POLL_SECONDS = 0.25

# How long a title must stay ad-shaped before we mute. Spotify blanks the title
# for a moment during ordinary track changes, and muting on that flicker would
# clip the first instant of the next song.
CONFIRM_SECONDS = 0.4

AUDIO_SESSION_ACTIVE = 1


def spotify_window_title():
    """Title of Spotify's main window, or None when it has none."""
    try:
        import win32gui
        import win32process
    except ImportError:
        return None

    from spotify_env import spotify_pids

    pids = set(spotify_pids())
    if not pids:
        return None

    found = []

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if pid not in pids:
            return True
        title = win32gui.GetWindowText(hwnd)
        if title:
            found.append(title)
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        return None

    return found[0] if found else None


def _spotify_sessions():
    """Live pycaw audio sessions belonging to Spotify.

    Re-enumerated on every call: Spotify creates a session when playback
    starts and lets it expire when it stops, so a cached handle goes stale.
    """
    try:
        from pycaw.pycaw import AudioUtilities
    except ImportError:
        return []

    sessions = []
    try:
        for session in AudioUtilities.GetAllSessions():
            process = session.Process
            if process is None:
                continue
            try:
                name = process.name()
            except Exception:
                continue
            if name.lower() == "spotify.exe":
                sessions.append(session)
    except Exception:
        return []
    return sessions


def _is_rendering(sessions):
    for session in sessions:
        try:
            if session.State == AUDIO_SESSION_ACTIVE:
                return True
        except Exception:
            continue
    return False


def _set_muted(sessions, muted):
    """Mute or unmute every Spotify session. Returns True if any call stuck."""
    changed = False
    for session in sessions:
        try:
            volume = session.SimpleAudioVolume
            volume.SetMute(1 if muted else 0, None)
            changed = True
        except Exception:
            continue
    return changed


def _is_muted(sessions):
    """Current mute state, or None when no session will say."""
    for session in sessions:
        try:
            return bool(session.SimpleAudioVolume.GetMute())
        except Exception:
            continue
    return None


def looks_like_ad(title):
    """True when the window title is not in music's "Artist - Track" shape.

    An unreadable title is deliberately not an ad. Spotify keeps playing with
    its window hidden when it is closed to the tray, and there is no title to
    read then - so treating "cannot tell" as "ad" mutes real music and, since
    the title never comes back while it stays hidden, never unmutes it.
    """
    if title is None:
        return False
    return MUSIC_SEPARATOR not in title


class AdWatcher(threading.Thread):
    """Background poller that mutes Spotify while an audio ad plays.

    on_state_change, when supplied, is called with True as an ad starts and
    False when it ends. It runs on this thread, so it must not block.
    """

    daemon = True

    def __init__(self, on_state_change=None, log=None):
        super().__init__(name="AdWatcher")
        self._stop_event = threading.Event()
        self._on_state_change = on_state_change
        self._log = log or (lambda _msg: None)

        self.ad_playing = False
        self.ads_muted = 0
        self._candidate_since = None
        self._muted_by_us = False

    def stop(self):
        """Signal the loop to exit; unmuting happens in run()'s cleanup."""
        self._stop_event.set()

    def _enter_ad(self, sessions):
        # Only claim the mute if we are the ones silencing it. Someone who
        # muted Spotify themselves should still have it muted when the ad ends
        # - the old code unmuted them and let the next track play out loud.
        #
        # `is not True` rather than `is False`: when the state cannot be read
        # at all we claim it anyway. Getting that wrong in this direction costs
        # the user an unmute they did not ask for; getting it wrong the other
        # way leaves their music silent with nothing left to turn it back on.
        was_muted = _is_muted(sessions)
        if _set_muted(sessions, True) and was_muted is not True:
            self._muted_by_us = True
        self.ad_playing = True
        self.ads_muted += 1
        self._log("Ad detected - Spotify muted.")
        if self._on_state_change:
            try:
                self._on_state_change(True)
            except Exception:
                pass

    def _leave_ad(self, sessions):
        if self._muted_by_us:
            _set_muted(sessions, False)
            self._muted_by_us = False
        self.ad_playing = False
        self._log("Ad over - Spotify unmuted.")
        if self._on_state_change:
            try:
                self._on_state_change(False)
            except Exception:
                pass

    def run(self):
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            pass

        try:
            self._loop()
        finally:
            # Never leave Spotify muted because we exited.
            if self._muted_by_us:
                _set_muted(_spotify_sessions(), False)
                self._muted_by_us = False
            try:
                import comtypes

                comtypes.CoUninitialize()
            except Exception:
                pass

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                self._log("Watcher tick failed: %s" % exc)
            self._stop_event.wait(POLL_SECONDS)

    def _tick(self):
        sessions = _spotify_sessions()

        # Nothing rendering: Spotify is paused, stopped or closed. Undo any
        # mute we own so the user is never left with silent music.
        if not sessions or not _is_rendering(sessions):
            self._candidate_since = None
            if self.ad_playing:
                self._leave_ad(sessions)
            return

        title = spotify_window_title()
        if title is None:
            # No readable title - closed to the tray, or the window helpers are
            # missing from this build. Either way we cannot tell an ad from a
            # song, so leave the audio alone and give back any mute we own.
            self._candidate_since = None
            if self.ad_playing:
                self._leave_ad(sessions)
            return

        if looks_like_ad(title):
            if self.ad_playing:
                return
            now = time.monotonic()
            if self._candidate_since is None:
                self._candidate_since = now
            elif now - self._candidate_since >= CONFIRM_SECONDS:
                self._enter_ad(sessions)
            return

        self._candidate_since = None
        if self.ad_playing:
            self._leave_ad(sessions)
