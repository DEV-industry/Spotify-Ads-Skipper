"""Local HTTPS proxy that drops Spotify's ad requests by URL path.

Spotify serves ads from the same host as everything else, so they can only be
separated once TLS is terminated and the request path is visible:

    POST /ads/v3/ads?slots=preroll     <- the ad fetch itself
    POST /ad-logic/prefetch
    GET  /ads/v2/config
         /sponsoredplaylist/v1/...

while playback travels /metadata/, /playplay/, /playlist/, /widevine-license/.
Blocking the former leaves music untouched and gapless - the client treats a
missing ad as "no ad to play" and moves straight to the next track.

Design constraints, in order of importance:

* Fail open. Anything unparseable or unexpected is forwarded verbatim. A bug
  here should at worst let an ad through, never break someone's playback.
* Narrow reach. Only hosts in SPOTIFY_SUFFIXES are carried at all, and only on
  web ports; anything else is refused outright. The PAC already sends the rest
  of the machine's traffic DIRECT, so nothing legitimate is turned away - and
  refusing keeps a listener that needs no authentication from doubling as an
  open relay for whatever else runs on the machine.
* HTTP/1.1 only. The server-side handshake advertises just http/1.1 via ALPN,
  so the client downgrades and no HTTP/2 framing implementation is needed.
"""

import re
import socket
import ssl
import threading
import urllib.parse

import proxy_ca

LISTEN_HOST = "127.0.0.1"
DEFAULT_PORT = 8799

# The only hosts this proxy will connect to at all. The PAC hands us Spotify
# and returns DIRECT for everything else, so nothing legitimate ever asks for
# another host - and refusing them keeps this from being an open relay that any
# local process could use to launder its own traffic through us.
SPOTIFY_SUFFIXES = (".spotify.com", ".scdn.co", ".spotifycdn.com")

# 443 only. Interception always speaks TLS, so a CONNECT to any other port
# would be accepted and then fail on the upstream handshake.
ALLOWED_PORTS = (443,)

# One RSA keygen and one SSLContext per distinct hostname, so an unbounded
# cache is an unbounded memory and CPU sink for anything that can reach the
# listener. Spotify itself touches a couple of dozen hostnames.
MAX_CACHED_CONTEXTS = 64

REFUSED_RESPONSE = (
    b"HTTP/1.1 403 Forbidden\r\n"
    b"Content-Length: 0\r\n"
    b"Connection: close\r\n"
    b"\r\n"
)

AD_PATH = re.compile(r"^/(ads?/|ad-logic/|sponsoredplaylist/)", re.I)
SAFE_PATH = re.compile(
    r"^/(metadata/|playplay/|playlist/|widevine-license/|extended-metadata/)", re.I
)

BLOCK_RESPONSE = (
    b"HTTP/1.1 404 Not Found\r\n"
    b"Content-Length: 0\r\n"
    b"Content-Type: application/octet-stream\r\n"
    b"Connection: keep-alive\r\n"
    b"\r\n"
)

# Returned by _relay_response when the exchange stopped being HTTP, so the
# caller stops parsing and starts tunnelling. A distinct object rather than a
# string or a bool: it must never be confused with "carry on".
UPGRADED = object()

SOCKET_TIMEOUT = 30
# Once upgraded there are no more request/response turns to time, and a
# WebSocket is idle for minutes at a time by design. Still finite, so a peer
# that dies without a FIN cannot hold one of MAX_CONNECTIONS for good.
TUNNEL_TIMEOUT = 900
# A fresh connection gets much less patience than an established one: the
# CONNECT line should arrive immediately, and holding a thread for 30 seconds
# per silent connection is all an abusive local process needs.
HANDSHAKE_TIMEOUT = 5
MAX_HEADER_BYTES = 65536

# Requests on this API are small; the largest seen is a few kilobytes. The cap
# exists because a declared Content-Length is committed before the body has to
# turn up, so an unbounded one is a memory sink for anything that can connect.
MAX_BODY_BYTES = 8 * 1024 * 1024

# Trailers after the terminating chunk. Spotify sends none; a handful is
# generous. The cap is on the count as well as the bytes, so a flood of
# two-byte lines cannot spin here indefinitely.
MAX_TRAILERS = 32

