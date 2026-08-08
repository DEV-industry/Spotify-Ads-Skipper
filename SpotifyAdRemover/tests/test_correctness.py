"""Regression suite.

Run from the repository root:

    python -m unittest discover -s SpotifyAdRemover/tests -v

Everything here is offline and side-effect free: no sockets to the outside
world, no certificate store, no registry writes, no Spotify. Paths that would
touch real user data are redirected at temporary directories.

The point of this file is that the invariants below stop being things somebody
has to remember. Each one is either a rule the product depends on - an ad path
is blocked, a playback path never is - or a trap that has already been fallen
into once.
"""

import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ad_proxy
import proxy_ca
import proxy_config
import xpui_patch


# -- ad_proxy: which requests are blocked -----------------------------------

class TestPathClassification(unittest.TestCase):
    """The rule the whole product rests on.

    A missed ad path means an ad plays. A blocked playback path means the
    music stops, which is far worse - so the second direction is tested at
    least as hard as the first.
    """

    AD_PATHS = [
        "/ads/v3/ads?slots=preroll",
        "/ads/v2/config",
        "/ad-logic/prefetch",
        "/ad-logic/state/config",
        "/sponsoredplaylist/v1/sponsored",
        "/ad/v1/anything",
    ]

    PLAYBACK_PATHS = [
        "/metadata/4/track/abc",
        "/extended-metadata/v0/extended-metadata",
        "/playplay/v1/key/abc",
        "/playlist/v2/playlist/abc",
        "/widevine-license/v1/audio/license",
    ]

    OTHER_PATHS = [
        "/",
        "/addresses/v1/list",       # starts with "ad" but is not "ad/" or "ads/"
        "/adjacent/thing",
        "/user-profile/v3/me",
        "/track/v1/abc",
    ]

    def test_ad_paths_are_blocked(self):
        for path in self.AD_PATHS:
            with self.subTest(path=path):
                self.assertTrue(ad_proxy._should_block(path))

    def test_playback_paths_are_never_blocked(self):
        for path in self.PLAYBACK_PATHS:
            with self.subTest(path=path):
                self.assertFalse(ad_proxy._should_block(path))

    def test_unrelated_paths_are_not_blocked(self):
        for path in self.OTHER_PATHS:
            with self.subTest(path=path):
                self.assertFalse(ad_proxy._should_block(path))

    def test_traversal_cannot_smuggle_an_ad_path_past_the_matcher(self):
        for path in [
            "/metadata/../ads/v3/ads",
            "//ads/v3/ads",
            "/./ads/v3/ads",
            "/metadata/%2e%2e/ads/v3/ads",
            "/%61ds/v3/ads",
        ]:
            with self.subTest(path=path):
                self.assertTrue(ad_proxy._should_block(path))

    def test_traversal_out_of_an_ad_path_is_not_blocked(self):
        # The reverse direction: a request that ends up somewhere legitimate
        # must not be blocked just because it passed through "ads" on the way.
        self.assertFalse(ad_proxy._should_block("/ads/../metadata/4/track/abc"))

    def test_safe_prefix_wins_over_ad_prefix(self):
        self.assertFalse(ad_proxy._should_block("/metadata/ads/thing"))

    def test_query_string_does_not_affect_the_decision(self):
        self.assertEqual(
            ad_proxy._should_block("/ads/v3/ads"),
            ad_proxy._should_block("/ads/v3/ads?slots=preroll&foo=bar#frag"),
        )


class TestHostGate(unittest.TestCase):
    """Which hosts the proxy will carry at all."""

    def test_spotify_hosts_are_carried(self):
        for host in ["gew4-spclient.spotify.com", "audio-fa.scdn.co",
                     "i.scdn.co", "dealer.spotify.com", "x.spotifycdn.com"]:
            with self.subTest(host=host):
                self.assertTrue(ad_proxy._intercept_host(host))

    def test_case_is_irrelevant(self):
        self.assertTrue(ad_proxy._intercept_host("GEW4-SPCLIENT.SPOTIFY.COM"))
        self.assertEqual(
            ad_proxy._canonical_host("GEW4-Spclient.Spotify.COM"),
            "gew4-spclient.spotify.com",
        )

    def test_suffix_confusion_is_refused(self):
        for host in ["evilspotify.com", "spotify.com.evil.com", "notscdn.co",
                     "spotify.com.attacker.example"]:
            with self.subTest(host=host):
                self.assertFalse(ad_proxy._intercept_host(host))

    def test_control_and_unicode_tricks_are_refused(self):
        # A NUL passes a Python suffix test but truncates in the C resolver;
        # a soft hyphen is stripped by IDNA. Both must die at the gate.
        for host in ["example.com\x00.spotify.com", "evil.com\x00.scdn.co",
                     "apre\xadsolve.spotify.com", "sp。spotify.com",
                     "sp．spotify.com", "host\n.spotify.com",
                     "host\r.spotify.com", " gew4.spotify.com"]:
            with self.subTest(host=repr(host)):
                self.assertFalse(ad_proxy._intercept_host(host))
                self.assertIsNone(ad_proxy._canonical_host(host))

    def test_empty_and_oversized_hosts_are_refused(self):
        self.assertIsNone(ad_proxy._canonical_host(""))
        self.assertIsNone(ad_proxy._canonical_host("a" * 250 + ".spotify.com"))

    def test_the_intercept_list_matches_what_the_ca_may_sign(self):
        # These two drifting apart means either signing certificates for hosts
        # the CA cannot vouch for, or refusing hosts it can.
        suffixes = {s.lstrip(".") for s in ad_proxy.SPOTIFY_SUFFIXES}
        self.assertEqual(suffixes, set(proxy_ca.PERMITTED_DOMAINS))


