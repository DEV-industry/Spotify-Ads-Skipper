"""Patching Spotify's UI bundle to hide display advertising.

xpui.spa is a plain ZIP holding the client's web UI. index.html always pulls in
/xpui-snapshot.css, so appending a rule block there hides every ad surface with
no JavaScript involved.

That is deliberate. Spotify reminifies on every build, so module ids and local
variable names churn constantly and any patch anchored to them rots within a
release or two. The data-testid attributes below are what Spotify's own test
suite selects on, so they last far longer - and when one does disappear the rule
simply stops matching: the ad comes back, the client still runs.

One exception: the badge in the top bar. A tooltip that follows the cursor and
carries a link needs real elements, which CSS cannot create, so a small script
goes into index.html as its own block - never inside Spotify's own code. It is
wrapped in try/catch and does nothing but append one element, so the worst case
is no badge rather than a broken client.
"""

import json
import os
import shutil
import sys
import zipfile

from spotify_env import XPUI_PATH, XPUI_BACKUP, STAMP_PATH, spotify_version

CSS_ENTRY = "xpui-snapshot.css"
HTML_ENTRY = "index.html"

START_MARKER = "/* === SPOTIFY ADS SKIPPER START === */"
END_MARKER = "/* === SPOTIFY ADS SKIPPER END === */"

HTML_START_MARKER = "<!-- === SPOTIFY ADS SKIPPER START === -->"
HTML_END_MARKER = "<!-- === SPOTIFY ADS SKIPPER END === -->"

GITHUB_URL = "https://github.com/DEV-industry"
GITHUB_LABEL = "DEV-industry"

# Ad surfaces, keyed by the data-testid Spotify renders them with.
# Deliberately excluded:
#   add-button / add-to-playlist-button - not ads, they merely contain "ad"
#   ad-controls, *-context-menu-item     - controls, hiding them strands the user
#   concert-promo                        - Spotify's own editorial, not paid media
AD_TEST_IDS = [
    "home-ads-container",
    "home-ad-card",
    "home-ad-display-asset",
    "home-ad-display-asset-fallback",
    "home-ad-video-asset",
    "hpto-parent-container",
    "html-hpto-container",
    "html-hpto-iframe",
    "hpto-image",
    "hpto-native",
    "hpto-native-buttons",
    "embedded-ad",
    "embedded-ad-carousel",
    "embedded-ad-carousel-header-logo",
    "embedded-ad-carousel-header-logo-image",
    "embedded-ad-creative-link",
    "embedded-ad-cta-button-fallback",
    "ad-companion-card",
    "ad-companion-card-tagline",
    "canvas-ad-container",
    "canvas-ad-player",
    "video-player-ad-wrapper",
    "standalone-video-ad-player",
    "ads-video-player-npv",
    "ads-npv-header-maximize-button",
    "survey-ad",
    "survey-option-ad",
    "survey-ad-thank-you",
    "sponsored-recommendation-modal-trigger",
    "context-ad-logo",
    # "Leave-behind" ads: the sponsor panel that stays in the now-playing view
    # after the audio spot ends. Served from the embedded-npv slot.
    "leavebehind",
    "leavebehind-advertiser",
    "leavebehind-button",
    "leavebehind-image",
    "leavebehind-tagline",
    "leavebehinds-list-wrapper",
    "leavebehinds-see-all",
    "leavebehinds-title",
    "leavebehinds-wrapper",
    "show-leavebehind-image",
    "show-leavebehinds-container",
    "show-leavebehinds-logos-container",
    "show-leavebehinds-more-count-indicator",
    "show-leavebehinds-more-count-indicator-text",
    "show-leavebehinds-text",
]


def _icon_path():
    """cat.ico, in a source checkout or inside the frozen bundle."""
    try:
        base = sys._MEIPASS
    except AttributeError:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "cat.ico")