# Ceiling on connections handled at once. Each takes a thread, and nothing
# legitimate needs more than a handful.
MAX_CONNECTIONS = 48

# Responses that carry no body regardless of what their headers say.
BODYLESS_STATUSES = frozenset({204, 205, 304})

# Expected ways a connection ends: keep-alives expiring, clients hanging up,
# TLS closing early. None of them indicate a problem, and logging every one
# drowns the real entries - a single day produced ~900 such lines.
ROUTINE_ERRORS = (
    socket.timeout,
    TimeoutError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    ssl.SSLEOFError,
    ssl.SSLZeroReturnError,
)


# A hostname is letters, digits, hyphens and dots. Nothing else reaches the
# resolver, because two different things read the string and they disagree
# about where it ends:
#
#   "example.com\x00.spotify.com" satisfies the suffix test below, and then
#   getaddrinfo - C, NUL-terminated - resolves plain example.com. Measured:
#   the gate returned True and the proxy opened a connection to example.com,
#   which is exactly the open relay the gate exists to prevent.
#
# The same mismatch turns up without a NUL. IDNA nameprep strips soft hyphens,
# so "apre\xADsolve.spotify.com" resolves to the real host while remaining a
# distinct string - one that x509.DNSName later rejects, after the RSA keygen,
# and that therefore never lands in the context cache. Every request pays for
# a fresh key.
#
# Validating the exact bytes that will be resolved closes both, and does it
# before any work is done.
_VALID_HOST = re.compile(r"\A[A-Za-z0-9.-]{1,253}\Z")


def _canonical_host(host):
    """Lower-case the host, or None when it is not a plain hostname.

    Case is folded here rather than at each call site: DNS is case-insensitive,
    so "SPOTIFY.com" and "spotify.com" are one host, and letting them be two
    cache keys is another way to force an unbounded number of key generations.
    """
    if not _VALID_HOST.match(host):
        return None
    return host.lower()


def _intercept_host(host):
    host = _canonical_host(host)
    if host is None:
        return False
    return any(host.endswith(suffix) for suffix in SPOTIFY_SUFFIXES)


