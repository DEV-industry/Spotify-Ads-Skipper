"""Certificate authority for the ad-filtering proxy.

The proxy has to terminate TLS to see request paths, which means presenting
certificates the Spotify client will accept - so a local CA has to exist and be
trusted by the machine.

Four rules govern this file, all load-bearing:

1. The CA is generated per installation and never shipped. A CA baked into the
   executable would put the same private key on every user's disk, and anyone
   who downloaded the app could impersonate any HTTPS site for every other
   user. Generation happens on first run, locally, and the key never leaves.

2. The CA is limited by name AND by purpose, which together keep the worst case
   small: even with the private key in hand, an attacker can only forge
   WEBSITE certificates, and only for the three domains below - not for a bank
   or anything else the user browses.

   Both halves are needed, and the first alone was not enough. RFC 5280
   constrains only the name FORMS that appear in permittedSubtrees, so DNS
   entries by themselves left an iPAddress SAN unconstrained: a leaf for IP
   1.1.1.1 validated clean against Windows' chain engine. And name constraints
   say nothing about purpose, so leaves for code signing and S/MIME validated
   clean too, needing no network position at all. Hence the IP exclusions and
   the serverAuth EKU in generate_ca(), both measured against Windows
   CryptoAPI and OpenSSL.

   is_constrained() must list every one of those limits: load_ca() reuses any
   CA it approves, so a limit missing from that check is a limit no existing
   installation ever receives.

3. The key is encrypted at rest with DPAPI, tied to this Windows account, and
   written with an ACL restricted to that account. A copy of the file taken to
   another machine - via a backup, a synced folder, a lifted disk - is inert.
   The exception is a machine where DPAPI itself fails: the key is then stored
   in the clear under KEY_PLAIN_MAGIC, key_is_sealed goes False, and the self
   test says so, because on that machine this paragraph is not true.

4. Trust is decided by comparing the stored certificate byte for byte with what
   is in the root store. Matching on the name instead would accept a leftover
   CA from an earlier install, whose private key we no longer hold, and the
   proxy would then fail every handshake while reporting itself healthy.

Leaf certificates are minted on demand per hostname and cached in memory only.
"""

import ctypes
import datetime
import ipaddress
import os
import subprocess
from ctypes import wintypes

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# Deliberately under LOCALAPPDATA rather than APPDATA: roaming profiles copy
# APPDATA to a network share at logoff, and plenty of backup tools sweep it.
# A signing key for a trusted root has no business travelling.
#
# The fallback chain is not decoration. os.path.join("", "SpotifyAdsSkipper")
# yields a RELATIVE path, so an empty LOCALAPPDATA would drop the CA private
# key into whatever directory the process happened to start in.
_BASE = (os.environ.get("LOCALAPPDATA")
         or os.environ.get("APPDATA")
         or os.path.expanduser("~"))
CA_DIR = os.path.join(_BASE, "SpotifyAdsSkipper", "ca")
CA_CERT = os.path.join(CA_DIR, "skipper-ca.pem")
CA_KEY = os.path.join(CA_DIR, "skipper-ca.key")

# Where releases before the move kept it, cleaned up on first run.
_ROAMING = os.environ.get("APPDATA", "")
LEGACY_CA_DIR = (os.path.join(_ROAMING, "SpotifyAdsSkipper", "ca")
                 if _ROAMING else "")

CA_COMMON_NAME = "Spotify Ads Skipper Local CA"
CA_VALID_DAYS = 825  # Matches the CA/Browser Forum cap browsers enforce.
LEAF_VALID_DAYS = 30

# The only domains this CA may ever vouch for. Kept in step with
# ad_proxy.SPOTIFY_SUFFIXES; a DNS constraint covers the domain and everything
# beneath it, so "spotify.com" also permits gew4-spclient.spotify.com.
PERMITTED_DOMAINS = ("spotify.com", "scdn.co", "spotifycdn.com")

