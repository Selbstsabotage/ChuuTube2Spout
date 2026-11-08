# SPDX-License-Identifier: GPL-3.0-or-later
"""One place that builds an upstream HTTP request.

There used to be two: resolver.verify() and the relay. They drifted - verify
stripped Accept-Encoding and always sent a Range, the relay did neither - which
produced the worst possible failure mode: a URL that verified successfully and
then 403'd when actually played. Any "fetch this URL as the client it was issued
for" now goes through here so that cannot happen again.
"""

from __future__ import annotations

import urllib.request

# Hop-by-hop and connection-management headers. Forwarding these upstream is
# either meaningless or actively harmful - urllib manages them itself.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
    "proxy-connection",
}

# Stripped separately because it is the likeliest single cause of a signed
# media URL being refused: the request that minted the URL did not ask for a
# compressed body, and media is already compressed.
NEGOTIATION = {"accept-encoding"}

DROP = HOP_BY_HOP | NEGOTIATION


def build_request(url: str, headers: dict | None,
                  extra: dict | None = None) -> urllib.request.Request:
    """A request carrying the identity of the client the URL was issued for."""
    req = urllib.request.Request(url)
    for key, value in (headers or {}).items():
        if key.lower() in DROP:
            continue
        req.add_header(key, value)
    for key, value in (extra or {}).items():
        if value is not None:
            req.add_header(key, value)
    return req


def minimal_headers(headers: dict | None) -> dict:
    """Just the identity, nothing else - what a plain media player would send.

    Used as a second attempt when the full header set is refused; some of
    yt-dlp's extraction-time headers make sense for the API request that
    produced the URL but not for fetching the media itself.
    """
    out = {}
    for key, value in (headers or {}).items():
        if key.lower() in ("user-agent", "cookie", "origin", "referer"):
            out[key] = value
    return out


def content_length_from_url(url: str) -> int | None:
    """googlevideo bakes the resource size into the URL as `clen`.

    Knowing it up front means never asking for a range past the end of the
    file - which these servers answer with 403 rather than 416, making it look
    like an access problem when it is an arithmetic one.
    """
    import re

    match = re.search(r"[?&]clen=(\d+)", url)
    return int(match.group(1)) if match else None
