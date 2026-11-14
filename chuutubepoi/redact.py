# SPDX-License-Identifier: GPL-3.0-or-later
"""Strip credentials out of log lines before they reach the console.

Verbose output is genuinely useful - it is how a missing JS runtime stopped
being a week-long mystery - but yt-dlp at `-v` prints things that are not
diagnostics:

    [debug] Invoking http downloader on "https://rr3---sn-x.googlevideo.com/
    videoplayback?...&ip=203.0.113.9&...&pot=MlmPip...&sig=AE0s2JY..."

That single line carries the user's public IP address, a PO token bound to
their session, and a signature that is a bearer credential for the media. The
predictable fate of a console log from an open-source Windows app is to be
pasted into a GitHub issue, and the person pasting it has no idea any of that
is in there.

So the values are replaced and the parameter names are kept. `ip=<redacted>`
still tells you an IP was present and which request carried it, which is all
the diagnostic value the address itself had.
"""

from __future__ import annotations

import re

# Query parameters in googlevideo/innertube URLs whose values authenticate or
# identify. `n` and `sig` are the signature challenge pair, `pot` is the PO
# token, `ip` is the user, and the rest bind the URL to one session.
SECRET_PARAMS = ("ip", "sig", "signature", "lsig", "pot", "n", "bui", "spc",
                 "sparams", "lsparams", "cpn", "key", "id", "ei", "sefc")

_PARAM = re.compile(
    r"([?&](?:" + "|".join(SECRET_PARAMS) + r")=)([^&\s\"'\\]+)", re.IGNORECASE)

# Session identifiers that travel as JSON rather than query parameters.
_JSON_FIELDS = ("visitorData", "remoteHost", "encryptedTokenJarContents",
                "rolloutToken", "deviceExperimentId", "appInstallData",
                "clickTrackingParams", "sessionId", "datasyncId")
_JSON = re.compile(
    r"(\\?\"(?:" + "|".join(_JSON_FIELDS) + r")\\?\"\s*:\s*\\?\")([^\"\\]+)")

# Whole-header leaks, if a request ever gets logged verbatim.
_HEADER = re.compile(
    r"(?im)^(\s*(?:cookie|authorization|x-goog-authuser|set-cookie)\s*:\s*).+$")

# A bare IPv4 that is not loopback or a private range. Catches the address
# where it appears outside a URL - "YouTube sees your traffic as coming from".
#
# Not straight after a "%": in a URL-encoded path, "%20" (a space) followed by
# a four-part version number reads exactly like an address. A tester unzipped
# into "ChuuTube2Spout 0.9.9.11", deno reported the path as a file:// URL, and
# "...ChuuTube2Spout%200.9.9.11/bgutil-server/..." came out of the log as
# "...ChuuTube2Spout%<redacted>/bgutil-server/..." - a mangled path in exactly
# the line someone would need to read. An address directly after a percent
# sign is not a thing that happens.
_BARE_IP = re.compile(
    r"(?<!%)\b(?!127\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|0\.)"
    r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")

MASK = "<redacted>"


def scrub(text: str) -> str:
    """Replace credentials in `text`, keeping the shape of the line intact."""
    if not text:
        return text
    text = _PARAM.sub(lambda m: m.group(1) + MASK, text)
    text = _JSON.sub(lambda m: m.group(1) + MASK, text)
    text = _HEADER.sub(lambda m: m.group(1) + MASK, text)
    text = _BARE_IP.sub(MASK, text)
    return text
