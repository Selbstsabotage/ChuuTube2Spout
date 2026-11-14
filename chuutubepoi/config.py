# SPDX-License-Identifier: GPL-3.0-or-later
"""Runtime knobs, overridable by environment variable.

These exist because the handful of settings most likely to need adjusting on a
given machine - image orientation, hardware decoding - are exactly the ones you
cannot verify without that machine in front of you. Changing one should not mean
editing source.

Set them in run.bat, or in a shell before launching:

    set CHUUTUBEPOI_HWDEC=no
    set CHUUTUBEPOI_PREVIEW_FLIP=0
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .bootstrap import project_root

# settings.txt, or a file left from one of the app's earlier names.
SETTINGS_FILES = ("settings.txt", "chuutube2" + "spout.conf", "yt2spout.conf")

# Settings are looked up as CHUUTUBEPOI_<KEY>. The prefixes of the app's two
# earlier names (ChuuTube2Spout until 1.1, yt2spout before that) are still
# accepted, so an existing environment or a copied run.bat keeps working;
# settings.txt uses bare keys and was never affected either way.
#
# The literals below are deliberately NOT written as one word. A blanket
# rename across the tree once rewrote the old prefix to match ENV_PREFIX,
# which left the fallback pointing at itself and silently doing nothing - the
# tests exist because of that. Newest first: it wins over the older one.
ENV_PREFIX = "CHUUTUBEPOI_"
LEGACY_PREFIXES = ("CHUUTUBE2" + "SPOUT_", "YT2" + "SPOUT_")


def _roots() -> list[Path]:
    """Where user-editable files live, most-preferred first.

    project_root() is the folder the app was installed into: the source tree
    when running from source, and the folder holding the .exe when frozen.
    Path(__file__).parent.parent is NOT the same thing in a frozen build - it
    is PyInstaller's `_internal`, which is a private implementation directory.
    Settings dropped there would be ignored and a cache built there would be
    thrown away by the next unzip, so the exe folder has to win.
    """
    roots = [project_root()]
    legacy = Path(__file__).resolve().parent.parent
    if legacy not in roots:
        roots.append(legacy)
    return roots


def _settings_path() -> Path | None:
    # CHUUTUBEPOI_SETTINGS_FILE names another file, or "none" for no file.
    # The tests set "none": they must see the built-in defaults, not whatever
    # the developer's own settings.txt says (K's 0.9.9.40 build stopped on
    # eight tests because his settings.txt set a fixed 1920x1080 canvas).
    override = os.environ.get(ENV_PREFIX + "SETTINGS_FILE")
    if override is not None:
        override = override.strip()
        if not override or override.lower() == "none":
            return None
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    for root in _roots():
        for name in SETTINGS_FILES:
            candidate = root / name
            if candidate.is_file():
                return candidate
    return None


_INLINE_COMMENT = re.compile(r"\s+#")


def load_settings_file() -> str | None:
    """Read settings.txt into the environment. Real env vars still win.

    Settings used to live as commented-out `set` lines in run.bat, which had two
    problems: doctor.bat and check.bat are separate processes that never saw
    them, and every update overwrote the file along with whatever you had
    uncommented. A plain key=value file next to the app is read by everything
    and is never overwritten.
    """
    path = _settings_path()
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;" or line.lower().startswith("rem "):
            continue
        if line.lower().startswith("set "):
            line = line[4:].strip()          # tolerate copied run.bat lines
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        # `set X=y & rem comment` leaves the comment in the value.
        value = value.split("&", 1)[0]
        # So does `X=y   # comment`, which settings.txt.example itself used
        # until 0.9.9.29: uncommenting `HWDEC=no   # black video...` set HWDEC
        # to the whole line. Only a # after whitespace starts a comment, so a
        # colour like SENDER_BACKGROUND=#101018 is kept.
        value = _INLINE_COMMENT.split(value, 1)[0]
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        key = key.upper()
        if not key.startswith((ENV_PREFIX,) + LEGACY_PREFIXES):
            key = ENV_PREFIX + key
        os.environ.setdefault(key, value)
    return str(path)


SETTINGS_FILE = load_settings_file()


def _raw(key: str) -> str | None:
    """The value of one setting, or None. Current prefix wins over the old."""
    for prefix in (ENV_PREFIX,) + LEGACY_PREFIXES:
        value = os.environ.get(prefix + key)
        if value is not None:
            return value
    return None


def _flag(key: str, default: bool) -> bool:
    raw = _raw(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _text(key: str, default: str) -> str:
    raw = _raw(key)
    return (raw or "").strip() or default


def _fnum(key, default: float) -> float:
    """A float setting. _num returns text for the caller to convert."""
    try:
        return float(_num(key, str(default)))
    except ValueError:
        return default


def _num(key: str, default: str) -> str:
    """Raw text for a value that a caller will convert to int/float."""
    return (_raw(key) or default).strip() or default


# --- Orientation -------------------------------------------------------------
# With flip_y=False mpv writes the picture top-down into our FBO: texel row 0 is
# the top of the image. That is what Direct3D (and therefore Spout) wants, so
# Spout needs no inversion - while the on-screen preview, drawn in OpenGL's
# bottom-up window space, does.
#
# These are independent on purpose: fixing an upside-down preview can never
# silently flip what Resolume receives.
MPV_FLIP_Y = _flag("MPV_FLIP_Y", False)
SPOUT_INVERT = _flag("SPOUT_INVERT", False)
PREVIEW_FLIP = _flag("PREVIEW_FLIP", True)

# Syphon (macOS) is OpenGL and expects row 0 at the BOTTOM - the opposite of
# what mpv writes for Spout. So the Syphon server is told the texture is
# flipped. Separate from SPOUT_INVERT for the same reason as above. If the
# picture arrives upside down in Resolume on a Mac, set SYPHON_FLIP=0.
SYPHON_FLIP = _flag("SYPHON_FLIP", True)

# How the frame gets into Syphon (macOS).
#   metal   (default) we copy our texture into a shared IOSurface ourselves
#           and Syphon's Metal server publishes it.
#   opengl  Syphon's OpenGL server draws our texture into its surface.
# K, 1.0.8: with opengl the 4:3 bars reached every receiver black - a second
# process read alpha 255 where our texture holds 0 - while Syphon's Metal
# server kept them transparent in Resolume. opengl stays as the fallback, and
# is used by itself if the Metal path cannot start.
SYPHON_SERVER = _text("SYPHON_SERVER", "metal").lower()
if SYPHON_SERVER not in ("metal", "opengl"):
    SYPHON_SERVER = "metal"

# --- Decoding ----------------------------------------------------------------
# 'auto-safe' only enables hardware decoders mpv considers reliable. If video
# comes out black or the app crashes on load, try 'no' (CPU decode), then
# 'd3d11va-copy'.
HWDEC = _text("HWDEC", "auto-safe")

# mpv log level piped into the status line / stderr: no, fatal, error, warn,
# info, v, debug
MPV_LOGLEVEL = _text("MPV_LOGLEVEL", "info")

# Let mpv's render call SLEEP until each frame's display time.
#
# Off, and it has to stay off. libmpv's render API defaults this ON, which is
# right for a video player - the render call is how a player paces itself to
# the monitor. We are not a player: we render off a timer into an FBO and hand
# the texture to Spout, and Resolume has its own clock. Blocking here parks the
# GUI thread inside mpv for most of every frame interval.
#
# Measured, not assumed: with it on, `PERF_LOG_S=5` reported 37.5ms per frame
# in render and 97% of the GUI thread, against two clips that together are a
# few milliseconds of actual GPU work. 37.5ms is not work - it is the 25fps
# clip's frame interval, which is the tell.
#
# It also made the two slots fight. Both are rendered in the same tick, so a
# 30fps clip in A could only advance when the 25fps clip in B let the loop go,
# and A played at 25fps.
#
# Here as a setting only so it can be turned back on to reproduce that.
MPV_BLOCK_FOR_TARGET_TIME = _flag("MPV_BLOCK_FOR_TARGET_TIME", False)

# --- How media is obtained ---------------------------------------------------
# "download": fetch the whole clip with yt-dlp, then play the file. Slower to
#   start, then rock solid - local files seek instantly, loop cleanly and cannot
#   expire mid-set. The default, and for YouTube currently the only one that
#   works at 1080p.
# "hybrid": resolve, start playing the network stream at once, cache underneath,
#   then swap to the file at the same timestamp. Sound idea, and it does fall
#   back correctly - but on YouTube today the stream half cannot deliver.
#   resolve() runs through yt-dlp's Python API, which has no PO token provider
#   wired into it, so the only formats it is offered are the token-free ones:
#   an HLS manifest (itag 96) whose every segment answers 403. The picture that
#   eventually appears is the download leg finishing, having also made mpv
#   hammer a dead playlist for fifteen seconds first. Fixing it means giving
#   the API the same token pipeline the subprocess has, and even then a signed
#   URL only tolerates a handful of requests. See ROADMAP.md.
# "stream": play over the network and never cache. For direct media URLs and
#   local files; the same 403 applies to YouTube.
MODE = _text("MODE", "download").lower()

# Settable, and two of the three settings do not work - which made this a trap
# rather than an option. A user who reads the list above and tries "stream" gets
# an app that extracts, plays nothing, and explains itself only in a log they
# are not watching. Refuse at startup instead, with the reason.
#
# Not raised here: config is imported by doctor, by the yt-dlp re-entry path and
# by the tests, and a module that can refuse to import takes all of them down
# with it. The caller decides what to do about a problem it can see.
WORKING_MODES = ("download",)


def mode_problem() -> str | None:
    """Why the configured MODE will not work, or None if it is fine."""
    if MODE in WORKING_MODES:
        return None
    if MODE in ("stream", "hybrid"):
        return (
            f"MODE={MODE} does not currently work for YouTube.\n\n"
            "Both routes resolve through yt-dlp's Python API, which has no PO "
            "token provider wired into it. The only formats it is offered are "
            "the token-free ones - an HLS manifest whose every segment answers "
            "403 - so nothing plays.\n\n"
            "Remove the MODE line from settings.txt, or set MODE=download."
        )
    return (f"MODE={MODE} is not a mode this understands. "
            f"Use one of: {', '.join(WORKING_MODES)}.")

# Where downloaded clips live. Deleting this folder is always safe.
CACHE_DIR = _raw("CACHE") or str(project_root() / "cache")

# Pass -v to yt-dlp. Verbose, but it prints exactly which player clients and
# JavaScript runtime it is using, which is the first thing worth knowing.
YTDLP_VERBOSE = _flag("YTDLP_VERBOSE", True)

# Verbose logging from the PO token server.
POT_VERBOSE = _flag("POT_VERBOSE", True)

# Prefer the token server over the per-request script.
#
# Both mint the same PO token. Script mode spawns Node once per token, which
# the timing line reported as about 1.3s of every cold load (measured with
# deno, before 1.1); the server is already running, so the same token costs an
# HTTP round trip on loopback.
# Script mode stays as the fallback - it needs no separate executable, and the
# server is refused outright when its version does not match the plugin's.
TOKEN_SERVER = _flag("TOKEN_SERVER", True)

# yt-dlp needs a challenge-solver script to descramble YouTube's signatures.
# Having a JS runtime is necessary but NOT sufficient: the solver itself is
# fetched on demand, and without it signature and "n" challenges fail, leaving
# only the formats that need a PO token. Set to "" to disable the download.
#
# Not _text(): that reads an empty value as "use the default", so
# `REMOTE_COMPONENTS=` - the documented way to turn this off, and the one
# SECURITY.md has always given - silently left it on until 0.9.9.26.
def _text_or_empty(key: str, default: str) -> str:
    raw = _raw(key)
    return default if raw is None else raw.strip()


REMOTE_COMPONENTS = _text_or_empty("REMOTE_COMPONENTS", "ejs:github")

# --- YouTube extraction ------------------------------------------------------
# Leave PLAYER_CLIENT empty to let yt-dlp choose - it retunes its own defaults
# every few weeks and usually knows better. Pin one only when `--check` has told
# you which client actually works, e.g. "tv" or "web_safari".
PLAYER_CLIENT = (_raw("PLAYER_CLIENT") or "").strip()

# Borrow YouTube cookies from an installed browser: firefox, chrome, edge, brave...
# Being signed in clears a lot of 403s, especially from shared or VPN addresses.
COOKIES_FROM_BROWSER = (_raw("COOKIES_FROM_BROWSER") or "").strip()

# Copy the browser's cookie store and read our own snapshot of it, rather than
# letting yt-dlp read the live database on every invocation. The browser can
# then stay open - which it needs to, since that is where the URLs come from -
# and each fetch skips re-parsing a few thousand cookies. Firefox-family only;
# Chrome-family stores are encrypted, and fall back to yt-dlp doing the work.
COOKIE_EXPORT = _flag("COOKIE_EXPORT", True)

# At start, check once that the browser above is signed in to YouTube, and
# say so in a message box if it is not (1.0.5). YouTube refuses or limits most
# clips without a login. COOKIE_WARNING=0 turns the message off, for anyone who
# runs without cookies on purpose.
COOKIE_WARNING = _flag("COOKIE_WARNING", True)

# Fetch the audio track at all. Off by default: this is a VJ tool feeding
# Resolume, the sound almost always comes from somewhere else, and skipping it
# removes a second stream from every load. Turning it on brings the mute button
# back with it.
FETCH_AUDIO = _flag("FETCH_AUDIO", False)

# One line per load saying where the seconds went. On by default: the whole
# reason it exists is that the answer was surprising - the transfer is 2-7s and
# the overhead in front of it is a flat ~11s - and a number nobody sees is a
# number nobody acts on.
LOAD_TIMING = _flag("LOAD_TIMING", True)

# Start playing while yt-dlp is still writing the file, once this much of it
# exists. The transfer is 1.5s on a fast line and minutes on venue wifi, and
# this is the difference between "slow" and "unusable" - so it is on by default
# and the size is small. Turning it off waits for the whole file, as before.
PARTIAL_PLAY = _flag("PARTIAL_PLAY", True)
PARTIAL_START_MB = _fnum("PARTIAL_START_MB", 4.0)
# Seconds of transfer before the half-written file is worth handing over.
# Handing over costs a reload when the download lands, and a reload two
# seconds into a clip is indistinguishable from starting it again - so a
# download that was going to be finished by now should just be waited for.
PARTIAL_AFTER_S = _fnum("PARTIAL_AFTER_S", 5.0)

# Start fetching a URL the moment it lands in the box, before Load is pressed.
# The fixed cost of a load is about five seconds of page extraction, PO token
# and JS challenge before a byte of video moves, and none of it depends on
# anyone having decided to play the clip yet. Paste the next clip while the
# current one is running and Load becomes instant.
PREFETCH = _flag("PREFETCH", True)

# How long the box has to stop changing before a typed or edited URL counts as
# settled. The Paste button does not wait - it knows the URL is complete.
PREFETCH_DEBOUNCE_MS = max(0, int(_num("PREFETCH_DEBOUNCE_MS", "900")))

# Keep yt-dlp's own description of a video, so asking for it again at a
# different quality does not repeat the expensive half of a load.
#
# Extraction, the PO token and the JS challenge are four to eight seconds and
# none of them depend on which format was asked for - the formats were all
# listed the first time. --load-info-json replays that answer and goes straight
# to the transfer, which is what makes the quality dropdown feel instant.
INFO_CACHE = _flag("INFO_CACHE", True)

# How long that answer is worth replaying. The media URLs inside it are signed
# and expire - YouTube's own `expire` is usually about six hours - but they also
# stop working for less visible reasons, so this is deliberately far short of
# it. A stale one costs a failed attempt and a normal fetch, not a failed load.
INFO_CACHE_MIN = float(_num("INFO_CACHE_MIN", "45"))

# Imported rather than repeated, so the two cannot drift apart again: this list
# used to be duplicated here with google.com still in it, which silently undid
# the narrowing done in cookies.py.
from .cookies import DEFAULT_DOMAINS as COOKIE_DEFAULTS  # noqa: E402

# How old the exported cookies.txt may get before it is refreshed, in minutes.
COOKIE_MAX_AGE_MIN = float(_num("COOKIE_MAX_AGE_MIN", "60"))

# Which cookies get written out. This is the list the exporter actually uses,
# so it must agree with cookies.DEFAULT_DOMAINS. An empty value falls back to
# the defaults (`_num`), so there is no way to ask for every cookie by leaving
# it blank - settings.txt.example claimed otherwise until 0.9.9.29.
#
# NOT google.com: a suffix match on it also takes accounts./mail./pay., which
# is the Google account rather than YouTube. See SECURITY.md.
COOKIE_DOMAINS = [d.strip() for d in _num(
    "COOKIE_DOMAINS", ",".join(COOKIE_DEFAULTS)).split(",") if d.strip()]

# Clients tried in order until one produces a file. "default" means "let yt-dlp
# choose", and it is first because measurement says so: across four logged loads
# it succeeded every time, at full resolution, in about 8s.
#
# What it displaced, and why:
#   mweb        - 403s partway through the transfer, every time it was tried.
#                 Extraction and the PO token both succeed, then the media
#                 server refuses a later ranged request (HTTP_CHUNK_SIZE means
#                 there are several). Roughly 12s to find that out. Kept as a
#                 fallback because it does yield full-resolution formats when it
#                 works, and the bytes it did fetch are resumed by whoever
#                 succeeds next.
#   web_safari  - forced SABR, so only an HLS manifest: lower-capped muxed
#                 itags, a fragment throttle, and no playing while it downloads.
#                 The chain walks away from it and comes back only if nothing
#                 else works.
#   tv_simply   - dropped. "Skipping client tv_simply since it does not support
#                 cookies", and cookies are always passed. It cannot ever
#                 produce a file here; it only spends 4s and one more request
#                 against the rate limit that then 429s the next client.
#   tv          - frequently UNPLAYABLE or DRM. Kept last.
#
# Every attempt is also a request YouTube counts: five clients in forty seconds
# is what produced "HTTP Error 429: Too Many Requests" on the ones after mweb,
# which then failed for that reason rather than on their own merits. A shorter
# chain is faster twice over.
CLIENT_CHAIN = [c.strip() for c in _num(
    "CLIENT_CHAIN",
    "default,mweb,web_safari,tv",
).split(",") if c.strip()]

# Names for "no --extractor-args at all; let yt-dlp pick the client".
AUTO_CLIENT = {"default", "auto", "none"}

# Fetch 2 KB of the chosen URL before handing it to mpv, so a refusal costs a
# retry with another client instead of a dead output. Worth ~200ms per load.
VERIFY_STREAMS = _flag("VERIFY_STREAMS", True)

# Route media through a loopback relay that attaches the URL's own headers.
# mpv's HTTP layer is migrating to curl and currently ignores --user-agent and
# --http-header-fields, which makes signed YouTube URLs 403. Turn this off only
# to test whether a future libmpv build has fixed that.
USE_PROXY = _flag("PROXY", True)

# Size of each upstream fetch, in MB. YouTube refuses open-ended range requests,
# so the relay always asks for a bounded window. Smaller means more requests but
# a slightly faster response to a seek.
CHUNK_MB = max(1, int(_num("CHUNK_MB", "10")))

# Ask yt-dlp to fetch in bounded chunks. Large DASH formats lack the
# `ratebypass` parameter that muxed ones carry, and a single continuous GET of
# one gets cut off with 403 partway through. Chunked requests are what the
# media servers expect for these.
HTTP_CHUNK_SIZE = _text("HTTP_CHUNK_SIZE", "10M")

# Preferred video codecs, best first. H.264 decodes on everything; AV1 is what
# YouTube hands out at 1080p by default and is fussier on older GPUs.
CODEC_PREFERENCE = [c.strip().lower() for c in _num(
    "CODEC_PREFERENCE", "avc1,h264,vp9,vp09,av01").split(",") if c.strip()]

# --- Spout sender names ------------------------------------------------------
# Renamed with the app at 2.11.1. Anything in Resolume that was pointing at
# yt2spout-A/B needs re-pointing once; set these back to the old names if you
# would rather not. (There was a third, SENDER_PROGRAM, until 0.9.9.20; a
# leftover line for it in settings.txt is simply not read.)
#
# Renamed again with the app at 1.1 (K: "ChuuTube2Spout doesn't really work"
# once it also sends Syphon): ChuuTubePoi-A/B, the same on Windows and macOS.
# A Resolume composition made with 1.0 points at the old names; to keep it
# working unchanged, set SENDER_A / SENDER_B to OLD_SENDER_NAMES.
OLD_SENDER_NAMES = ("ChuuTube2" + "Spout-A", "ChuuTube2" + "Spout-B")


def default_sender_names(platform: str | None = None) -> tuple[str, str]:
    """The two source names. `platform` no longer matters: one name for both."""
    return "ChuuTubePoi-A", "ChuuTubePoi-B"


SENDER_A = _text("SENDER_A", default_sender_names()[0])
SENDER_B = _text("SENDER_B", default_sender_names()[1])

# Ceiling on any texture we allocate, and therefore on the canvas below. Up
# here rather than with the other odds and ends because canvas() clamps
# against it, and a module-level call cannot read a name defined further down.
MAX_TEXTURE_DIM = int(_num("MAX_TEXTURE_DIM", "8192"))

# --- Spout canvas ------------------------------------------------------------
# A fixed resolution every sender publishes at, whatever the clip's own size.
#
# Off by default since 0.9.9.38 (K): each clip is sent at its own resolution,
# so a 4:3 clip arrives as 4:3 and Resolume fits it with its own clip settings
# - no bars in the picture. A fixed canvas (0.9.9.7 to 0.9.9.37) kept the
# layer's size constant across clips, at the cost of black bars around every
# clip that is not the canvas shape. Set both to get it back.
SENDER_WIDTH = int(_num("SENDER_WIDTH", "0"))
SENDER_HEIGHT = int(_num("SENDER_HEIGHT", "0"))

# Without a fixed canvas: the largest size a clip is sent at. A bigger clip is
# scaled down to fit inside, keeping its aspect ratio; a smaller one is sent
# as it is. Either may be set alone. 0 is no limit. (K, 0.9.9.38: "wouldn't a
# max width or height setting work to preserve aspect ratio?")
SENDER_MAX_WIDTH = max(0, int(_num("SENDER_MAX_WIDTH", "0")))
SENDER_MAX_HEIGHT = max(0, int(_num("SENDER_MAX_HEIGHT", "0")))

# What to do when the clip is not the canvas shape - a 4:3 upload, or a phone
# video shot vertically.
#
#   fit     scale until it fits and leave bars. Nothing is lost; you see the
#           whole frame. The default, because cropping something you have not
#           watched yet is a decision that cannot be undone on stage.
#   fill    scale until it covers and crop the overflow. No bars, at the cost
#           of the edges - and of most of a vertical video.
#   stretch ignore the aspect ratio entirely. Distorts. Occasionally what you
#           want on an LED wall that is not a normal shape.
SENDER_FIT = _text("SENDER_FIT", "fit").lower()
FIT_MODES = ("fit", "fill", "stretch")

# What fills the bars that "fit" leaves. Black by default, because that is what
# every Resolume project built against this app has been receiving and because
# black is what a VJ expects an empty part of a layer to be.
#
#   black        opaque black. The default.
#   transparent  alpha 0, so a 4:3 clip composites over the layer beneath it
#                instead of boxing it out. Spout carries RGBA and Resolume
#                respects it - but it changes what every existing project
#                receives, so it is opt-in rather than clever-by-default.
#   #RRGGBB      any colour, for a wall whose "off" is not black.
SENDER_BACKGROUND = _text("SENDER_BACKGROUND", "black").lower()

_HEX = "0123456789abcdef"


def _is_hex_colour(text: str) -> bool:
    return (len(text) == 7 and text[0] == "#"
            and all(c in _HEX for c in text[1:]))


def background_rgba() -> tuple[float, float, float, float]:
    """The clear colour for our own FBO, as OpenGL wants it.

    mpv paints the bars, but the buffer has to START the right colour too -
    the first frame after an allocation, and the clear that blanks a slot with
    nothing loaded, are both ours.
    """
    if SENDER_BACKGROUND == "transparent":
        return 0.0, 0.0, 0.0, 0.0
    if _is_hex_colour(SENDER_BACKGROUND):
        r = int(SENDER_BACKGROUND[1:3], 16) / 255.0
        g = int(SENDER_BACKGROUND[3:5], 16) / 255.0
        b = int(SENDER_BACKGROUND[5:7], 16) / 255.0
        return r, g, b, 1.0
    return 0.0, 0.0, 0.0, 1.0


def background_colour() -> str:
    """The same thing as mpv spells it, or "" when there is no colour."""
    if SENDER_BACKGROUND == "transparent":
        return ""
    if _is_hex_colour(SENDER_BACKGROUND):
        return SENDER_BACKGROUND
    return "#000000"


def canvas() -> tuple[int, int]:
    """The fixed canvas, or (0, 0) meaning "follow each clip".

    Both numbers or neither: a width with no height cannot describe a canvas,
    and silently inventing the missing one would publish at a size nobody
    asked for. Falling back to the clip's own size is at least the behaviour
    that was there before.
    """
    width, height = SENDER_WIDTH, SENDER_HEIGHT
    if width <= 0 or height <= 0:
        return 0, 0
    return (max(2, min(width, MAX_TEXTURE_DIM)),
            max(2, min(height, MAX_TEXTURE_DIM)))


def canvas_for(video_w: int, video_h: int) -> tuple[int, int]:
    """The size to render this clip at: the canvas, or the clip's own size."""
    width, height = canvas()
    if width and height:
        return width, height
    return limited_size(int(video_w), int(video_h))


