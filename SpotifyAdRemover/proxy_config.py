"""Pointing Windows at the local ad proxy - for Spotify only.

A blanket system proxy would push every application's traffic through this
process, so a crash here would take the machine's connectivity with it. A PAC
file instead routes only Spotify hosts to the proxy and returns DIRECT for
everything else, keeping the blast radius to Spotify even in the worst case.
Other applications do read the same PAC, but DIRECT means their traffic never
touches us.

The PAC has to be served over HTTP. Measured against Spotify 1.2.94: a
file:/// AutoConfigURL is ignored outright - the client keeps connecting
directly - while an http://127.0.0.1 one is fetched and honoured. So a tiny
loopback HTTP server ships alongside the proxy purely to hand out this file.
"""

import http.client
import os
import secrets
import socket
import threading
import winreg
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# Our own corner of the registry, used to park a PAC URL that was already
# configured when the proxy came up so disable() can put it back.
OWN_KEY = r"Software\SpotifyAdsSkipper"
SAVED_VALUE = "PreviousAutoConfigURL"

PAC_HOST = "127.0.0.1"

# 0 means "let Windows pick". A fixed, published port is one an attacker knows
# in advance, and the value of knowing it is not theoretical: AutoConfigURL
# outlives the process on every OS shutdown (atexit does not run under
# TerminateProcess, and pystray installs no WM_ENDSESSION handler), so the
# registry can point at a port nobody holds. Loopback on Windows is a single
# machine-wide namespace with no session isolation, so a second standard user
# can then bind it and serve a PAC of their choosing to this user's
# applications. An unpredictable port does not close that window, but it stops
# it being campable in advance.
DEFAULT_PAC_PORT = 0

# The stable half of the path. is_enabled() and disable() both recognise our
# own AutoConfigURL by finding this substring, so it must not be randomised or
# the app can no longer identify - or clean up - its own routing.
PAC_FILENAME = "spotify-ads.pac"

# Connections handled at once. Windows fetches this file occasionally, per
# application, so a handful is generous - and the port is reachable by anything
# on the machine, which is the reason there is a ceiling at all.
MAX_PAC_CONNECTIONS = 16

# Kept in step with ad_proxy.SPOTIFY_SUFFIXES.
#
# The "; DIRECT" fallback matters: if this app is killed rather than closed, the
# PAC entry outlives it, and without a fallback Spotify would keep dialling a
# dead port and lose connectivity entirely. With it, an unreachable proxy just
# means the client connects straight out - ads come back, nothing breaks.
PAC_TEMPLATE = """function FindProxyForURL(url, host) {
    host = host.toLowerCase();
    if (dnsDomainIs(host, ".spotify.com") ||
        dnsDomainIs(host, ".scdn.co") ||
        dnsDomainIs(host, ".spotifycdn.com")) {
        return "PROXY 127.0.0.1:%(port)d; DIRECT";
    }
    return "DIRECT";
}
"""


def build_pac(proxy_port):
    return (PAC_TEMPLATE % {"port": proxy_port}).encode("utf-8")


def _refresh():
    """Tell WinINET the proxy configuration changed."""
    try:
        import ctypes

        wininet = ctypes.windll.Wininet
        wininet.InternetSetOptionW(0, 39, 0, 0)  # SETTINGS_CHANGED
        wininet.InternetSetOptionW(0, 37, 0, 0)  # REFRESH
    except Exception:
        pass


