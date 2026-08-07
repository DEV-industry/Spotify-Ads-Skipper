# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['Spotify.py'],
    pathex=[],
    binaries=[],
    datas=[('cat.ico', '.')],
    # psutil and the pywin32 modules are reached only through lazy imports, so
    # PyInstaller's static scan never sees them.
    hiddenimports=[
        'psutil',
        'win32gui',
        'win32process',
        'win32api',
        # pywin32 imports this at run time and PyInstaller never sees it.
        'win32timezone',
        # Local CA generation and leaf signing.
        'cryptography',
        'cryptography.x509',
        'cryptography.hazmat.primitives.serialization',
        'cryptography.hazmat.primitives.asymmetric.rsa',
        # Trust anchors for the proxy's upstream connections. Without this the
        # bundle falls back to the Windows store, which holds our own CA.
        'certifi',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Spotify-Ads-Skipper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX packing is one of the strongest antivirus heuristics there is, and
    # this binary already behaves the way a scanner dislikes: windowless, edits
    # another app's files, runs a TLS proxy, installs a root certificate.
    # The few megabytes saved are not worth a false positive on release day.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['cat.ico'],
    version='version_info.txt',
)