# Marks a key file as DPAPI-wrapped. Files without it are plaintext PEM left by
# an older version and get re-wrapped on first load.
KEY_MAGIC = b"SASKEY1\n"

# Marks a key file that DPAPI refused to seal. A bare PEM used to be written
# instead, which is indistinguishable from the pre-magic format and therefore
# invisible: nothing could tell "old file, re-wrap it" from "this machine
# cannot encrypt keys and the guarantee in the README does not hold here".
KEY_PLAIN_MAGIC = b"SASPLAIN1\n"

# Mixed into the DPAPI ciphertext so a blob from this app cannot be handed
# straight to CryptUnprotectData by an unrelated process that happens to run as
# the same user. Not a secret, and not meant to be one.
DPAPI_ENTROPY = b"SpotifyAdsSkipper/ca-key/v1"

CREATE_NO_WINDOW = 0x08000000

# Absolute paths, because CreateProcess searches the executable's own directory
# first and this app installs per-user into %LOCALAPPDATA%\Programs - which the
# user, and anything running as them, can write. A planted certutil.exe cannot
# fake the outcome (is_trusted() reads the store directly), but it would run.
_SYSTEM32 = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"), "System32",
)
CERTUTIL = os.path.join(_SYSTEM32, "certutil.exe")
ICACLS = os.path.join(_SYSTEM32, "icacls.exe")

# Set False when the key file's ACL could not be narrowed to this account. The
# key is still DPAPI-encrypted, so this is not fatal - but the self test reports
# it rather than letting the module quietly promise more than it delivered.
key_permissions_ok = True

# False when the key on disk is not DPAPI-sealed, so the self test can say so.
# Unlike the ACL, this one changes what is true of a copy of the file taken off
# the machine, which is the strongest claim the project makes about the key.
key_is_sealed = True

# certutil is unhurried - measured at 13 seconds for a single addstore on an
# ordinary desktop, and it consults the network on the way. The old 30 second
# limit was close enough to that to fail for no reason on a busy machine, and a
# timeout here reads to the user as "the app is broken".
CERTUTIL_TIMEOUT = 120

crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


# -- Windows plumbing -------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class _CertContext(ctypes.Structure):
    _fields_ = [
        ("dwCertEncodingType", wintypes.DWORD),
        ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbCertEncoded", wintypes.DWORD),
        ("pCertInfo", ctypes.c_void_p),
        ("hCertStore", ctypes.c_void_p),
    ]


_PCertContext = ctypes.POINTER(_CertContext)

crypt32.CryptProtectData.restype = wintypes.BOOL
crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob),
]
crypt32.CryptUnprotectData.restype = wintypes.BOOL
crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob),
]
crypt32.CertOpenStore.restype = ctypes.c_void_p
crypt32.CertOpenStore.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
]
crypt32.CertEnumCertificatesInStore.restype = _PCertContext
crypt32.CertEnumCertificatesInStore.argtypes = [ctypes.c_void_p, _PCertContext]
crypt32.CertCloseStore.restype = wintypes.BOOL
crypt32.CertCloseStore.argtypes = [ctypes.c_void_p, wintypes.DWORD]
kernel32.LocalFree.restype = ctypes.c_void_p
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
# Declared explicitly: left to ctypes' defaults these return c_int, which
# truncates a 64-bit HANDLE and makes every call fail for no visible reason.
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetCurrentProcess.argtypes = []
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
]
advapi32.GetTokenInformation.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
advapi32.ConvertSidToStringSidW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p),
]

_ENCODING = 0x00000001 | 0x00010000  # X509_ASN | PKCS_7_ASN
_STORE_PROV_SYSTEM_W = 10
_STORE_CURRENT_USER = 1 << 16
_STORE_READONLY = 0x00008000
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _to_blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf


def _from_blob(blob):
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(blob.pbData)