def build_logo_css():
    """A small badge in Spotify's top bar showing the app is active.

    Drawn with a pseudo-element rather than injected markup, so there is no
    JavaScript to go wrong: if Spotify renames the container the rule simply
    stops matching and the badge disappears, leaving the client untouched.

    Returns an empty string if the icon cannot be read - this is decoration,
    and it must never be the reason ad blocking fails to apply.
    """
    try:
        import base64
        import io

        from PIL import Image

        with Image.open(_icon_path()) as source:
            icon = source.convert("RGBA").resize((48, 48), Image.LANCZOS)
        buffer = io.BytesIO()
        icon.save(buffer, "PNG", optimize=True)
        uri = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:
        return ""

    # Anchored to body, not to the top bar: data-testid="topbar" matches
    # nothing in the shipped DOM and topbar-content-wrapper is an empty
    # measuring div. Both this badge and Spotify's own icons hang off the right
    # edge, so the gap between them holds.
    #
    # It sits between the search field and the notification bell. That strip
    # always starts 271px from the right edge - the bell does not move - but
    # its far side follows the search field, which is centred and has
    # breakpoints. Room left beside a badge ending at 314px:
    #
    #     800-815px     105px         search field collapsed to an icon
    #     830-950px      -3px         <- expands; no room at all
    #     980px          16px
    #     1000-1120px   36-105px      fine
    #     1130px          2px         <- jumps to a wider variant
    #     1170px+        22px upward
    #
    # Hence ranges below rather than one threshold. right: 292px leaves 21px of
    # clearance, matching the spacing between Spotify's own top-bar icons; the
    # offset is to the badge's right edge.
    return """
#sas-badge {
  display: none;
  position: fixed;
  top: 20px;
  right: 292px;
  width: 22px;
  height: 22px;
  border-radius: 50%%;
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.18);
  background-image: url("%s");
  background-size: cover;
  background-position: center;
  background-repeat: no-repeat;
  opacity: 0.9;
  z-index: 9999;
  cursor: pointer;
  transition: opacity 120ms ease, transform 120ms ease;
  /* Spotify's top bar is a window-drag region, and Chromium lets a drag region
     swallow every pointer event over it. Without this the badge draws but never
     sees the cursor - no hover, no tooltip, no click. */
  -webkit-app-region: no-drag;
}
@media (max-width: 815px), (min-width: 1000px) {
  #sas-badge {
    display: block;
  }
}
/* The 1130px cliff, with a margin either side of it. */
@media (min-width: 1121px) and (max-width: 1175px) {
  #sas-badge {
    display: none !important;
  }
}
#sas-badge:hover {
  opacity: 1;
  transform: scale(1.08);
}
#sas-tip {
  position: absolute;
  top: 30px;
  right: -8px;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 10px;
  border-radius: 6px;
  background: #282828;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.5);
  color: #fff;
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
  white-space: nowrap;
  opacity: 0;
  visibility: hidden;
  transition: opacity 120ms ease;
}
#sas-badge:hover #sas-tip {
  opacity: 1;
  visibility: visible;
}
#sas-tip svg {
  width: 14px;
  height: 14px;
  flex: 0 0 auto;
  fill: #fff;
}
""" % uri


GITHUB_MARK = (
    '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 '
    "3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49"
    "-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82"
    ".72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31"
    "-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 "
    "1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 "
    '0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55'
    '.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"></path></svg>'
)


def build_badge_js():
    """Script that creates the top-bar badge as a real, clickable element.

    A pseudo-element cannot take pointer events or hold a link, so the hover
    tooltip needs actual nodes. Everything here is defensive: it runs in a
    try/catch, only ever appends one element of its own to body, touches nothing
    Spotify rendered, and bails out if the element is already there. A failure
    costs the badge, not the client.

    The link is opened through Spotify's own handler when possible so it lands
    in the system browser rather than a stray CEF window.
    """
    return """
<script>
(function () {
  try {
    var ID = "sas-badge";
    var URL_ = %(url)s;
    function mount() {
      try {
        if (!document.body || document.getElementById(ID)) return;
        var a = document.createElement("a");
        a.id = ID;
        a.href = URL_;
        a.target = "_blank";
        a.rel = "noreferrer noopener";
        // No title attribute: the browser's own tooltip would pop up next to
        // ours and show the same text twice.
        var tip = document.createElement("span");
        tip.id = "sas-tip";
        tip.innerHTML = '%(mark)s';
        var label = document.createElement("span");
        // textContent, not innerHTML: the label is ours today, and this is what
        // keeps it from becoming script if it ever stops being.
        label.textContent = %(label)s;
        tip.appendChild(label);
        a.appendChild(tip);
        a.addEventListener("click", function (e) {
          try {
            e.preventDefault();
            // "noopener" matters here. The anchor carries rel="noopener", but
            // preventDefault means the anchor never opens anything - this call
            // does, and without the feature string the opened page keeps a live
            // window.opener handle back into Spotify's renderer.
            window.open(URL_, "_blank", "noopener");
          } catch (err) {}
        });
        document.body.appendChild(a);
      } catch (err) {}
    }
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", mount);
    } else {
      mount();
    }
    // Spotify re-renders large parts of the tree; put the badge back if it goes.
    setInterval(mount, 4000);
  } catch (err) {}
})();
</script>
""" % {
        # json.dumps produces the quoted JS literal, escaping included. These
        # are constants today; substituting a bare string into a script would
        # make that a happy accident rather than a property of the code.
        "url": json.dumps(GITHUB_URL),
        "label": json.dumps(GITHUB_LABEL),
        "mark": GITHUB_MARK,
    }