def limited_size(video_w: int, video_h: int) -> tuple[int, int]:
    """The clip's own size, scaled down to SENDER_MAX_* if it is larger.

    One factor for both axes, so the shape is kept. Never scales up.
    """
    if video_w <= 0 or video_h <= 0:
        return video_w, video_h
    scale = 1.0
    if SENDER_MAX_WIDTH and video_w > SENDER_MAX_WIDTH:
        scale = min(scale, SENDER_MAX_WIDTH / video_w)
    if SENDER_MAX_HEIGHT and video_h > SENDER_MAX_HEIGHT:
        scale = min(scale, SENDER_MAX_HEIGHT / video_h)
    width = max(2, min(round(video_w * scale), MAX_TEXTURE_DIM))
    height = max(2, min(round(video_h * scale), MAX_TEXTURE_DIM))
    return width, height


def output_description() -> str:
    """How the senders are sized, in a few words for the window and the log."""
    width, height = canvas()
    if width and height:
        return f"{width}x{height}"
    if SENDER_MAX_WIDTH and SENDER_MAX_HEIGHT:
        return f"clip size, max {SENDER_MAX_WIDTH}x{SENDER_MAX_HEIGHT}"
    if SENDER_MAX_WIDTH:
        return f"clip size, max {SENDER_MAX_WIDTH} wide"
    if SENDER_MAX_HEIGHT:
        return f"clip size, max {SENDER_MAX_HEIGHT} high"
    return "clip size"