def _dpapi_protect(data):
    """Encrypt to this Windows account. Returns None if DPAPI is unavailable."""
    src, _keep = _to_blob(data)
    entropy, _keep2 = _to_blob(DPAPI_ENTROPY)
    out = _Blob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(src), "Spotify Ads Skipper CA key", ctypes.byref(entropy),
        None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        return None
    return _from_blob(out)


def _dpapi_unprotect(blob_bytes):
    """Decrypt a blob written by this account. Returns None if it cannot be."""
    src, _keep = _to_blob(blob_bytes)
    entropy, _keep2 = _to_blob(DPAPI_ENTROPY)
    out = _Blob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(src), None, ctypes.byref(entropy),
        None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        return None
    return _from_blob(out)


def _root_store_certs():
    """Every certificate in the current user's Root store, DER encoded.

    Read-only, so the worst a failure here can do is make is_trusted() say no
    and have the caller install the CA again - which is harmless.
    """
    store = crypt32.CertOpenStore(
        ctypes.c_void_p(_STORE_PROV_SYSTEM_W), _ENCODING, None,
        _STORE_CURRENT_USER | _STORE_READONLY, ctypes.c_wchar_p("ROOT"),
    )
    if not store:
        return []
    found = []
    try:
        ctx = crypt32.CertEnumCertificatesInStore(store, None)
        while ctx:
            found.append(ctypes.string_at(ctx.contents.pbCertEncoded,
                                          ctx.contents.cbCertEncoded))
            ctx = crypt32.CertEnumCertificatesInStore(store, ctx)
    finally:
        crypt32.CertCloseStore(store, 0)
    return found


# -- key storage ------------------------------------------------------------