class TestHttpParsing(unittest.TestCase):
    def test_chunk_size_rejects_what_it_should(self):
        self.assertEqual(ad_proxy._chunk_size(b"a\r\n"), 10)
        self.assertEqual(ad_proxy._chunk_size(b"1;ext=1\r\n"), 1)
        self.assertEqual(ad_proxy._chunk_size(b"0\r\n"), 0)
        for bad in [b"+5\r\n", b"0x5\r\n", b"1_0\r\n", b"\r\n", b"zz\r\n"]:
            with self.subTest(line=bad):
                self.assertIsNone(ad_proxy._chunk_size(bad))

    def test_content_lengths_finds_back_to_back_headers(self):
        raw = b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nContent-Length: 20\r\n\r\n"
        self.assertEqual(ad_proxy._content_lengths(raw), [3, 20])

    def test_absent_and_zero_length_are_distinguishable(self):
        absent = b"HTTP/1.1 200 OK\r\nX: y\r\n\r\n"
        zero = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        self.assertEqual(ad_proxy._content_lengths(absent), [])
        self.assertEqual(ad_proxy._content_lengths(zero), [0])
        # _content_length collapses both to 0, which is why callers that need
        # to tell them apart must use _content_lengths.
        self.assertEqual(ad_proxy._content_length(absent), 0)
        self.assertEqual(ad_proxy._content_length(zero), 0)

    def test_ambiguous_framing_is_detected(self):
        for raw in [
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nContent-Length: 20\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nX: y\nContent-Length: 3\r\n\r\n",
        ]:
            with self.subTest(raw=raw[:40]):
                self.assertTrue(ad_proxy._framing_is_ambiguous(raw))

    def test_unambiguous_framing_is_allowed(self):
        for raw in [
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nContent-Length: 3\r\n\r\n",
        ]:
            with self.subTest(raw=raw[:40]):
                self.assertFalse(ad_proxy._framing_is_ambiguous(raw))

    def test_status_code(self):
        self.assertEqual(ad_proxy._status_code(b"HTTP/1.1 204 No Content\r\n\r\n"), 204)
        self.assertEqual(ad_proxy._status_code(b"HTTP/1.1 101 Switching\r\n\r\n"), 101)
        self.assertIsNone(ad_proxy._status_code(b"garbage\r\n\r\n"))


class _Endless(io.RawIOBase):
    """A peer that keeps talking and never terminates the message."""

    def __init__(self, block, limit=200 * 1024 * 1024):
        self.block = block
        self.sent = 0
        self.limit = limit

    def readable(self):
        return True

    def readinto(self, buf):
        if self.sent >= self.limit:
            return 0
        n = min(len(buf), len(self.block))
        buf[:n] = self.block[:n]
        self.sent += n
        return n


class TestChunkedBodyLimits(unittest.TestCase):
    """A hostile body must not be able to buy unbounded memory."""

    def _read(self, block):
        return ad_proxy._read_chunked_body(
            io.BufferedReader(_Endless(block), buffer_size=65536))

    def test_well_formed_body_is_returned_verbatim(self):
        good = b"5\r\nHELLO\r\n0\r\n\r\n"
        self.assertEqual(
            ad_proxy._read_chunked_body(io.BufferedReader(io.BytesIO(good))), good)

    def test_body_with_a_trailer_is_accepted(self):
        good = b"5\r\nHELLO\r\n0\r\nX-Sig: abc\r\n\r\n"
        self.assertEqual(
            ad_proxy._read_chunked_body(io.BufferedReader(io.BytesIO(good))), good)

    def test_chunk_extension_padding_is_refused(self):
        self.assertIsNone(self._read(b"1;" + b"A" * 65000 + b"\r\nX\r\n"))

    def test_trailer_flood_is_refused(self):
        self.assertIsNone(self._read(b"0\r\n" + b"X: y\r\n" * 4096))

    def test_unterminated_trailer_line_is_refused(self):
        self.assertIsNone(self._read(b"0\r\n" + b"Z" * 65536))

    def test_oversized_honest_body_is_refused(self):
        self.assertIsNone(self._read(b"100000\r\n" + b"B" * 0x100000 + b"\r\n"))

    def test_truncated_body_is_refused(self):
        self.assertIsNone(ad_proxy._read_chunked_body(
            io.BufferedReader(io.BytesIO(b"5\r\nHEL"))))