def fit_report(video_w: int, video_h: int, canvas_w: int, canvas_h: int,
               mode: str = "") -> tuple[int, int, float]:
    """Where the picture lands in the canvas: (width, height, fraction kept).

    This is mpv's arithmetic restated, not a second implementation of it - mpv
    does the actual scaling. It is here because two things need the same
    answer: the warning when "fill" is about to crop most of a clip away, and
    the tests that say in numbers what each mode does to an odd-shaped video.

    `fraction kept` is how much of the source frame survives: 1.0 whenever
    nothing is cropped, which is every case except "fill".
    """
    mode = (mode or SENDER_FIT).lower()
    if video_w <= 0 or video_h <= 0 or canvas_w <= 0 or canvas_h <= 0:
        return 0, 0, 0.0
    if mode == "stretch":
        # Fills the canvas by distorting. Nothing is cropped, which is why
        # "kept" is 1.0 - it is not a measure of whether it looks right.
        return canvas_w, canvas_h, 1.0

    source = video_w / video_h
    canvas = canvas_w / canvas_h
    if mode == "fill":
        return canvas_w, canvas_h, min(source, canvas) / max(source, canvas)

    # fit: scale until it is inside, leave the rest as background.
    scale = min(canvas_w / video_w, canvas_h / video_h)
    return (max(1, round(video_w * scale)), max(1, round(video_h * scale)), 1.0)


