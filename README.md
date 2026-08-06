<div align="center">
  <img src="photos/cat.png" alt="Spotify Ads Skipper logo" width="120" height="120" />

  # Spotify Ads Skipper

  **Display ads removed. Audio ads muted - or dropped entirely, gapless.**

  <p>
    <img src="https://komarev.com/ghpvc/?username=DEV-industry-Spotify-Ads-Skipper&label=VIEWS&style=for-the-badge&color=green" alt="Views" />
    <img src="https://img.shields.io/github/v/release/DEV-industry/Spotify-Ads-Skipper?style=for-the-badge&color=1ED760&label=RELEASE" alt="Latest release" />
    <img src="https://img.shields.io/github/downloads/DEV-industry/Spotify-Ads-Skipper/total?style=for-the-badge&color=1ED760&label=DOWNLOADS" alt="Downloads" />
    <img src="https://img.shields.io/github/stars/DEV-industry/Spotify-Ads-Skipper?style=for-the-badge&color=1ED760&logo=github&label=STARS" alt="Stars" />
    <img src="https://img.shields.io/github/license/DEV-industry/Spotify-Ads-Skipper?style=for-the-badge&color=1ED760&label=LICENSE" alt="License" />
  </p>

  <p>
    <img src="https://img.shields.io/badge/Spotify-1ED760?style=for-the-badge&logo=spotify&logoColor=white" alt="Spotify" />
    <img src="https://img.shields.io/badge/Python-3.x-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
    <img src="https://img.shields.io/badge/Platform-Windows-0078D6?style=for-the-badge&logo=windows&logoColor=white" alt="Platform" />
    <img src="https://img.shields.io/github/last-commit/DEV-industry/Spotify-Ads-Skipper?style=for-the-badge&color=555555&label=UPDATED" alt="Last commit" />
  </p>

  <a href="https://github.com/DEV-industry/Spotify-Ads-Skipper/releases/latest">
    <img src="https://img.shields.io/badge/DOWNLOAD%20THE%20INSTALLER-1ED760?style=for-the-badge&logo=windows&logoColor=black&labelColor=1ED760" alt="Download the installer" height="42" />
  </a>
  &nbsp;
  <a href="https://spotify-skipper-web.vercel.app/">
    <img src="https://img.shields.io/badge/VISIT%20THE%20WEBSITE-191414?style=for-the-badge&logo=vercel&logoColor=1ED760" alt="Visit the website" height="42" />
  </a>
</div>

---

## Contents

- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [Seamless mode](#seamless-mode)
- [Risks, stated plainly](#risks-stated-plainly)
- [Installation](#installation)
- [Project structure](#project-structure)
- [FAQ](#faq)
- [Disclaimer](#disclaimer)

---

## What it does

A tray utility for Windows that removes Spotify's advertising. Spotify delivers
its ads through three different paths, so there are three mechanisms:

| Ad type | Default | Seamless mode |
| :-- | :-- | :-- |
| **Display** - home banners, takeovers, video overlays | Removed | Removed |
| **Audio** - the spoken ads between tracks | Muted, still runs its length | **Never plays.** Next track starts immediately |

The default install needs no administrator rights. It patches Spotify's UI
bundle in `%APPDATA%\Spotify` and keeps its own settings and log in
`%LOCALAPPDATA%\SpotifyAdsSkipper`, and touches nothing else. Seamless mode is
opt-in and asks first, because it needs a local certificate authority - see
below.

---

## How it works

**Display ads** live in `xpui.spa`, the client's UI bundle. The app appends a
CSS block hiding every ad container by its `data-testid`. Those attributes are
what Spotify's own tests select on, so they outlast the minified class names
that change with each build. If one is renamed the rule stops matching - the ad
reappears, nothing breaks.

**Audio ads** are fetched by the client's native core from a handful of paths on
the same host as everything else:

```
POST /ads/v3/ads?slots=preroll     <- the ad fetch
POST /ad-logic/prefetch
GET  /ads/v2/config
     /sponsoredplaylist/v1/sponsored
```

Playback travels entirely different paths - `/metadata/`, `/playplay/`,
`/playlist/`, `/widevine-license/`. Seamless mode runs a local proxy that
answers the ad paths with a 404 and forwards everything else untouched. The
client treats the empty slot as "no ad" and moves straight to the next track.

Without seamless mode the app instead watches playback and mutes Spotify for the
ad's duration.

**Staying patched.** Spotify replaces `xpui.spa` wholesale when it updates. The
app records the version it patched and re-applies after an update.

---

## Seamless mode

This is the mode that makes ads vanish rather than fall silent. It is off by
default and shows an explanation before enabling.

To read request paths inside Spotify's HTTPS traffic, the proxy has to terminate
TLS, which means presenting certificates the client trusts. So:

- **A certificate authority is generated on your machine, on first use.** It is
  never shipped in the installer. This matters: a CA baked into the executable
  would put the same private key on every user's disk, and anyone who downloaded
  the app could impersonate any HTTPS site for everyone else who installed it.
- **The CA can only vouch for Spotify.** It carries a critical `NameConstraints`
  extension permitting `spotify.com`, `scdn.co` and `spotifycdn.com` and nothing
  else. A certificate it signs for any other domain is rejected by the operating
  system before it ever reaches an application - so even someone holding the
  private key cannot use it against your bank or your email. Verified against
  both Windows CryptoAPI (`HasNotPermittedNameConstraint`) and OpenSSL
  (`permitted subtree violation`).
- **The private key is encrypted at rest** with DPAPI, tied to your Windows
  account, and written with an ACL restricted to it. Copied to another machine
  or another account, the file is useless.
- **Only Spotify's domains are intercepted.** A PAC file routes
  `*.spotify.com`, `*.scdn.co` and `*.spotifycdn.com` to the proxy and returns
  `DIRECT` for everything else. The proxy refuses to connect anywhere else at
  all, so it cannot be used as a relay by anything else on the machine.
- **A dead proxy falls back to DIRECT.** If the app is killed rather than closed,
  the PAC entry outlives it - the fallback means Spotify connects straight out
  and ads return, instead of losing connectivity. The next start clears it.
- **Turning the mode off removes the CA and the routing**, and so does
  uninstalling. If either ever fails, the app says so rather than reporting
  success, and **Remove local certificate** stays in the tray menu for as long
  as a certificate is installed.

---

## Risks, stated plainly

Read this before enabling seamless mode or handing the app to someone else.

- **A root CA is a sensitive thing to install.** The name constraints mean it
  can only ever vouch for Spotify's three domains, and the key is encrypted to
  your account - but it is still a signing key sitting on your disk. If that
  trade is not one you want, leave seamless mode off; muting needs none of it.
- **While the mode is on, traffic to Spotify's domains is decrypted on your
  machine.** That is the whole mechanism. It covers anything on this machine
  talking to those domains, a browser tab on `open.spotify.com` included, so
  your Spotify login passes through the local proxy. Nothing is written to disk
  and nothing leaves the machine, but it is worth knowing before you enable it.
- **The certificate stays installed until you remove it.** Closing the app stops
  the proxy and the routing, but deliberately leaves the certificate in place so
  the mode can come back up next time. Turning seamless mode off, using **Remove
  local certificate**, or uninstalling all remove it.
- **Seamless mode is detectable by Spotify.** It does not merely block traffic on
  your own machine; it answers Spotify's API requests on their behalf, and the
  client will keep re-requesting an ad it never receives. That is a visible
  pattern server-side. No account action is known to follow from it, but the
  risk is not zero, and it is a deeper intervention than the hosts-file blocking
  earlier versions used.
- **Patching the client breaks Spotify's Terms of Service.** Both modes do this.
- **Ad endpoints can move.** They are paths, not domains, and Spotify can rename
  them. When that happens ads come back until the list is updated.

---

## Installation

### Method 1: the installer

1. Download `SpotifyAdsSkipper_Setup.exe` from the [latest release](https://github.com/DEV-industry/Spotify-Ads-Skipper/releases/latest).
2. Run it. No administrator prompt - it installs per-user.
3. Launch it. Spotify restarts once to pick up the patch, then it sits in the tray.
4. Optional: right-click the tray icon and enable **Seamless mode**.

<details>
<summary><b>Method 2: run from source</b></summary>

<br />

```bash
git clone https://github.com/DEV-industry/Spotify-Ads-Skipper.git
cd Spotify-Ads-Skipper
pip install pystray Pillow pycaw comtypes psutil pywin32 cryptography certifi
python SpotifyAdRemover/Spotify.py
```

Undo everything without opening the app:

```bash
python SpotifyAdRemover/Spotify.py --cleanup
```

Build with `pyinstaller SpotifyAdRemover/Spotify.spec`.

</details>

---

## Project structure

```text
SpotifyAdRemover/
├── Spotify.py        # Tray app, orchestrates all three mechanisms
├── spotify_env.py    # Locates the Spotify install, inspects its state
├── xpui_patch.py     # Backs up / patches / restores xpui.spa
├── ad_watch.py       # Audio-ad detection and muting (default mode)
├── ad_proxy.py       # Path-filtering HTTPS proxy (seamless mode)
├── proxy_ca.py       # Per-installation certificate authority
├── proxy_config.py   # PAC file and Windows proxy routing
└── Spotify.spec      # PyInstaller build spec
```

---

## FAQ

<details>
<summary><b>Version 2 used the hosts file. Why did that stop working?</b></summary>

<br />

Two changes on Spotify's side, either one fatal:

1. **Endpoints became regional.** The client is handed a per-region hostname at
   run time - `gew4-`, `gae2-`, `guc3-spclient.spotify.com` and others - so no
   static list can enumerate them. Blocking them all would break the app anyway,
   since that host also carries playlists, search and playback state.
2. **Ad audio moved onto the music CDN.** Measured directly: during an ad the
   client streams from `audio-fa.scdn.co`, the same host that serves music.
   There is no hostname that is only ever an ad.

10 of the 39 domains on the old list had also stopped resolving entirely. The
approach is dead, not stale - which is why v3 filters by URL path instead, where
the separation is still clean.

</details>

<details>
<summary><b>Do I need seamless mode?</b></summary>

<br />

Only if silence during ads bothers you. Display ads are gone either way. Muting
needs no certificate, no proxy and no routing changes, so if you are unsure,
start without it.

</details>

<details>
<summary><b>What happens if the app crashes while seamless mode is on?</b></summary>

<br />

The PAC file falls back to `DIRECT`, so Spotify connects normally and ads come
back. You lose the blocking, not your connection. Starting the app again
restores it.

</details>

<details>
<summary><b>Does it need administrator rights?</b></summary>

<br />

No. It writes inside `%APPDATA%\Spotify` and `%LOCALAPPDATA%\SpotifyAdsSkipper`,
adjusts per-application volume, and installs its CA into the *user* certificate
store. None of that needs elevation.

</details>

---

## Disclaimer

Built for educational purposes: client-side UI patching, Windows audio session
control, and HTTPS path filtering through a local proxy.

Modifying the Spotify client and intercepting its API traffic both break
Spotify's Terms of Service. The author does not encourage blocking ads on
services you enjoy - if you love Spotify, Premium supports the artists.

---

<div align="center">
  If this tool saved your ears, consider leaving a star.
  <br /><br />
  <a href="https://buymeacoffee.com/kakofinds">
    <img src="https://img.shields.io/badge/BUY%20ME%20A%20COFFEE-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black" alt="Buy me a coffee" height="42" />
  </a>
  <br /><br />
  Built and maintained by DEV. Licensed under MIT.
</div>