def build_css():
    selectors = ",\n".join('[data-testid="%s"]' % t for t in AD_TEST_IDS)
    return "\n%s\n%s {\n  display: none !important;\n}\n%s%s\n" % (
        START_MARKER,
        selectors,
        build_logo_css(),
        END_MARKER,
    )


def _strip_block(css):
    """Remove a previously injected block so patches never stack up."""
    if START_MARKER not in css or END_MARKER not in css:
        return css
    before = css.split(START_MARKER)[0]
    after = css.split(END_MARKER)[1]
    return before.rstrip() + "\n" + after.lstrip()


def _strip_html_block(html):
    """Same idea for index.html, so repeated patches leave one script."""
    if HTML_START_MARKER not in html or HTML_END_MARKER not in html:
        return html
    before = html.split(HTML_START_MARKER)[0]
    after = html.split(HTML_END_MARKER)[1]
    return before + after


def build_html(html):
    """Put the badge script in just before </body>.

    Appended as its own marked block rather than woven into Spotify's markup:
    it stays trivially removable, and a future Spotify build that reshuffles
    index.html cannot half-apply it.
    """
    html = _strip_html_block(html)
    block = "%s%s%s" % (HTML_START_MARKER, build_badge_js(), HTML_END_MARKER)
    if "</body>" in html:
        return html.replace("</body>", block + "</body>", 1)
    return html + block


def _is_intact(path):
    """True when path is a bundle whose CSS entry can actually be read.

    This guards both directions of the backup: whether the current bundle is
    fit to copy, and whether a backup is fit to write over a working file. So
    it has to decompress, not just list. namelist() only reads the central
    directory, and an entry can be present there while its data is truncated
    or stored with a method zipfile cannot handle - which would pass the guard
    and restore a Spotify that does not render.
    """
    try:
        with zipfile.ZipFile(path, "r") as archive:
            if CSS_ENTRY not in archive.namelist():
                return False
            with archive.open(CSS_ENTRY) as entry:
                while entry.read(65536):
                    pass
        return True
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError, ValueError):
        return False


def is_patched(path=None):
    # Resolved at call time, not bound as a default: binding XPUI_PATH into the
    # signature would freeze it at import and silently ignore any later change.
    path = path or XPUI_PATH
    if not os.path.isfile(path):
        return False
    try:
        with zipfile.ZipFile(path, "r") as archive:
            if CSS_ENTRY not in archive.namelist():
                return False
            css = archive.read(CSS_ENTRY).decode("utf-8", errors="replace")
        return START_MARKER in css
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return False