def canvas_problem() -> str | None:
    """Why the canvas settings will not do what they say, or None."""
    if (SENDER_BACKGROUND not in ("black", "transparent")
            and not _is_hex_colour(SENDER_BACKGROUND)):
        return (f"SENDER_BACKGROUND={SENDER_BACKGROUND} is not a background. "
                "Use 'black', 'transparent', or a colour like #101018.")
    if SENDER_FIT not in FIT_MODES:
        return (f"SENDER_FIT={SENDER_FIT} is not a fit mode. "
                f"Use one of: {', '.join(FIT_MODES)}.")
    if (SENDER_WIDTH > 0) != (SENDER_HEIGHT > 0):
        return ("SENDER_WIDTH and SENDER_HEIGHT have to be set together - "
                "one on its own does not describe a canvas. Set both, or "
                "neither to publish each clip at its own size.")
    return None


# --- Preview window ----------------------------------------------------------
# The on-screen preview is a monitor, not the output: Spout gets every frame
# regardless of what this is set to. Each preview repaint is a GUI-thread round
# trip, so a lower number leaves more of the thread for buttons and the seekbar.
#
# *** THE DEFAULT IS 60 AND MUST STAY 60. ***
# It was lowered to 30 once, on the theory that halving the repaints was free
# responsiveness because nobody judges quality from a preview. That was wrong: a
# 30Hz ceiling against 24fps material lands paints unevenly and the preview
# visibly judders. Do not "optimise" this again - if the GUI thread needs
# relief, take it from the tick rate below instead.
PREVIEW_FPS = max(1, int(_num("PREVIEW_FPS", "60")))