class PacServer(threading.Thread):
    """Loopback HTTP server that serves the PAC file and nothing else."""

    daemon = True

    def __init__(self, proxy_port, port=DEFAULT_PAC_PORT, log=None):
        super().__init__(name="PacServer")
        self.port = port
        self._body = build_pac(proxy_port)
        self._log = log or (lambda _m: None)
        self._httpd = None
        self.bind_error = None
        # Unguessable suffix on the path. Windows fetches whatever URL it is
        # given, so this does not authenticate anything to Windows - what it
        # does is stop a web page, or another local process, from requesting a
        # URL it can predict and learning that this app is installed and which
        # port the proxy is on.
        self._token = secrets.token_urlsafe(12)
        # Nothing legitimate needs more than a handful: Windows fetches this
        # file occasionally, per application.
        self._slots = threading.Semaphore(MAX_PAC_CONNECTIONS)

    @property
    def path(self):
        return "/%s-%s" % (PAC_FILENAME, self._token)

    @property
    def url(self):
        return "http://%s:%d%s" % (PAC_HOST, self.port, self.path)

    @property
    def alive(self):
        """Whether the server is still accepting - the mode silently stops
        working if it is not, so callers surface this."""
        return self.is_alive() and self._httpd is not None and self.bind_error is None

    def serves_our_pac(self, timeout=3):
        """Fetch our own PAC and check we are the ones answering.

        Whoever holds this port decides the proxy configuration for every
        application on the machine that reads a PAC, so it is worth one request
        to confirm the answer is ours before pointing Windows at it.
        """
        # http.client, not urllib.request. The default opener honours this
        # user's own HKCU ProxyEnable/ProxyServer and http_proxy, and CPython's
        # proxy_bypass_registry treats "<local>" as covering only hosts with no
        # dot - so 127.0.0.1 is NOT bypassed. Reproduced against a healthy
        # PacServer: the direct GET returned a byte-identical body while this
        # check returned False, which the caller reads as "another program is
        # answering on that port" and responds to by tearing down and deleting
        # the root CA it installed seconds earlier. Every logon.
        try:
            conn = http.client.HTTPConnection(PAC_HOST, self.port, timeout=timeout)
            try:
                conn.request("GET", self.path, headers={
                    "Host": "%s:%d" % (PAC_HOST, self.port),
                })
                response = conn.getresponse()
                return response.status == 200 and response.read() == self._body
            finally:
                conn.close()
        except Exception:
            return False


    def run(self):
        body = self._body
        logger = self._log
        expected_path = self.path
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                # Answer only our own path, and only when addressed by
                # loopback. A page in a browser cannot read this response, but
                # a DNS-rebinding one can, and it would learn that the app is
                # installed and which port the proxy sits on.
                #
                # expected_hosts is computed per request rather than captured:
                # the port is not known until after the bind, because it is
                # allocated by Windows.
                expected_hosts = {"%s:%d" % (PAC_HOST, server.port), PAC_HOST}
                host = (self.headers.get("Host") or "").strip().lower()
                if self.path != expected_path or host not in expected_hosts:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ns-proxy-autoconfig")
                self.send_header("Content-Length", str(len(body)))
                # A PAC file is syntactically valid JavaScript, and the Host
                # check does not stop <script src="http://127.0.0.1:PORT/...">,
                # which sends exactly the Host header we accept. Without this
                # header a page that guessed the URL could load the body as a
                # script and call FindProxyForURL cross-origin.
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass  # Silence the default stderr logging.

        slots = self._slots

        class Server(ThreadingHTTPServer):
            # Threaded so one stalled fetch cannot stop the rest being served.
            daemon_threads = True
            request_queue_size = 16

            def process_request(self, request, client_address):
                # A real ceiling, not just a listen backlog. request_queue_size
                # bounds connections WAITING to be accepted; on its own this
                # server still spawned a thread for every one it did accept, so
                # any local process could exhaust the process's thread budget -
                # which starves the proxy's accept loop and the tray with it.
                # The proxy has had a semaphore all along; this had nothing.
                if not slots.acquire(blocking=False):
                    logger("PAC server at capacity, dropping a connection")
                    # Closed directly, NOT through shutdown_request: that path
                    # ends in close_request, and releasing a slot that was
                    # never acquired would raise the ceiling every time it is
                    # hit - turning the limit into its own bypass.
                    try:
                        request.close()
                    except Exception:
                        pass
                    return
                try:
                    ThreadingHTTPServer.process_request(self, request, client_address)
                except BaseException:
                    # The thread never started, so nothing else will release.
                    slots.release()
                    raise

            def process_request_thread(self, request, client_address):
                try:
                    ThreadingHTTPServer.process_request_thread(
                        self, request, client_address)
                finally:
                    slots.release()
            # Emphatically not allow_reuse_address. On Windows that maps to
            # SO_REUSEADDR, which - unlike on POSIX - lets another process bind
            # this live port and take delivery of the requests. Whoever wins
            # gets to hand Windows a PAC file of their choosing, for every
            # application on the machine that reads one.
            allow_reuse_address = False

            def server_bind(self):
                try:
                    self.socket.setsockopt(socket.SOL_SOCKET,
                                           socket.SO_EXCLUSIVEADDRUSE, 1)
                except (AttributeError, OSError):
                    pass
                ThreadingHTTPServer.server_bind(self)

            def handle_error(self, request, client_address):
                # The stdlib default prints a traceback to sys.stderr. Under a
                # windowed PyInstaller build stderr is None, so that print
                # raises and kills this thread - leaving AutoConfigURL pointing
                # at a dead port, which looks exactly like the proxy silently
                # doing nothing. Swallow it instead; a dropped PAC fetch is
                # never worth taking the server down for.
                logger("PAC request from %s failed (ignored)" % (client_address,))

        try:
            httpd = Server((PAC_HOST, self.port), Handler)
        except OSError as exc:
            self.bind_error = str(exc)
            logger("PAC server could not bind port %d: %s" % (self.port, exc))
            return

        # Read back what was actually allocated. With DEFAULT_PAC_PORT = 0 the
        # port is not known until the bind has happened, and `url` - which is
        # what gets written into the registry - is built from it. Set before
        # _httpd, so `alive` cannot go True while `port` still reads 0.
        self.port = httpd.server_address[1]
        self._httpd = httpd

        logger("PAC server on %s" % self.url)
        try:
            self._httpd.serve_forever(poll_interval=0.5)
        except Exception as exc:
            logger("PAC server stopped unexpectedly: %s" % exc)
        finally:
            self._httpd = None

    def stop(self):
        httpd = self._httpd
        if httpd:
            try:
                httpd.shutdown()
            except Exception:
                pass
            # server_close as well as shutdown: shutdown only stops the serve
            # loop, and with SO_EXCLUSIVEADDRUSE a socket left open by a
            # garbage-collection delay makes the next bind fail outright, so
            # toggling the mode off and on again would not come back up.
            try:
                httpd.server_close()
            except Exception:
                pass