class _FakeSock:
    def __init__(self):
        self.out = bytearray()

    def sendall(self, data):
        self.out += data

    def settimeout(self, _v):
        pass


class TestResponseRelay(unittest.TestCase):
    def setUp(self):
        self.proxy = ad_proxy.AdProxy(port=0, log=lambda _m: None)

    def _relay(self, wire, method="GET"):
        sock = _FakeSock()
        reader = io.BufferedReader(io.BytesIO(wire))
        return self.proxy._relay_response(reader, sock, method), sock, reader

    def test_zero_length_response_completes_without_reading_further(self):
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: keep-alive\r\n\r\n"
        nxt = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"
        outcome, sock, reader = self._relay(head + nxt)
        self.assertIs(outcome, True)
        self.assertEqual(bytes(sock.out), head)
        self.assertTrue(reader.read().startswith(b"HTTP/1.1 200 OK"))

    def test_body_is_relayed(self):
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n"
        outcome, sock, _ = self._relay(head + b"HELLO")
        self.assertIs(outcome, True)
        self.assertEqual(bytes(sock.out), head + b"HELLO")

    def test_101_hands_over_instead_of_parsing(self):
        head = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n"
        outcome, sock, _ = self._relay(head + b"\x81\x05HELLO")
        self.assertIs(outcome, ad_proxy.UPGRADED)
        self.assertEqual(bytes(sock.out), head)

    def test_interim_1xx_is_followed_by_the_real_response(self):
        wire = (b"HTTP/1.1 103 Early Hints\r\nLink: </x>\r\n\r\n"
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
        outcome, sock, reader = self._relay(wire)
        self.assertIs(outcome, True)
        self.assertTrue(bytes(sock.out).endswith(b"hi"))
        self.assertEqual(reader.read(), b"")

    def test_head_response_body_is_not_read(self):
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n"
        outcome, sock, reader = self._relay(head + b"leftover", method="HEAD")
        self.assertIs(outcome, True)
        self.assertEqual(reader.read(), b"leftover")

    def test_304_declares_a_body_it_does_not_send(self):
        head = b"HTTP/1.1 304 Not Modified\r\nContent-Length: 500\r\n\r\n"
        outcome, _, reader = self._relay(head + b"leftover")
        self.assertIs(outcome, True)
        self.assertEqual(reader.read(), b"leftover")

    def test_ambiguous_response_framing_closes_the_connection(self):
        head = (b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n")
        outcome, _, _ = self._relay(head)
        self.assertIs(outcome, False)


class TestTunnel(unittest.TestCase):
    """After a 101 the bytes are opaque and must flow both ways."""

    def test_frames_buffered_with_the_head_are_not_lost(self):
        proxy = ad_proxy.AdProxy(port=0, log=lambda _m: None)
        client_a, client_b = socket.socketpair()
        up_a, up_b = socket.socketpair()
        for s in (client_a, client_b, up_a, up_b):
            s.settimeout(10)
        try:
            head = b"HTTP/1.1 101 Switching Protocols\r\n\r\n"
            early = b"\x81\x05HELLO"
            up_b.sendall(head + early)  # one segment: head and frame together
            time.sleep(0.2)

            up_reader = up_a.makefile("rb")
            client_reader = client_a.makefile("rb")
            self.assertIs(
                proxy._relay_response(up_reader, client_a, "GET"), ad_proxy.UPGRADED)
            self.assertEqual(client_b.recv(4096), head)

            t = threading.Thread(
                target=proxy._relay_tunnel,
                args=(client_reader, client_a, up_reader, up_a), daemon=True)
            t.start()
            time.sleep(0.3)
            self.assertEqual(client_b.recv(4096), early)

            up_b.sendall(b"\x81\x05WORLD")
            time.sleep(0.3)
            self.assertEqual(client_b.recv(4096), b"\x81\x05WORLD")

            client_b.sendall(b"\x81\x04PING")
            time.sleep(0.3)
            self.assertEqual(up_b.recv(4096), b"\x81\x04PING")

            up_b.close()
            t.join(timeout=6)
            self.assertFalse(t.is_alive(), "tunnel thread outlived its peer")
        finally:
            for s in (client_a, client_b, up_a, up_b):
                try:
                    s.close()
                except OSError:
                    pass


class TestUpstreamAnchors(unittest.TestCase):
    def test_our_own_ca_is_never_an_upstream_anchor(self):
        count, ours = ad_proxy.upstream_anchor_count()
        self.assertGreater(count, 0)
        self.assertFalse(ours, "the proxy would trust certificates it signed itself")


# -- proxy_ca ---------------------------------------------------------------

class TestCaConstraints(unittest.TestCase):
    """is_constrained() decides whether an existing CA is reused.

    Anything it wrongly approves is a limit that never reaches an installed
    user, so it is tested against a CA missing each limit in turn.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="skipper-ca-test-")
        cls._orig = (proxy_ca.CA_DIR, proxy_ca.CA_CERT, proxy_ca.CA_KEY)
        proxy_ca.CA_DIR = cls.tmp
        proxy_ca.CA_CERT = os.path.join(cls.tmp, "skipper-ca.pem")
        proxy_ca.CA_KEY = os.path.join(cls.tmp, "skipper-ca.key")
        cls.cert, cls.key = proxy_ca.generate_ca()

    @classmethod
    def tearDownClass(cls):
        proxy_ca.CA_DIR, proxy_ca.CA_CERT, proxy_ca.CA_KEY = cls._orig
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _build(self, permitted=None, excluded="both", eku=True):
        import datetime
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")])
        now = __import__("datetime").datetime.now(datetime.timezone.utc)
        names = permitted if permitted is not None else proxy_ca.PERMITTED_DOMAINS
        excl = {
            "both": [x509.IPAddress(ipaddress.ip_network("0.0.0.0/0")),
                     x509.IPAddress(ipaddress.ip_network("::/0"))],
            "v4": [x509.IPAddress(ipaddress.ip_network("0.0.0.0/0"))],
            "none": None,
        }[excluded]
        b = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
             .public_key(key.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(now - datetime.timedelta(minutes=5))
             .not_valid_after(now + datetime.timedelta(days=10))
             .add_extension(x509.NameConstraints(
                 permitted_subtrees=[x509.DNSName(d) for d in names],
                 excluded_subtrees=excl), critical=True))
        if eku:
            b = b.add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        return b.sign(key, hashes.SHA256())

    def test_a_freshly_generated_ca_is_accepted(self):
        self.assertTrue(proxy_ca.is_constrained(self.cert))

    def test_missing_eku_is_rejected(self):
        self.assertFalse(proxy_ca.is_constrained(self._build(eku=False)))

    def test_missing_ip_exclusions_is_rejected(self):
        self.assertFalse(proxy_ca.is_constrained(self._build(excluded="none")))

    def test_ipv4_only_exclusion_is_rejected(self):
        self.assertFalse(proxy_ca.is_constrained(self._build(excluded="v4")))

    def test_wrong_domain_set_is_rejected(self):
        self.assertFalse(proxy_ca.is_constrained(self._build(permitted=["example.com"])))
        self.assertFalse(proxy_ca.is_constrained(self._build(permitted=["spotify.com"])))

    def test_generated_ca_carries_the_expected_extensions(self):
        from cryptography import x509

        nc = self.cert.extensions.get_extension_for_class(x509.NameConstraints)
        self.assertTrue(nc.critical, "name constraints must be critical")
        bc = self.cert.extensions.get_extension_for_class(x509.BasicConstraints)
        self.assertTrue(bc.value.ca)
        self.assertEqual(bc.value.path_length, 0, "sub-CAs must be impossible")

    def test_leaf_for_a_hostname_carries_a_dns_san(self):
        from cryptography import x509

        leaf, _ = proxy_ca.make_leaf("gew4-spclient.spotify.com", self.cert, self.key)
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual([type(g).__name__ for g in san], ["DNSName"])

    def test_key_round_trips_and_is_marked_sealed(self):
        loaded = proxy_ca._load_key()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.key_size, self.key.key_size)
        with open(proxy_ca.CA_KEY, "rb") as handle:
            head = handle.read(len(proxy_ca.KEY_MAGIC))
        self.assertEqual(head, proxy_ca.KEY_MAGIC)
        self.assertTrue(proxy_ca.key_is_sealed)

    def test_a_plaintext_key_file_is_recognised_and_re_sealed(self):
        from cryptography.hazmat.primitives import serialization

        pem = self.key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption())
        with open(proxy_ca.CA_KEY, "wb") as handle:
            handle.write(proxy_ca.KEY_PLAIN_MAGIC + pem)
        self.assertIsNotNone(proxy_ca._load_key())
        # DPAPI works here, so the load path should have re-sealed it.
        with open(proxy_ca.CA_KEY, "rb") as handle:
            self.assertEqual(handle.read(len(proxy_ca.KEY_MAGIC)), proxy_ca.KEY_MAGIC)

    def test_a_corrupt_key_file_is_reported_rather_than_raising(self):
        with open(proxy_ca.CA_KEY, "wb") as handle:
            handle.write(b"not a key")
        self.assertIsNone(proxy_ca._load_key())


# -- proxy_config -----------------------------------------------------------

class TestPacContents(unittest.TestCase):
    def test_pac_routes_only_spotify_and_falls_back_to_direct(self):
        body = proxy_config.build_pac(4242).decode()
        self.assertIn("PROXY 127.0.0.1:4242; DIRECT", body)
        self.assertIn('return "DIRECT"', body)
        for domain in proxy_ca.PERMITTED_DOMAINS:
            self.assertIn("." + domain, body)

    def test_the_stable_marker_survives_tokenisation(self):
        # is_enabled() and disable() recognise our own routing by finding this
        # substring in whatever AutoConfigURL holds; losing it from the URL
        # would leave the app unable to identify - or clean up - its own
        # routing.
        server = proxy_config.PacServer(1234)
        self.assertIn(proxy_config.PAC_FILENAME, server.url)
        self.assertNotEqual(server.path, "/" + proxy_config.PAC_FILENAME,
                            "the path must not be guessable")

    def test_two_servers_do_not_share_a_token(self):
        self.assertNotEqual(proxy_config.PacServer(1).path,
                            proxy_config.PacServer(1).path)


class TestPacServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = proxy_config.PacServer(9999, log=lambda _m: None)
        cls.server.start()
        deadline = time.time() + 10
        while time.time() < deadline and not (cls.server.alive or cls.server.bind_error):
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _get(self, path, host=None):
        import http.client

        # `host is None`, not `host or ...`: an empty Host is one of the cases
        # under test, and falsiness would silently replace it with the valid
        # one - a test that passes by never sending what it claims to send.
        if host is None:
            host = "127.0.0.1:%d" % self.server.port
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        try:
            conn.request("GET", path, headers={"Host": host})
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    def test_it_came_up_on_an_unpredictable_port(self):
        self.assertTrue(self.server.alive, self.server.bind_error)
        self.assertNotIn(self.server.port, (0, 8798))

    def test_the_right_url_is_served(self):
        status, body, headers = self._get(self.server.path)
        self.assertEqual(status, 200)
        self.assertIn(b"FindProxyForURL", body)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_a_guessable_url_is_not(self):
        status, _, _ = self._get("/" + proxy_config.PAC_FILENAME)
        self.assertEqual(status, 404)

    def test_a_foreign_host_header_is_refused(self):
        for host in ["evil.attacker.com", "localhost:%d" % self.server.port, ""]:
            with self.subTest(host=host):
                status, _, _ = self._get(self.server.path, host=host)
                self.assertEqual(status, 404)

    def test_self_check_agrees(self):
        self.assertTrue(self.server.serves_our_pac())

    def test_connection_ceiling_bounds_threads_and_does_not_leak(self):
        before = threading.active_count()
        held = []
        try:
            for _ in range(proxy_config.MAX_PAC_CONNECTIONS * 3):
                try:
                    held.append(socket.create_connection(
                        ("127.0.0.1", self.server.port), timeout=3))
                except OSError:
                    break
            time.sleep(1.5)
            self.assertLessEqual(
                threading.active_count() - before,
                proxy_config.MAX_PAC_CONNECTIONS + 2,
                "a local process can spawn unbounded threads")
        finally:
            for s in held:
                try:
                    s.close()
                except OSError:
                    pass
        time.sleep(1.0)
        self.assertTrue(self.server.serves_our_pac(), "slots leaked; server wedged")


# -- xpui_patch -------------------------------------------------------------

def _read(path):
    """Read a file whole and close it, so the suite emits no ResourceWarnings."""
    with open(path, "rb") as handle:
        return handle.read()


def _make_bundle(path, css=b"body{}", html=b"<html></html>"):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(xpui_patch.CSS_ENTRY, css)
        archive.writestr(xpui_patch.HTML_ENTRY, html)


import contextlib


@contextlib.contextmanager
def _redirected_bundle(tmp, css=b"body{original}", html=b"<html></html>"):
    """Point xpui_patch at a synthetic bundle in tmp instead of real Spotify.

    The paths are module-level names bound at import, so they are rebound here
    rather than patched at source.
    """
    paths = {
        "xpui": os.path.join(tmp, "xpui.spa"),
        "backup": os.path.join(tmp, "xpui.spa.orig"),
        "stamp": os.path.join(tmp, "xpui.spa.skipper-stamp"),
    }
    _make_bundle(paths["xpui"], css=css, html=html)
    saved = (xpui_patch.XPUI_PATH, xpui_patch.XPUI_BACKUP,
             xpui_patch.STAMP_PATH, xpui_patch.spotify_version)
    xpui_patch.XPUI_PATH = paths["xpui"]
    xpui_patch.XPUI_BACKUP = paths["backup"]
    xpui_patch.STAMP_PATH = paths["stamp"]
    xpui_patch.spotify_version = lambda: "1.2.95.453"
    try:
        yield paths
    finally:
        (xpui_patch.XPUI_PATH, xpui_patch.XPUI_BACKUP,
         xpui_patch.STAMP_PATH, xpui_patch.spotify_version) = saved


class TestXpuiRoundTrip(unittest.TestCase):
    """apply -> restore has to give back exactly what was there before."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="skipper-round-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_patch_then_restore_returns_the_original_bytes(self):
        with _redirected_bundle(self.tmp) as paths:
            before = _read(paths["xpui"])

            ok, message = xpui_patch.apply_patch()
            self.assertTrue(ok, message)
            self.assertTrue(xpui_patch.is_patched())
            self.assertNotEqual(_read(paths["xpui"]), before)

            ok, message = xpui_patch.restore()
            self.assertTrue(ok, message)
            self.assertFalse(xpui_patch.is_patched())
            with zipfile.ZipFile(paths["xpui"]) as archive:
                self.assertEqual(archive.read(xpui_patch.CSS_ENTRY), b"body{original}")

    def test_patching_twice_does_not_stack_the_block(self):
        with _redirected_bundle(self.tmp) as paths:
            self.assertTrue(xpui_patch.apply_patch()[0])
            once = _read(paths["xpui"])
            self.assertTrue(xpui_patch.apply_patch()[0])
            with zipfile.ZipFile(paths["xpui"]) as archive:
                css = archive.read(xpui_patch.CSS_ENTRY).decode()
            self.assertEqual(css.count(xpui_patch.START_MARKER), 1)
            self.assertLessEqual(
                abs(len(_read(paths["xpui"])) - len(once)), 64)

    def test_the_patch_keeps_spotifys_own_css(self):
        with _redirected_bundle(self.tmp, css=b".player{color:red}") as paths:
            self.assertTrue(xpui_patch.apply_patch()[0])
            with zipfile.ZipFile(paths["xpui"]) as archive:
                css = archive.read(xpui_patch.CSS_ENTRY).decode()
            self.assertIn(".player{color:red}", css)

    def test_every_entry_survives_the_patch(self):
        path = os.path.join(self.tmp, "xpui.spa")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(xpui_patch.CSS_ENTRY, b"body{}")
            archive.writestr(xpui_patch.HTML_ENTRY, b"<html></html>")
            archive.writestr("vendor~xpui.js", b"console.log(1)")
            archive.writestr("images/logo.png", b"\x89PNG\r\n")
        saved = (xpui_patch.XPUI_PATH, xpui_patch.XPUI_BACKUP,
                 xpui_patch.STAMP_PATH, xpui_patch.spotify_version)
        xpui_patch.XPUI_PATH = path
        xpui_patch.XPUI_BACKUP = path + ".orig"
        xpui_patch.STAMP_PATH = path + ".stamp"
        xpui_patch.spotify_version = lambda: "1.2.95.453"
        try:
            self.assertTrue(xpui_patch.apply_patch()[0])
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.read("vendor~xpui.js"), b"console.log(1)")
                self.assertEqual(archive.read("images/logo.png"), b"\x89PNG\r\n")
        finally:
            (xpui_patch.XPUI_PATH, xpui_patch.XPUI_BACKUP,
             xpui_patch.STAMP_PATH, xpui_patch.spotify_version) = saved

    def test_restore_refuses_a_damaged_backup(self):
        with _redirected_bundle(self.tmp) as paths:
            self.assertTrue(xpui_patch.apply_patch()[0])
            patched = _read(paths["xpui"])
            with open(paths["backup"], "wb") as handle:
                handle.write(b"this is not a zip at all")

            ok, message = xpui_patch.restore()
            self.assertFalse(ok)
            self.assertEqual(_read(paths["xpui"]), patched,
                             "a damaged backup was written over the working bundle")

    def test_restore_with_no_backup_is_a_no_op(self):
        with _redirected_bundle(self.tmp) as paths:
            before = _read(paths["xpui"])
            ok, _ = xpui_patch.restore()
            self.assertTrue(ok)
            self.assertEqual(_read(paths["xpui"]), before)


class TestXpuiInspection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="skipper-xpui-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_clean_bundle_is_intact_but_not_patched(self):
        p = os.path.join(self.tmp, "xpui.spa")
        _make_bundle(p)
        self.assertTrue(xpui_patch._is_intact(p))
        self.assertFalse(xpui_patch.is_patched(p))

    def test_a_patched_bundle_is_detected(self):
        p = os.path.join(self.tmp, "xpui.spa")
        _make_bundle(p, css=(xpui_patch.START_MARKER + "\n.x{}\n"
                             + xpui_patch.END_MARKER).encode())
        self.assertTrue(xpui_patch.is_patched(p))

    def test_a_missing_file_is_neither(self):
        p = os.path.join(self.tmp, "nope.spa")
        self.assertFalse(xpui_patch.is_patched(p))
        self.assertFalse(xpui_patch._is_intact(p))

    def test_a_non_zip_does_not_raise(self):
        p = os.path.join(self.tmp, "junk.spa")
        with open(p, "wb") as handle:
            handle.write(b"definitely not a zip")
        self.assertFalse(xpui_patch.is_patched(p))
        self.assertFalse(xpui_patch._is_intact(p))

    def test_a_bundle_without_the_css_entry_is_not_intact(self):
        p = os.path.join(self.tmp, "xpui.spa")
        with zipfile.ZipFile(p, "w") as archive:
            archive.writestr("something-else.txt", b"x")
        self.assertFalse(xpui_patch._is_intact(p))

    def test_an_unsupported_compression_method_is_not_intact(self):
        # zipfile raises RuntimeError, not BadZipFile, for a method it cannot
        # decompress - and the entry is still listed in the central directory,
        # so a check based on namelist() alone would call this bundle good and
        # let restore() write it over a working one.
        p = os.path.join(self.tmp, "weird.spa")
        _make_bundle(p, css=b"body{}" * 500)
        with open(p, "rb") as handle:
            data = bytearray(handle.read())
        # The CENTRAL directory record, not the local header: that is the one
        # zipfile honours, so patching the local header alone would leave the
        # entry perfectly readable and the test would prove nothing.
        i = data.find(b"PK\x01\x02")
        data[i + 10:i + 12] = (98).to_bytes(2, "little")  # PPMd
        with open(p, "wb") as handle:
            handle.write(bytes(data))
        self.assertIn(xpui_patch.CSS_ENTRY, zipfile.ZipFile(p).namelist())
        self.assertFalse(xpui_patch.is_patched(p))
        self.assertFalse(xpui_patch._is_intact(p))

    def test_a_backup_is_never_taken_from_an_already_patched_bundle(self):
        # The blank-screen failure: Spotify self-updates, the patch is
        # re-applied from a backup belonging to the previous version, and the
        # UI is rolled back under newer binaries.
        original = b"body{original}"
        with _redirected_bundle(self.tmp, css=original) as paths:
            ok, _ = xpui_patch.apply_patch()
            self.assertTrue(ok)
            self.assertTrue(xpui_patch.is_patched())

            first = _read(paths["backup"])
            xpui_patch._ensure_backup()
            self.assertEqual(_read(paths["backup"]), first,
                             "a patched bundle was enshrined as the original")

    def test_a_damaged_entry_is_not_intact(self):
        p = os.path.join(self.tmp, "cut.spa")
        _make_bundle(p, css=b"body{}" * 500)
        with open(p, "rb") as handle:
            data = bytearray(handle.read())
        # Central directory left alone; the compressed stream is mangled.
        i = data.find(b"PK\x03\x04")
        for k in range(i + 40, i + 90):
            data[k] ^= 0xFF
        with open(p, "wb") as handle:
            handle.write(bytes(data))
        self.assertIn(xpui_patch.CSS_ENTRY, zipfile.ZipFile(p).namelist())
        self.assertFalse(xpui_patch._is_intact(p))


# -- Spotify.py: config, consent and logging --------------------------------

class TestConsentAndConfig(unittest.TestCase):
    """The record that decides whether a root CA gets installed."""

    def setUp(self):
        import Spotify

        self.app = Spotify
        self.tmp = tempfile.mkdtemp(prefix="skipper-cfg-")
        self._orig = (Spotify.CONFIG_PATH, Spotify.LEGACY_CONFIG_PATH, Spotify.DATA_DIR)
        Spotify.CONFIG_PATH = os.path.join(self.tmp, "local", "settings.json")
        Spotify.LEGACY_CONFIG_PATH = os.path.join(self.tmp, "roaming", "settings.json")
        Spotify.DATA_DIR = os.path.join(self.tmp, "data")

    def tearDown(self):
        (self.app.CONFIG_PATH, self.app.LEGACY_CONFIG_PATH,
         self.app.DATA_DIR) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, path, payload):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload if isinstance(payload, str) else json.dumps(payload))

    def test_no_config_means_no_consent(self):
        self.assertFalse(self.app.has_consented())

    def test_explicit_answers_are_honoured(self):
        self._write(self.app.CONFIG_PATH, {"consented": True})
        self.assertTrue(self.app.has_consented())
        self._write(self.app.CONFIG_PATH, {"consented": False})
        self.assertFalse(self.app.has_consented())

    def test_the_legacy_key_counts_as_consent(self):
        self._write(self.app.CONFIG_PATH, {"seamless": True})
        self.assertTrue(self.app.has_consented())

    def test_an_explicit_no_beats_a_legacy_yes(self):
        self._write(self.app.CONFIG_PATH, {"seamless": True, "consented": False})
        self.assertFalse(self.app.has_consented())

    def test_a_corrupt_file_fails_closed(self):
        self._write(self.app.CONFIG_PATH, "{ not json")
        self.assertFalse(self.app.has_consented())

    def test_a_non_object_document_fails_closed(self):
        for payload in ["[1, 2, 3]", '"yes"', "true", "null"]:
            with self.subTest(payload=payload):
                self._write(self.app.CONFIG_PATH, payload)
                self.assertFalse(self.app.has_consented())

    def test_a_roaming_file_is_read_once_and_then_retired(self):
        self._write(self.app.LEGACY_CONFIG_PATH, {"consented": True})
        self.assertTrue(self.app.has_consented())
        self.assertTrue(self.app.record_consent(True))
        self.assertTrue(os.path.isfile(self.app.CONFIG_PATH))
        self.assertFalse(os.path.isfile(self.app.LEGACY_CONFIG_PATH),
                         "consent must not follow the user to another machine")

    def test_recording_consent_reports_whether_it_landed(self):
        self.assertTrue(self.app.record_consent(True))
        # A plain file where the settings directory should be: makedirs fails,
        # and record_consent has to say so. Reporting success here would let
        # "Remove local certificate" delete a certificate while the file still
        # says the user consented, so the next launch installs a fresh one.
        blocker = os.path.join(self.tmp, "blocked")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("not a directory")
        self.app.CONFIG_PATH = os.path.join(blocker, "settings.json")
        self.assertFalse(self.app.record_consent(False))

    def test_recording_drops_the_superseded_key(self):
        self._write(self.app.CONFIG_PATH, {"seamless": True})
        self.app.record_consent(True)
        with open(self.app.CONFIG_PATH, encoding="utf-8") as handle:
            self.assertNotIn("seamless", json.load(handle))