# Aspect ratio the preview cells are sized for when the window first opens, so
# two of them sit side by side with no letterboxing. 16/9 unless your material
# is something else.
def _preview_aspect() -> float:
    raw = (_raw("PREVIEW_ASPECT") or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    # Follow the canvas rather than assuming 16/9. The preview is a monitor of
    # what the senders publish, so a canvas of 1080x1920 for a vertical wall
    # should give vertical preview cells without a second setting to remember.
    width, height = canvas()
    if width and height:
        return width / height
    return 16 / 9


PREVIEW_ASPECT = _preview_aspect()


# Print a timing breakdown of the render loop every N seconds. 0 is off.
#
# Exists because "the preview feels laggier" is a true and useless bug report,
# and the four things it could be - the GL context switch, mpv's render, the
# Spout copy, the preview repaint - are indistinguishable from the outside.
# Each is timed separately so the answer is a number rather than an argument.
# Set PERF_LOG_S=5 and read the log.
PERF_LOG_S = float(_num("PERF_LOG_S", "0"))


FILL_WORDS = ("max", "fit", "screen", "full")


def _scale() -> float:
    """How large the window opens, as a multiple of the standard size.

    1 is the size the window has always opened at. 2 is twice as wide, with the
    height following so the cells stay 16:9. Clamped because a typo here means
    a window you cannot reach the controls of.
    """
    raw = (_raw("WINDOW_SCALE") or "").strip().lower()
    if raw in FILL_WORDS:
        return 1.0            # handled by WINDOW_FILL_SCREEN instead
    try:
        value = float(raw or 1.0)
    except ValueError:
        return 1.0
    return min(8.0, max(0.25, value))


def _fill_screen() -> bool:
    """WINDOW_SCALE=max - as large as the screen allows, whatever that is.

    More useful than guessing a multiplier: it cannot overshoot, and it does not
    need to be changed when the app is opened on a different monitor.
    """
    return (_raw("WINDOW_SCALE") or "").strip().lower() in FILL_WORDS


WINDOW_FILL_SCREEN = _fill_screen()


# Multiplies the startup window size. This only makes the *window* bigger - the
# buttons and text stay the same size, so you get more picture rather than a
# magnified UI. For bigger controls too, set Qt's own QT_SCALE_FACTOR instead.
# The window is never opened larger than the screen it lands on.
WINDOW_SCALE = _scale()


def _window_width() -> int:
    """WINDOW_WIDTH: open at exactly this width every time. 0 is off.

    Absolute where WINDOW_SCALE is relative (K, 0.9.9.29), and the way back to
    the default size: WINDOW_WIDTH=1320. Width rather than height because the
    height already follows the width to keep both previews 16:9.
    """
    try:
        value = int(float(_num("WINDOW_WIDTH", "0")))
    except ValueError:
        return 0
    return max(0, min(16384, value))


# Overrides WINDOW_SCALE when set. Still never wider than the screen, and never
# narrower than the controls need.
WINDOW_WIDTH = _window_width()

# While the window is being dragged or resized, Windows runs its own modal move
# loop and our work queues up behind it - which once left a smear behind the
# window. With this above 0, the preview stops repainting and the render loop
# slows to DRAG_TICK_MS until this long after the last move event.
#
# OFF by default since 0.9.9.22. K, with it at 0: no trail when moving, and the
# preview keeps playing instead of freezing; and the slowdown was what made
# Spout drop to ~30fps whenever the window moved. Set e.g. 220 if a machine
# does show the trail again.
DRAG_SETTLE_MS = max(0, int(_num("DRAG_SETTLE_MS", "0")))

# Render-loop period during a drag, in ms. Normally the loop wakes every 4ms so
# a finished frame reaches Spout immediately; during a drag that is 250 wakeups
# a second competing with the move loop for the same thread. 32ms still feeds
# Spout at ~30fps while leaving the thread mostly to Windows.
DRAG_TICK_MS = max(4, int(_num("DRAG_TICK_MS", "32")))

# Stop rendering entirely while the window is being dragged - Spout included,
# frozen on the last frame until you let go. Off by default because Resolume is
# showing that output to a room. Turn it on to find out whether a smear is our
# GL work or Qt's own compositing: if the trail survives with this on, nothing
# we do to the render loop will fix it.
DRAG_PAUSE_RENDER = _flag("DRAG_PAUSE_RENDER", False)

# --- Downloading -------------------------------------------------------------
# Remember what was fetched for a given URL + quality, so loading it again skips
# yt-dlp entirely. Without this, a re-load still costs a full page extraction, a
# PO token and yt-dlp's politeness sleeps before it notices the file is there.
CACHE_MANIFEST = _flag("CACHE_MANIFEST", True)

# Cache size ceiling, in GB. Clips are never deleted while they are playing and
# never while they are fresh; past that, the least recently used go first once
# the folder exceeds this. 0 means never delete anything, which is what this did
# before there was a policy at all - a VJ set of 1080p clips is ~70MB each and
# nothing ever removed them.
CACHE_MAX_GB = float(_num("CACHE_MAX_GB", "10"))

# Never evict something touched more recently than this, in minutes. Protects
# whatever was just downloaded and whatever is on screen.
CACHE_MIN_AGE_MIN = float(_num("CACHE_MIN_AGE_MIN", "30"))

# How long a clip is kept, in hours. Past this it is deleted whether or not the
# folder is anywhere near its size limit.
#
# This is the setting that makes "build the set at home, play it at the venue"
# work: the clips survive closing the app, so the venue does not start with an
# empty cache and a re-download of everything over its wifi. 24 hours covers a
# set prepared the day before without leaving last month's clips on the disk.
# 0 means no age limit - only CACHE_MAX_GB then decides.
#
# Not to be confused with CACHE_MIN_AGE_MIN below, which is the opposite: a
# protection window, not an expiry.
CACHE_MAX_AGE_H = float(_num("CACHE_MAX_AGE_H", "24"))

# Empty the cache when the app closes. OFF by default since 0.9.8, and the
# checkbox in the status bar is the real control - this is only its starting
# position.
#
# It used to be on, for two reasons. The first no longer holds: "a VJ set leaves
# gigabytes behind" is handled continuously by CACHE_MAX_GB and CACHE_MAX_AGE_H,
# which do it without throwing away something you are about to play. The second
# still does: the folder is a plain, timestamped record of what was played. That
# is a real reason to want it gone, but it is a choice about what the tool
# should be rather than housekeeping, so it is made visibly rather than by a
# default that silently breaks preparing a set in advance.
CLEAR_CACHE_ON_EXIT = _flag("CLEAR_CACHE_ON_EXIT", False)

# Fetch the video and its audio track in one yt-dlp invocation instead of two.
# Two invocations meant extracting the page twice and minting a PO token twice,
# which was most of the wait. Turn off if a site dislikes multi-format requests.
SINGLE_PASS_AUDIO = _flag("SINGLE_PASS_AUDIO", True)

# --- Logging -----------------------------------------------------------------
# Everything the app prints also goes to a timestamped file in `logs\` beside
# the exe. Without this a tester who double-clicks the exe and hits a problem
# has nothing to send: run.bat leaves a console window behind the app, and the
# exe does not.
LOG_TO_FILE = _flag("LOG_TO_FILE", True)

# How many previous runs to keep. Old ones are deleted oldest-first when the
# app starts, after the current run's file is opened - so this is a count of
# files kept, including the one being written.
LOG_KEEP = max(1, int(_num("LOG_KEEP", "10")))

# Somewhere else to put them. Empty means `logs\` next to the app, which is
# where someone looking for a log will look.
LOG_DIR = _text("LOG_DIR", "")


# --- Spout senders while idle ------------------------------------------------
# Publish a black frame on each sender at startup, so both names appear in
# Resolume's source list before anything has been loaded.
#
# A Spout sender is registered on its first frame. Without this the names only
# appear once a clip has played, which means the person patching the show has
# to load a video into each slot first, purely so Resolume has something to
# point at - and every guide for a Spout app says "it will show up once you
# send something", which is not an answer when you are building the project the
# day before.
#
# The cost is a GPU copy of a black texture a couple of times a second while a
# slot is empty, which does not register against anything.
IDLE_SENDERS = _flag("IDLE_SENDERS", True)

# Resolution of that idle frame, and therefore the size Resolume shows for a
# slot with nothing in it. It follows the canvas, so the slot does not change
# size the moment something plays - which is the whole point of having a
# canvas. 1920x1080 only when there is no canvas to follow.
IDLE_SENDER_WIDTH, IDLE_SENDER_HEIGHT = canvas_for(1920, 1080)

# How often an idle slot re-sends its black frame, in milliseconds. It only has
# to be often enough that a receiver connecting mid-set sees black rather than
# whatever was last played; it is not a frame rate anyone is watching.
IDLE_SENDER_MS = max(50, int(_num("IDLE_SENDER_MS", "500")))

# What a slot with no video sends: before anything is loaded, after Clear, and
# while a failed download is retried. Transparent by default (0.9.9.27, K's
# request), so an empty slot in Resolume shows the layers beneath it instead
# of a black box. 0 sends opaque black, which is what every version before
# 0.9.9.27 sent after Clear. Separate from SENDER_BACKGROUND, which is only
# about the bars around a playing clip.
EMPTY_TRANSPARENT = _flag("EMPTY_TRANSPARENT", True)

# The preview shows a transparent empty slot as a dark checkerboard, the usual
# sign for "nothing here". 0 shows it black, as before 0.9.9.27. Has no effect
# when EMPTY_TRANSPARENT=0: then nothing is transparent.
PREVIEW_GRID = _flag("PREVIEW_GRID", True)


def empty_rgba() -> tuple[float, float, float, float]:
    """The clear colour of a slot with no video, as OpenGL wants it."""
    return (0.0, 0.0, 0.0, 0.0) if EMPTY_TRANSPARENT else (0.0, 0.0, 0.0, 1.0)


def show_grid() -> bool:
    """Does the preview draw the checkerboard for an empty slot?"""
    return EMPTY_TRANSPARENT and PREVIEW_GRID


# --- Misc --------------------------------------------------------------------
LOOP = _flag("LOOP", True)