def _read_autoconfig():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS) as key:
            value, _ = winreg.QueryValueEx(key, "AutoConfigURL")
        return value
    except OSError:
        return None


def _read_saved():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, OWN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, SAVED_VALUE)
        return value
    except OSError:
        return None


def _save_previous(value):
    """Remember a PAC URL that was already configured before we arrived."""
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, OWN_KEY) as key:
            winreg.SetValueEx(key, SAVED_VALUE, 0, winreg.REG_SZ, value)
    except OSError:
        pass


def _forget_previous():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, OWN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, SAVED_VALUE)
    except OSError:
        pass


def is_enabled(pac_url=None):
    """True when AutoConfigURL points at our PAC."""
    value = _read_autoconfig()
    if value is None:
        return False
    return PAC_FILENAME in value if pac_url is None else value == pac_url


def enable(pac_url):
    """Route Spotify hosts through the local proxy. Returns (ok, message)."""
    # A machine may already be using a PAC - a corporate one, a VPN client's,
    # another tool's. Overwriting it and later deleting the value outright,
    # which is what this used to do, silently destroys that configuration.
    existing = _read_autoconfig()
    if existing and PAC_FILENAME not in existing:
        _save_previous(existing)

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, pac_url)
        _refresh()
        return True, "Spotify traffic routed through the local proxy."
    except OSError as exc:
        return False, "Could not set AutoConfigURL (%s)" % exc


def disable():
    """Undo the routing, restoring whatever was configured before.

    Safe to call when nothing was ever set, and deliberately leaves an
    AutoConfigURL alone when it is not ours - a crash could otherwise have us
    tearing down a PAC that some other tool installed in the meantime.
    """
    current = _read_autoconfig()
    if current is not None and PAC_FILENAME not in current:
        # Someone else's PAC is installed now. Leave it and drop our parked
        # copy; restoring over it would be us overwriting a live setting.
        _forget_previous()
        return True, "Proxy routing already cleared."

    if current is None:
        # No AutoConfigURL at all. The old guard was `current is not None and
        # ...`, so this case fell through to the restore below and WROTE the
        # parked value into a slot that held nothing - resurrecting a corporate
        # PAC that Group Policy or a VPN client had since removed. There is
        # nothing of ours to undo here.
        _forget_previous()
        return True, "Proxy routing already cleared."

    previous = _read_saved()
    if previous is not None and not isinstance(previous, str):
        # REG_SZ is what we write, but the value is readable and writable by
        # anything running as this user. A non-string would raise inside the
        # key block below, past the OSError handler, leaving AutoConfigURL
        # pointing at a port that is about to close.
        previous = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INTERNET_SETTINGS, 0,
                            winreg.KEY_SET_VALUE) as key:
            if previous:
                winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, previous)
            else:
                try:
                    winreg.DeleteValue(key, "AutoConfigURL")
                except FileNotFoundError:
                    pass
        _forget_previous()
        _refresh()
        return True, "Proxy routing removed."
    except OSError as exc:
        return False, "Could not clear AutoConfigURL (%s)" % exc


def clear_spotify_prefs():
    """Remove proxy keys from Spotify's own prefs so the two cannot disagree.

    This file also holds the user's saved login blob, so it is handled as if it
    were precious: read as bytes so nothing is lost in a decode, left entirely
    alone when there is nothing to strip - which is the usual case - backed up
    once, and replaced atomically so a crash mid-write cannot leave the user
    logged out of Spotify.
    """
    prefs = os.path.join(os.environ.get("APPDATA", ""), "Spotify", "prefs")
    if not os.path.isfile(prefs):
        return False

    try:
        with open(prefs, "rb") as handle:
            original = handle.read()
    except OSError:
        return False

    kept = [line for line in original.splitlines(keepends=True)
            if not line.lstrip().startswith(b"network.proxy.")]
    updated = b"".join(kept)
    if updated == original:
        return True  # Nothing to do; do not touch the file at all.

    backup = prefs + ".skipper-backup"
    try:
        if not os.path.isfile(backup):
            with open(backup, "wb") as handle:
                handle.write(original)
    except OSError:
        pass

    temp = prefs + ".skipper-tmp"
    try:
        with open(temp, "wb") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, prefs)
        # The replace was atomic, so the backup has done its job. Keeping it
        # would leave a frozen copy of a file containing the user's saved login
        # blob lying around indefinitely - outliving a Spotify logout, a
        # credential rotation, and Spotify's own uninstall.
        try:
            os.remove(backup)
        except OSError:
            pass
        return True
    except OSError:
        try:
            if os.path.isfile(temp):
                os.remove(temp)
        except OSError:
            pass
        return False