class TestLogging(unittest.TestCase):
    def setUp(self):
        import Spotify

        self.app = Spotify
        self.tmp = tempfile.mkdtemp(prefix="skipper-log-")
        self._orig = Spotify.DATA_DIR
        Spotify.DATA_DIR = self.tmp
        self.path = os.path.join(self.tmp, "debug_log.txt")

    def tearDown(self):
        self.app.DATA_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_redact_replaces_the_profile_path(self):
        home = os.environ.get("USERPROFILE")
        if not home:
            self.skipTest("no USERPROFILE")
        self.assertIn("%USERPROFILE%", self.app.redact(home + r"\thing"))

    def test_redact_leaves_a_multi_line_report_alone(self):
        # The self-test report goes through redact() and is many lines by
        # design; escaping newlines here rendered it as literal "\x0a".
        report = "line one\nline two\n  indented"
        self.assertEqual(self.app.redact(report), report)

    def test_a_log_entry_cannot_span_lines(self):
        self.app.log_debug("injected\r\n[2026-01-01 00:00:00] FORGED")
        with open(self.path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertNotIn("FORGED", lines[0].split("] ", 1)[0])

    def test_a_nul_never_reaches_the_file(self):
        self.app.log_debug("host 1.1.1.1\x00.spotify.com")
        with open(self.path, "rb") as handle:
            self.assertNotIn(b"\x00", handle.read())

    def test_tabs_survive(self):
        self.app.log_debug("a\tb")
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn("\t", handle.read())

    def test_concurrent_writers_do_not_interleave(self):
        def worker(i):
            for j in range(60):
                self.app.log_debug("thread %d entry %d" % (i, j))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(self.path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(len(lines), 12 * 60)
        self.assertTrue(all(l.startswith("[2") for l in lines))

    def test_the_log_is_rotated_rather_than_growing_without_bound(self):
        self.app.log_debug("x" * 100)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("y" * (self.app.LOG_MAX_BYTES + 10))
        self.app.log_debug("after rotation")
        self.assertTrue(os.path.isfile(self.path + ".1"))
        with open(self.path, encoding="utf-8") as handle:
            self.assertIn("after rotation", handle.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