def _current_user_sid():
    """SID of the account this process runs as, as a string, or None."""
    TOKEN_QUERY = 0x0008
    TOKEN_USER = 1
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                     TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_USER, buf,
                                            size.value, ctypes.byref(size)):
            return None
        # TOKEN_USER opens with a SID_AND_ATTRIBUTES whose first member is the
        # PSID, so the pointer sits at offset zero.
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents
        text = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(psid, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def _lock_down(path):
    """Restrict a file to the account this process runs as. Returns success.

    The principal comes from the process token, never from %USERNAME%. That
    environment variable is attacker-controlled - anyone able to launch this
    app with a crafted environment could set it to "*S-1-1-0", which icacls
    accepts happily and which grants the CA key to Everyone.
    """
    sid = _current_user_sid()
    if not sid:
        return False
    try:
        result = subprocess.run(
            [
                ICACLS, path,
                # Drops ACEs the file inherited from its parent...
                "/inheritance:r",
                # ...replaces whatever this account had with exactly full
                # control...
                "/grant:r", "*%s:F" % sid,
                # ...and removes the two principals Windows puts on files
                # directly. /inheritance:r does not touch those, because they
                # are explicit rather than inherited, so without these the
                # command reports success and changes nothing at all on a file
                # that already has them.
                "/remove:g", "*S-1-5-32-544",  # BUILTIN\Administrators
                "/remove:g", "*S-1-5-18",      # NT AUTHORITY\SYSTEM
            ],
            capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return False
    # DPAPI is the real protection for the key contents; this narrows who can
    # read the file at all, so a failure is worth reporting but not fatal.
    return result.returncode == 0


def _write_private(path, data):
    """Create a file readable only by this user, then fill it."""
    global key_permissions_ok

    # O_BINARY is not optional: without it Windows opens the descriptor in text
    # mode and rewrites every 0x0A byte as 0x0D 0x0A, which quietly corrupts
    # the DPAPI blob and makes the key unrecoverable on the next start.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    handle = os.open(path, flags, 0o600)
    try:
        os.write(handle, data)
    finally:
        os.close(handle)
    if not _lock_down(path) and path == CA_KEY:
        key_permissions_ok = False


def _store_key(key):
    """Write the CA key, DPAPI-wrapped when Windows lets us."""
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    global key_is_sealed

    try:
        sealed = _dpapi_protect(pem)
    except Exception:
        sealed = None

    # Falling back to plaintext keeps the app working on a machine where DPAPI
    # is broken; the ACL and the name constraints still apply. But it quietly
    # voids the promise that a copy of this file is useless on another machine
    # - it is the one route by which the key can leave without code execution
    # here - so the state is recorded rather than inferred, given its own magic
    # so it is distinguishable on sight, and reported by the self test.
    key_is_sealed = sealed is not None
    _write_private(CA_KEY, (KEY_MAGIC + sealed) if sealed else (KEY_PLAIN_MAGIC + pem))


def _load_key():
    """Read the CA key back. Returns None when it cannot be recovered."""
    global key_is_sealed

    try:
        with open(CA_KEY, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None

    if raw.startswith(KEY_MAGIC):
        try:
            pem = _dpapi_unprotect(raw[len(KEY_MAGIC):])
        except Exception:
            pem = None
        if pem is None:
            # Written by a different Windows account, or the profile's master
            # key is gone. The CA is unusable; the caller regenerates.
            return None
        key_is_sealed = True
    elif raw.startswith(KEY_PLAIN_MAGIC):
        pem = raw[len(KEY_PLAIN_MAGIC):]
        key_is_sealed = False
    else:
        pem = raw  # Plaintext key from a version before either magic existed.
        key_is_sealed = False

    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except Exception:
        return None

    if not raw.startswith(KEY_MAGIC):
        # Re-seal whenever DPAPI is working again. _store_key sets
        # key_is_sealed to whatever it actually achieved, so a machine where
        # DPAPI is still broken stays honestly marked as unsealed.
        try:
            _store_key(key)
        except OSError:
            pass
    return key


# -- CA ---------------------------------------------------------------------

def ca_exists():
    return os.path.isfile(CA_CERT) and os.path.isfile(CA_KEY)


def is_constrained(cert):
    """True when this CA carries every limit the current version applies.

    Deliberately an equality test against today's full set, not a "looks
    roughly right" check. load_ca() reuses any CA this returns True for, so
    every limit added later has to be listed here or existing installs keep
    the weaker CA they already have, forever, and never see the fix. That is
    exactly what happened when the DNS permitted set was the only thing
    checked: a CA with no purpose limit and no IP exclusions passed.
    """
    try:
        ext = cert.extensions.get_extension_for_class(x509.NameConstraints)
    except x509.ExtensionNotFound:
        return False

    permitted = ext.value.permitted_subtrees or []
    names = {n.value.lower().lstrip(".") for n in permitted
             if isinstance(n, x509.DNSName)}
    if names != set(PERMITTED_DOMAINS):
        return False

    # Both families, or an IPv6 literal walks through the v4-only exclusion.
    excluded = {n.value for n in (ext.value.excluded_subtrees or [])
                if isinstance(n, x509.IPAddress)}
    if excluded != {ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0")}:
        return False

    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound:
        return False
    return set(eku.value) == {ExtendedKeyUsageOID.SERVER_AUTH}


def _purge_legacy_dir():
    """Delete the pre-move CA directory in the roaming profile."""
    if not LEGACY_CA_DIR or not os.path.isdir(LEGACY_CA_DIR):
        return
    # On a profile with no LOCALAPPDATA the fallback chain lands CA_DIR on the
    # roaming path - the same directory. Purging it there would delete the CA
    # this call is in the middle of creating.
    try:
        if os.path.normcase(os.path.abspath(LEGACY_CA_DIR)) == \
           os.path.normcase(os.path.abspath(CA_DIR)):
            return
    except OSError:
        return
    for name in ("skipper-ca.key", "skipper-ca.pem"):
        try:
            path = os.path.join(LEGACY_CA_DIR, name)
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
    try:
        os.rmdir(LEGACY_CA_DIR)
    except OSError:
        pass


def generate_ca():
    """Create this installation's CA. Returns (cert, key)."""
    os.makedirs(CA_DIR, exist_ok=True)
    # Lock the directory before any key material lands in it, so the file is
    # never briefly readable under inherited permissions.
    _lock_down(CA_DIR)
    _purge_legacy_dir()

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Spotify Ads Skipper"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=CA_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        # The blast-radius limit. Critical, as RFC 5280 requires: a verifier
        # that cannot enforce this must reject the CA outright rather than
        # quietly treat it as unrestricted.
        #
        # The IP exclusions are not decoration. RFC 5280 constrains only the
        # name FORMS that appear in permittedSubtrees, so with DNS entries
        # alone an iPAddress SAN was entirely unconstrained - and make_leaf()
        # below emits exactly that for an IP literal. Measured against Windows'
        # own chain engine: a leaf for IP 1.1.1.1 validated clean, and with
        # these two lines it returns HAS_EXCLUDED_NAME_CONSTRAINT instead.
        #
        # Excluding directoryName is NOT possible here, tempting as it looks:
        # an empty DN is an initial subsequence of every DN, so it would
        # reject our own leaves and break interception entirely.
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=[x509.DNSName(d) for d in PERMITTED_DOMAINS],
                excluded_subtrees=[
                    x509.IPAddress(ipaddress.ip_network("0.0.0.0/0")),
                    x509.IPAddress(ipaddress.ip_network("::/0")),
                ],
            ),
            critical=True,
        )
        # Purpose, as opposed to name. Without this the CA is trusted for every
        # purpose there is, and name constraints say nothing about most of
        # them: leaves for code signing (subject "Microsoft Corporation") and
        # for S/MIME (rfc822 anyone@anywhere) both validated clean, needing no
        # network position at all. Constraining the CA to server
        # authentication makes all of those NOT_VALID_FOR_USAGE, because both
        # CryptoAPI and OpenSSL nest EKU down the chain.
        #
        # Non-critical deliberately: a critical EKU on a trust anchor is
        # handled inconsistently by verifiers, and the nesting works either way.
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )

    _store_key(key)
    with open(CA_CERT, "wb") as handle:
        handle.write(cert.public_bytes(serialization.Encoding.PEM))

    return cert, key


def load_ca():
    """Load this installation's CA, creating it when it is missing or unusable.

    An existing CA is replaced rather than reused when it predates name
    constraining or its key cannot be decrypted - in both cases keeping it
    would mean running with a CA that is either too broadly trusted or unable
    to sign at all.
    """
    if not ca_exists():
        return generate_ca()

    try:
        with open(CA_CERT, "rb") as handle:
            cert = x509.load_pem_x509_certificate(handle.read())
    except (OSError, ValueError):
        cert = None

    key = _load_key() if cert is not None else None

    # Expiry is checked here because nothing else would notice. ca_exists(),
    # is_constrained() and is_trusted() all stay True past not_valid_after, so
    # a CA would be reused forever - and the failure is not graceful: CONNECT
    # succeeds and the 200 goes out before the client-side handshake fails, so
    # Spotify gets a certificate error rather than falling back to a direct
    # connection.
    expired = cert is not None and cert.not_valid_after_utc <= datetime.datetime.now(
        datetime.timezone.utc
    )

    if cert is None or key is None or expired or not is_constrained(cert):
        # Drop the old CA from the trust store first: it shares our common
        # name, so leaving it there would mean two CAs claiming to be us.
        untrust()
        return generate_ca()

    # Re-assert the ACL on every load rather than only when the key is written.
    # A key created by a version whose lock-down did not work - or by a run
    # where it failed - would otherwise keep its loose permissions for as long
    # as the CA lives, since nothing rewrites the file once it is valid.
    global key_permissions_ok
    key_permissions_ok = _lock_down(CA_KEY)

    return cert, key


def make_leaf(hostname, ca_cert, ca_key):
    """Mint a short-lived certificate for one hostname. Returns (cert, key)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)

    try:
        alt = x509.IPAddress(ipaddress.ip_address(hostname))
    except ValueError:
        alt = x509.DNSName(hostname)

    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname[:64])]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=LEAF_VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName([alt]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        # Lets a verifier tie this leaf to our CA by key rather than by name,
        # which matters while an older CA of the same name is still around.
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return cert, key


# -- trust store ------------------------------------------------------------

def _our_cert_der():
    try:
        with open(CA_CERT, "rb") as handle:
            cert = x509.load_pem_x509_certificate(handle.read())
    except (OSError, ValueError):
        return None
    return cert.public_bytes(serialization.Encoding.DER)


def is_trusted():
    """True when exactly this installation's CA sits in the user's root store.

    Compares the certificate itself, not its name. A same-named CA left behind
    by an earlier install carries a different key, so treating it as ours would
    leave every handshake failing with no indication of why.
    """
    der = _our_cert_der()
    if der is None:
        return False
    return der in _root_store_certs()


def _foreign_namesakes():
    """Trusted CAs using our common name that are not the CA we hold."""
    ours = _our_cert_der()
    hits = []
    for blob in _root_store_certs():
        if blob == ours:
            continue
        try:
            cert = x509.load_der_x509_certificate(blob)
            cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        except Exception:
            continue
        if cn and cn[0].value == CA_COMMON_NAME:
            hits.append(blob)
    return hits


def trust():
    """Add the CA to the user's root store. Returns (ok, message)."""
    if not os.path.isfile(CA_CERT):
        return False, "No CA generated yet."

    # Clear out namesakes first. certutil deletes by name, so this has to run
    # before ours goes in or it would take ours with it.
    if _foreign_namesakes():
        try:
            subprocess.run(
                [CERTUTIL, "-user", "-delstore", "Root", CA_COMMON_NAME],
                capture_output=True, timeout=CERTUTIL_TIMEOUT, creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            pass

    try:
        result = subprocess.run(
            [CERTUTIL, "-user", "-addstore", "-f", "Root", CA_CERT],
            capture_output=True, timeout=CERTUTIL_TIMEOUT, creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:
        return False, "certutil failed (%s)" % exc
    if result.returncode != 0:
        return False, "certutil returned %d" % result.returncode

    # certutil reports success even where the store rejected the certificate,
    # so confirm against the store rather than trusting the exit code.
    if not is_trusted():
        return False, "CA was not accepted into the certificate store."
    return True, "Local CA trusted (limited to Spotify domains)."


def untrust():
    """Remove the CA from the store and delete the key material.

    The certificate file is kept when the store entry could not be removed. It
    is the only thing that identifies our entry, so deleting it after a failed
    removal would strand a trusted root that nothing can find again - and the
    old code did exactly that while reporting success.
    """
    had_cert = os.path.isfile(CA_CERT)

    try:
        subprocess.run(
            [CERTUTIL, "-user", "-delstore", "Root", CA_COMMON_NAME],
            capture_output=True, timeout=CERTUTIL_TIMEOUT, creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        pass

    # Drop the key first and unconditionally: once it is gone nothing can sign
    # with this CA, whatever happened in the trust store.
    try:
        if os.path.isfile(CA_KEY):
            os.remove(CA_KEY)
    except OSError:
        pass

    # is_trusted() compares exact DER, so it needs the PEM to know what to look
    # for. When the PEM is already gone - a previous failed removal, or the
    # uninstaller having deleted the data directory - fall back to matching our
    # common name, or this returns "removed" having consulted nothing at all.
    if had_cert:
        still_trusted = is_trusted()
    else:
        still_trusted = bool(_foreign_namesakes())

    if still_trusted:
        return False, (
            "The local CA could not be removed from the certificate store. "
            "Open certmgr.msc, go to Trusted Root Certification Authorities "
            "and delete '%s'." % CA_COMMON_NAME
        )

    try:
        if os.path.isfile(CA_CERT):
            os.remove(CA_CERT)
    except OSError:
        pass
    _purge_legacy_dir()

    return True, "Local CA removed."