def read_stamp():
    """Spotify version recorded at patch time, or None."""
    try:
        with open(STAMP_PATH, "r", encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def needs_repatch():
    """True when Spotify has updated itself and wiped our patch.

    Spotify replaces xpui.spa wholesale on update, so both the injected CSS and
    our backup's provenance go stale at once.
    """
    if not os.path.isfile(XPUI_PATH):
        return False
    if not is_patched():
        return True
    return read_stamp() != spotify_version()


def _ensure_backup():
    """Keep one pristine copy of xpui.spa, matching the installed version.

    Only ever taken from an unpatched bundle - re-backing-up a patched file
    would destroy the ability to cleanly restore.
    """
    if is_patched(XPUI_PATH):
        # Current bundle is already patched and cannot serve as a fresh
        # original; fall back to whatever backup exists rather than enshrine
        # a patched file as the "original".
        return os.path.isfile(XPUI_BACKUP)
    if not _is_intact(XPUI_PATH):
        # Unreadable bundle - never let a corrupt file clobber a good backup.
        return os.path.isfile(XPUI_BACKUP)
    # Current bundle is pristine - retake the backup from it even when one
    # already exists. Spotify swaps xpui.spa wholesale on self-update, so a
    # backup kept from before the update belongs to the previous version;
    # patching on top of it rolls the UI back under newer client binaries,
    # which renders as an empty window on startup.
    try:
        shutil.copy2(XPUI_PATH, XPUI_BACKUP)
    except OSError:
        # Locked or unwritable backup: fail the patch cleanly instead of
        # letting the exception kill the calling thread. The old backup (if
        # any) may be stale or half-written, so it must not be used either.
        return False
    return True


def apply_patch():
    """Inject the ad-hiding CSS. Returns (ok, message).

    Spotify must not be running: it holds xpui.spa open.
    """
    if not os.path.isfile(XPUI_PATH):
        return False, "xpui.spa not found - is Spotify installed?"

    if not _ensure_backup():
        return False, "No clean backup of xpui.spa available."

    source = XPUI_BACKUP if os.path.isfile(XPUI_BACKUP) else XPUI_PATH
    temp_path = XPUI_PATH + ".tmp"

    # Recorded before the work starts. Reading it afterwards would mean that a
    # Spotify update landing mid-patch gets stamped with the new version while
    # the bundle on disk is built from the old one - and needs_repatch() would
    # then see a matching stamp and never correct itself.
    version_at_start = spotify_version()
    try:
        source_state = os.stat(XPUI_PATH)
    except OSError:
        source_state = None

    try:
        with zipfile.ZipFile(source, "r") as src:
            if CSS_ENTRY not in src.namelist():
                return False, "%s missing - Spotify's bundle layout changed." % CSS_ENTRY

            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as dst:
                for item in src.infolist():
                    data = src.read(item.filename)
                    # surrogateescape, not "replace": these bytes get written
                    # straight back into Spotify's own bundle, and "replace"
                    # turns anything that is not valid UTF-8 into U+FFFD
                    # permanently. surrogateescape round-trips it untouched.
                    if item.filename == CSS_ENTRY:
                        css = _strip_block(data.decode("utf-8", errors="surrogateescape"))
                        data = (css + build_css()).encode("utf-8", errors="surrogateescape")
                    elif item.filename == HTML_ENTRY:
                        html = data.decode("utf-8", errors="surrogateescape")
                        data = build_html(html).encode("utf-8", errors="surrogateescape")
                    dst.writestr(item, data)

        # Spotify may have replaced the bundle while we were rebuilding it. If
        # it did, ours was built from what is now the previous version, and
        # dropping it into place would roll the UI back under newer binaries.
        current_state = os.stat(XPUI_PATH)
        if source_state is not None and (
            current_state.st_mtime_ns != source_state.st_mtime_ns
            or current_state.st_size != source_state.st_size
        ):
            raise OSError("Spotify replaced xpui.spa while it was being patched")

        os.replace(temp_path, XPUI_PATH)
    # RuntimeError belongs here alongside the obvious ones: zipfile raises it,
    # not BadZipFile, for an encrypted entry or a compression method it does
    # not implement. Left out, it escaped this function entirely and took the
    # caller's startup thread with it - so a single odd entry in Spotify's own
    # bundle stopped the audio-ad blocking, which has nothing to do with zips.
    except (zipfile.BadZipFile, OSError, PermissionError, RuntimeError) as exc:
        if os.path.isfile(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return False, "Could not write xpui.spa (%s)" % exc

    try:
        with open(STAMP_PATH, "w", encoding="utf-8") as handle:
            handle.write(version_at_start or "")
    except OSError:
        pass

    return True, "Display ads blocked (%d selectors)." % len(AD_TEST_IDS)


def restore():
    """Put the untouched bundle back. Returns (ok, message).

    The backup is checked before it is trusted. A truncated one - a crash or a
    full disk while it was being taken - used to be copied over a perfectly
    good xpui.spa and then deleted, leaving Spotify unable to start and no way
    back short of reinstalling it, all reported as a success.
    """
    if not os.path.isfile(XPUI_BACKUP):
        return True, "Nothing to restore."

    if not _is_intact(XPUI_BACKUP):
        return False, (
            "The saved copy of Spotify's UI is damaged, so it has been left "
            "alone rather than written over the working one. Reinstall Spotify "
            "to get a clean copy."
        )

    temp_path = XPUI_PATH + ".restore-tmp"
    try:
        shutil.copy2(XPUI_BACKUP, temp_path)
        if not _is_intact(temp_path):
            raise OSError("copy did not survive verification")
        os.replace(temp_path, XPUI_PATH)
    except (OSError, PermissionError) as exc:
        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return False, "Could not restore xpui.spa (%s)" % exc

    # Only now is the backup expendable.
    for path in (XPUI_BACKUP, STAMP_PATH):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    return True, "Original Spotify UI restored."