def _normalise_path(path):
    """Collapse the tricks that let an ad path slip past a prefix match."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    try:
        path = urllib.parse.unquote(path)
    except Exception:
        pass
    # Squash repeated slashes and resolve . / .. so "//ads/", "/./ads/" and
    # "/metadata/../ads/" all end up looking like "/ads/".
    parts = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    normalised = "/" + "/".join(parts)
    if path.endswith("/") and not normalised.endswith("/"):
        normalised += "/"
    return normalised


def _should_block(path):
    path = _normalise_path(path)
    if SAFE_PATH.match(path):
        return False
    return bool(AD_PATH.match(path))


def _read_headers(sock_file):
    """Read a request line plus headers. Returns (raw, first_line) or (None, None)."""
    raw = b""
    while b"\r\n\r\n" not in raw:
        # The size argument is what makes MAX_HEADER_BYTES a real limit. An
        # unbounded readline() buffers the whole line before the check below
        # can run, so a single header with no newline could be gigabytes.
        chunk = sock_file.readline(MAX_HEADER_BYTES + 1)
        if not chunk:
            return None, None
        raw += chunk
        if len(raw) > MAX_HEADER_BYTES:
            return None, None
    first = raw.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    return raw, first


def _content_lengths(raw):
    """Every Content-Length declared, as integers.

    The trailing CRLF is matched with a lookahead so it is not consumed:
    findall does not overlap, and two headers back to back share the separator
    between them - which is exactly the case worth catching.
    """
    return [int(m) for m in
            re.findall(rb"\r\ncontent-length:[ \t]*(\d+)[ \t]*(?=\r\n)", raw, re.I)]


def _content_length(raw):
    values = _content_lengths(raw)
    return values[0] if values else 0


def _is_chunked(raw):
    return re.search(rb"\r\ntransfer-encoding:.*chunked", raw, re.I) is not None


def _framing_is_ambiguous(raw):
    """True when a message can be framed more than one way.

    Conflicting Content-Length values, a length alongside chunked encoding, or
    a bare LF anywhere in the head all mean two parsers can disagree about
    where this message stops and the next begins. RFC 9112 tells a proxy to
    reject rather than guess, and nothing Spotify sends looks like this.

    The bare-LF test covers the whole header block, not just the request line.
    _content_lengths only matches headers delimited by CRLF, so a header sitting
    behind a bare LF is invisible to it - which is precisely how a second,
    conflicting Content-Length could sail through unnoticed while the upstream
    reads it and frames the body differently.
    """
    lengths = _content_lengths(raw)
    if len(set(lengths)) > 1:
        return True
    if lengths and _is_chunked(raw):
        return True
    # Every line of a well-formed head ends CRLF, so any LF without a CR in
    # front of it is a bare LF.
    return re.search(rb"(?<!\r)\n", raw) is not None


_HEX = frozenset(b"0123456789abcdefABCDEF")


def _chunk_size(line):
    """Size from a chunk header line, or None when it is not a valid one.

    int(token, 16) is too generous to frame with: it happily accepts "+5",
    "0x5" and "1_0", none of which an RFC-compliant server would ever produce
    or read the same way. Accepting them means we and the upstream can end up
    framing the same body differently.
    """
    token = line.split(b";", 1)[0].strip()
    if not token or not all(b in _HEX for b in token):
        return None
    try:
        return int(token, 16)
    except ValueError:
        return None


def _read_chunked_body(reader):
    """Read a chunked body exactly. Returns the raw bytes, or None if malformed.

    Sizes are parsed rather than scanned for, which is the whole point: the
    previous version stopped at the first line that happened to read "0", and
    Spotify's bodies are binary protobuf, so a payload containing that byte
    sequence ended the body early. The leftover was then parsed as the next
    request - and the client got the answer to a request it never sent.
    """
    out = bytearray()
    total = 0

    # `total` counts what the sender DECLARES; this counts what we actually
    # hold. The two diverge, and only the second one bounds memory:
    #
    #   "1;<65000 bytes of chunk-extension>\r\n" declares one byte. _chunk_size
    #   splits on ";" so `total` rises by 1 while the whole line is appended.
    #   Trailers were worse - that loop had no counter at all. Measured on the
    #   real function: 1132 MiB buffered in three seconds, `total` still 0.
    #
    # Every append is followed by this check, so no path can outrun the cap.
    def over_cap():
        return len(out) > MAX_BODY_BYTES

    while True:
        line = reader.readline(MAX_HEADER_BYTES + 1)
        if not line or not line.endswith(b"\n"):
            return None
        out += line
        if over_cap():
            return None
        size = _chunk_size(line)
        if size is None:
            return None

        total += size
        if total > MAX_BODY_BYTES:
            return None

        if size == 0:
            # Trailers, terminated by a blank line. Bounded three ways: a line
            # that never ends, a flood of short lines, and total bytes held.
            trailers = 0
            while True:
                trailer = reader.readline(MAX_HEADER_BYTES + 1)
                if not trailer or not trailer.endswith(b"\n"):
                    return None
                out += trailer
                if over_cap():
                    return None
                if trailer in (b"\r\n", b"\n"):
                    return bytes(out)
                trailers += 1
                if trailers > MAX_TRAILERS:
                    return None

        remaining = size
        while remaining > 0:
            chunk = reader.read(min(65536, remaining))
            if not chunk:
                return None
            out += chunk
            if over_cap():
                return None
            remaining -= len(chunk)
        tail = reader.read(2)  # The CRLF that closes the chunk.
        if not tail:
            return None
        out += tail
        if over_cap():
            return None


def _status_code(raw):
    """Status code from a response head, or None."""
    try:
        return int(raw.split(b"\r\n", 1)[0].split()[1])
    except (IndexError, ValueError):
        return None


_upstream_ctx_cache = []


def _upstream_ctx():
    """Client-side TLS context for the leg out to Spotify.

    Trust anchors come from certifi rather than the Windows root store. The
    store contains this app's own CA, and anchoring on it would mean a stolen
    copy of our key could sit in front of the upstream connection as well as
    the client one - the very hop that is supposed to notice.
    """
    if _upstream_ctx_cache:
        return _upstream_ctx_cache[0]

    # Fails closed. The previous fallback to ssl.create_default_context() was
    # worse than not running: on Windows that loads the user's root store,
    # which by then contains this app's own CA - measured, 123 anchors with
    # "Spotify Ads Skipper Local CA" among them. The upstream leg is the one
    # hop that is supposed to notice a forged Spotify certificate, and
    # anchoring it on our own signing key is precisely the circularity the
    # docstring above says must be avoided. Silent, too: this function has no
    # logger and the context is cached for the life of the process.
    #
    # certifi is a hard dependency and PyInstaller bundles it, so reaching the
    # raise means the build is broken - which the caller surfaces as "ads are
    # NOT being blocked" rather than quietly weakening the guarantee.
    import certifi

    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.set_alpn_protocols(["http/1.1"])
    _upstream_ctx_cache.append(ctx)
    return ctx


def upstream_anchor_count():
    """How many trust anchors the upstream leg uses, and whether ours is one.

    Exposed for the self test: a bundle that silently lost certifi, or a
    context that somehow picked up the Windows store, is exactly the failure
    that has no visible symptom until someone is being intercepted.
    """
    ctx = _upstream_ctx()
    anchors = ctx.get_ca_certs()
    ours = any(
        proxy_ca.CA_COMMON_NAME in str(entry.get("subject", ""))
        for entry in anchors
    )
    return len(anchors), ours


class AdProxy(threading.Thread):
    """Threaded HTTPS-intercepting proxy. One thread per connection."""

    daemon = True

    def __init__(self, port=DEFAULT_PORT, log=None):
        super().__init__(name="AdProxy")
        self.port = port
        self._log = log or (lambda _m: None)
        self._server = None
        # Not `_stop`: Thread already has a private _stop() method, and
        # shadowing it with an Event makes is_alive() raise TypeError as soon
        # as the thread finishes.
        self._stop_event = threading.Event()
        self._slots = threading.Semaphore(MAX_CONNECTIONS)

        # Counters are read by the tray's status line from another thread and
        # written from every connection thread, so they take the same lock as
        # the context cache. `+=` is not atomic and the tray would otherwise
        # under-report - which reads as "the proxy is not working".
        self.blocked = 0
        self.passed = 0
        self.refused = 0
        self._counter_lock = threading.Lock()
        self.bind_error = None

        self._ca_cert, self._ca_key = proxy_ca.load_ca()
        self._ctx_cache = {}
        self._cache_lock = threading.Lock()

    @property
    def listening(self):
        """Whether the listener actually came up and owns its port."""
        return self._server is not None and self.bind_error is None

    # -- TLS ---------------------------------------------------------------

    def _server_ctx(self, hostname):
        """An SSLContext presenting a leaf for `hostname`, HTTP/1.1 only."""
        with self._cache_lock:
            ctx = self._ctx_cache.get(hostname)
            if ctx is not None:
                return ctx

        import os
        import tempfile

        from cryptography.hazmat.primitives import serialization

        cert, key = proxy_ca.make_leaf(hostname, self._ca_cert, self._ca_key)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Advertising only http/1.1 makes the client skip HTTP/2, which keeps
        # this proxy to a single, simple wire format.
        ctx.set_alpn_protocols(["http/1.1"])

        # Written into the CA directory rather than %TEMP%: SSLContext can only
        # load a chain from a path, and %TEMP% files inherit permissions that
        # let every process on the machine read this private key. The CA
        # directory is already locked to this account.
        os.makedirs(proxy_ca.CA_DIR, exist_ok=True)
        handle, path = tempfile.mkstemp(suffix=".pem", dir=proxy_ca.CA_DIR)
        try:
            with os.fdopen(handle, "wb") as tmp:
                tmp.write(cert.public_bytes(serialization.Encoding.PEM))
                tmp.write(key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                ))
            proxy_ca._lock_down(path)
            ctx.load_cert_chain(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        with self._cache_lock:
            if len(self._ctx_cache) >= MAX_CACHED_CONTEXTS:
                # Plain FIFO eviction. Spotify uses far fewer hostnames than
                # the cap, so this only ever trips under something abusive, and
                # then all we want is a ceiling - not a good hit rate.
                self._ctx_cache.pop(next(iter(self._ctx_cache)), None)
            self._ctx_cache[hostname] = ctx
        return ctx

    # -- connection handling ----------------------------------------------

    def run(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_EXCLUSIVEADDRUSE, not SO_REUSEADDR. Windows' SO_REUSEADDR is not
        # the POSIX one: it lets a second process bind a port that is already
        # live and take delivery of the connections. With it set, any local
        # process that got here first would silently receive all of Spotify's
        # proxied traffic while this thread happily logged "listening".
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except (AttributeError, OSError):
            pass
        try:
            server.bind((LISTEN_HOST, self.port))
            server.listen(128)
        except OSError as exc:
            # Someone else owns the port. Say so and stay down: pretending to
            # run would leave the tray reporting a healthy proxy that is not
            # actually receiving anything.
            self.bind_error = str(exc)
            self._log("Proxy could not bind %s:%d: %s" % (LISTEN_HOST, self.port, exc))
            try:
                server.close()
            except OSError:
                pass
            return
        self._server = server
        self._server.settimeout(1.0)
        self._log("Proxy listening on %s:%d" % (LISTEN_HOST, self.port))

        while not self._stop_event.is_set():
            try:
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # Bounded concurrency: without it, anything that can open sockets
            # to loopback can hold a thread per connection for the full socket
            # timeout and push the tray app into thousands of them.
            if not self._slots.acquire(blocking=False):
                self._log("At the connection ceiling; turning one away")
                try:
                    client.close()
                except OSError:
                    pass
                continue
            try:
                threading.Thread(target=self._handle_slot, args=(client,),
                                 daemon=True).start()
            except RuntimeError as exc:
                # Out of threads. Drop this connection rather than let the
                # exception unwind the accept loop, which would stop the proxy
                # while `listening` still claimed everything was fine.
                self._log("Could not start a connection thread: %s" % exc)
                self._slots.release()
                try:
                    client.close()
                except OSError:
                    pass

        try:
            self._server.close()
        except OSError:
            pass

    def _handle_slot(self, client):
        try:
            self._handle(client)
        finally:
            self._slots.release()

    def stop(self):
        self._stop_event.set()
        try:
            if self._server:
                self._server.close()
        except OSError:
            pass

    def _handle(self, client):
        try:
            # Inside the try: on a socket the peer already reset, settimeout
            # itself raises, and out here that killed the thread without a word.
            client.settimeout(HANDSHAKE_TIMEOUT)
            reader = client.makefile("rb")
            raw, first = _read_headers(reader)
            if not first or not first.upper().startswith("CONNECT"):
                # Only CONNECT is expected; anything else is not ours to rewrite.
                client.close()
                return

            target = first.split()[1]
            raw_host, _, port_text = target.partition(":")
            try:
                port = int(port_text or 443)
            except ValueError:
                self._refuse(client, "malformed port in %r" % target)
                return

            # Refuse anything that is not Spotify on a web port. Without this
            # the listener is an unauthenticated proxy that any local process
            # can use to reach arbitrary hosts and ports - including services
            # bound to loopback that it could not otherwise talk to - with the
            # connections appearing to come from this app.
            #
            # Everything downstream uses the canonical form, never the bytes
            # the client sent: the string that was validated has to be the
            # string that gets resolved, cached and put into a certificate, or
            # the check does not mean anything.
            host = _canonical_host(raw_host)
            if host is None or not _intercept_host(host) or port not in ALLOWED_PORTS:
                self._refuse(client, "%r:%d is out of scope" % (raw_host, port))
                return

            upstream = socket.create_connection((host, port), timeout=SOCKET_TIMEOUT)
            # Past the handshake this is a long-lived connection again.
            client.settimeout(SOCKET_TIMEOUT)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

            self._intercept(client, upstream, host)
        except ROUTINE_ERRORS:
            # Idle keep-alives expiring, clients hanging up mid-handshake and
            # resets are all normal here and say nothing about whether the proxy
            # works. Logging them buried the useful lines under hundreds of
            # repeats within a day, so they are dropped on the floor.
            try:
                client.close()
            except OSError:
                pass
        except Exception as exc:
            self._log("Connection failed: %s" % exc)
            try:
                client.close()
            except OSError:
                pass

    def _refuse(self, client, reason):
        """Turn away a CONNECT we will not carry."""
        with self._counter_lock:
            self.refused += 1
        self._log("Refused CONNECT: %s" % reason)
        try:
            client.sendall(REFUSED_RESPONSE)
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass

    def _intercept(self, client, upstream, host):
        """Terminate TLS both ways and filter requests by path."""
        try:
            tls_client = self._server_ctx(host).wrap_socket(client, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            self._log("Client handshake failed for %s: %s" % (host, exc))
            try:
                client.close()
                upstream.close()
            except OSError:
                pass
            return

        up_ctx = _upstream_ctx()
        try:
            tls_up = up_ctx.wrap_socket(upstream, server_hostname=host)
        except (ssl.SSLError, OSError) as exc:
            self._log("Upstream handshake failed for %s: %s" % (host, exc))
            try:
                tls_client.close()
            except OSError:
                pass
            return

        try:
            self._proxy_requests(tls_client, tls_up, host)
        finally:
            for sock in (tls_client, tls_up):
                try:
                    sock.close()
                except OSError:
                    pass

    def _proxy_requests(self, tls_client, tls_up, host):
        client_reader = tls_client.makefile("rb")
        up_reader = tls_up.makefile("rb")

        while not self._stop_event.is_set():
            raw, first = _read_headers(client_reader)
            if not raw:
                return

            parts = first.split()
            method = parts[0].upper() if parts else ""
            path = parts[1] if len(parts) > 1 else ""

            if _framing_is_ambiguous(raw):
                self._log("Dropped a request whose framing is ambiguous")
                return

            length = _content_length(raw)
            if length > MAX_BODY_BYTES:
                # The declared length gets allocated before a single body byte
                # has to arrive, so an honest-looking header can reserve
                # gigabytes of commit charge. Nothing on this API is that big.
                self._log("Dropped a request declaring a %d byte body" % length)
                return

            body = b""
            if length:
                body = client_reader.read(length)
                if body is None or len(body) < length:
                    return
            elif _is_chunked(raw):
                body = _read_chunked_body(client_reader)
                if body is None:
                    self._log("Dropped a request with a malformed chunked body")
                    return

            if path and _should_block(path):
                with self._counter_lock:
                    self.blocked += 1
                self._log("Blocked %s%s" % (host, _normalise_path(path)))
                try:
                    tls_client.sendall(BLOCK_RESPONSE)
                except OSError:
                    return
                continue

            with self._counter_lock:
                self.passed += 1
            try:
                tls_up.sendall(raw + body)
            except OSError:
                return

            outcome = self._relay_response(up_reader, tls_client, method)
            # Checked before truthiness: UPGRADED is a truthy object, and
            # "keep going" is exactly the wrong reading of it.
            if outcome is UPGRADED:
                self._relay_tunnel(client_reader, tls_client, up_reader, tls_up)
                return
            if not outcome:
                return

    def _relay_tunnel(self, client_reader, tls_client, up_reader, tls_up):
        """Pump bytes both ways until either side hangs up.

        Reached only after a 101, where HTTP framing no longer applies. Reads
        go through the buffered readers rather than the sockets: the response
        head was read through them, so frames that arrived in the same TCP
        segment are already sitting in their buffers and a raw recv() would
        skip straight past them. read1() hands back whatever is buffered
        without waiting for a full block.
        """
        for sock in (tls_client, tls_up):
            try:
                # A WebSocket is idle for long stretches by design, and the
                # request-oriented timeout would tear it down mid-conversation.
                # Still bounded: a silently dead peer must not hold a slot out
                # of the connection ceiling for the life of the process.
                sock.settimeout(TUNNEL_TIMEOUT)
            except OSError:
                return

        def pump(reader, dest):
            try:
                while True:
                    data = reader.read1(65536)
                    if not data:
                        break
                    dest.sendall(data)
            except (OSError, ValueError):
                pass
            finally:
                # Unblock the other direction, which is parked in read1() on a
                # socket that will never speak again.
                try:
                    dest.shutdown(socket.SHUT_RDWR)
                except (OSError, ValueError):
                    pass

        upward = threading.Thread(
            target=pump, args=(client_reader, tls_up),
            name="TunnelUp", daemon=True,
        )
        upward.start()
        pump(up_reader, tls_client)
        upward.join(timeout=5)

    def _relay_response(self, up_reader, tls_client, method=""):
        """Forward one response.

        Returns False when the connection is done, UPGRADED when it has stopped
        being HTTP, True to carry on with the next request.
        """
        while True:
            raw, _ = _read_headers(up_reader)
            if not raw:
                return False
            try:
                tls_client.sendall(raw)
            except OSError:
                return False

            status = _status_code(raw)

            # 101 first, because it is in the 1xx range and means the opposite
            # of the rest of it. Spotify's dealer connection - dealer.spotify.com
            # and its regional siblings, all of which this proxy carries - is a
            # WebSocket, and after the 101 the bytes are WS frames, not another
            # HTTP head. Treating it as interim made the loop read those frames
            # as a response head and throw them away, so the client saw a
            # successful handshake onto a socket that then delivered nothing,
            # forever, in a reconnect loop. Hand the tunnel over instead.
            if status == 101:
                return UPGRADED

            # 1xx proper is interim: the real response still follows on the
            # same connection. Returning here would leave it in the buffer and
            # hand it to the NEXT request, so every reply after one 103 Early
            # Hints - which CDNs in front of the CDN domains do send - would be
            # one request out of step. Go round and read the real head.
            if status is not None and 100 <= status < 200:
                continue

            # A 304 or a reply to HEAD describes a body it does not send.
            # Reading one blocks until the socket times out and strands
            # everything queued behind it.
            if method == "HEAD" or (status is not None and status in BODYLESS_STATUSES):
                return True
            break

        # Presence of the header, not truthiness of its value. _content_length
        # returns 0 both for "no such header" and for "the server said zero",
        # and the old `if length:` sent the second case down the read-until-
        # close path at the bottom of this function. A perfectly ordinary
        # "200 OK, Content-Length: 0, keep-alive" then blocked for the full
        # socket timeout while the client, which already considered that
        # response complete, sent its next request into a connection about to
        # be torn down. This app's own BLOCK_RESPONSE is shaped exactly like
        # that, so it was modelling the case it mishandled.
        lengths = _content_lengths(raw)
        if lengths:
            remaining = lengths[0]
            while remaining > 0:
                chunk = up_reader.read(min(65536, remaining))
                if not chunk:
                    return False
                tls_client.sendall(chunk)
                remaining -= len(chunk)
            return True

        if _is_chunked(raw):
            while True:
                line = up_reader.readline(MAX_HEADER_BYTES + 1)
                if not line:
                    return False
                tls_client.sendall(line)
                size = _chunk_size(line)
                if size is None:
                    return False
                if size == 0:
                    # Trailers run until a blank line. Reading a single line
                    # here left the terminator in the buffer, so the next
                    # response head started with a stray CRLF and the whole
                    # connection slid out of step.
                    trailers = 0
                    while True:
                        trailer = up_reader.readline(MAX_HEADER_BYTES + 1)
                        if not trailer:
                            return False
                        tls_client.sendall(trailer)
                        if trailer in (b"\r\n", b"\n"):
                            return True
                        trailers += 1
                        if trailers > MAX_TRAILERS:
                            return False
                remaining = size
                while remaining > 0:
                    chunk = up_reader.read(min(65536, remaining))
                    if not chunk:
                        return False
                    tls_client.sendall(chunk)
                    remaining -= len(chunk)
                # Bounded like every other readline in this file. Left
                # unbounded, a chunk followed by no newline buffered without
                # limit - measured at 96.7 MiB of heap for 48 MiB fed.
                tail = up_reader.readline(MAX_HEADER_BYTES + 1)
                if not tail:
                    return False
                tls_client.sendall(tail)

        # Neither framing header: the body runs until the server closes, so
        # relay it that way instead of hanging up on the client mid-message.
        while True:
            chunk = up_reader.read(65536)
            if not chunk:
                return False
            try:
                tls_client.sendall(chunk)
            except OSError:
                return False
