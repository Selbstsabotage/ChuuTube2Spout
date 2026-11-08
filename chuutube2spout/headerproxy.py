# SPDX-License-Identifier: GPL-3.0-or-later
"""A loopback HTTP relay that fetches media the way the media server expects.

DORMANT as of 0.9.7, and read the next paragraph before believing anything
below it. This is reached only from MODE=stream and MODE=hybrid, both of which
refuse to start (see config.mode_problem) because yt-dlp's Python API has no PO
token provider wired into it. On the download path - the only one that works -
mpv is handed a local file and this module is never called.

It is kept rather than deleted because it is correct code solving two real
problems, and the trigger for reviving it is specific: **if the yt-dlp Python
API gains a PO token provider.** Not "when mpv's curl migration settles" - that
would fix only the second of the two problems below, and it has no date, no
trigger and no first step. Note also that the fast-first-frame benefit this was
built for is already delivered a different way, by playing the partially
downloaded file (see downloader.PARTIAL_PLAY) and by prefetch-on-paste.

Two things break a direct fetch of a signed googlevideo URL:

1. **Unbounded range requests are refused.** A player opening a URL asks for
   `Range: bytes=0-` - the whole file - and YouTube answers 403. The same URL,
   the same second, with `Range: bytes=0-2047` answers 206. This is why yt-dlp
   can download a video that no player can open: yt-dlp pulls it in windows.
   The relay does the same, transparently, so mpv can carry on believing it
   opened one long file.

2. **The headers must match the client the URL was issued for.** mpv's HTTP
   layer is mid-migration to curl and applies neither --user-agent nor
   --http-header-fields, so setting them achieves nothing.

Handing mpv a 127.0.0.1 URL solves both: we control the request completely.
Only bytes cross the relay - no parsing, no buffering beyond one socket copy.
Decode and render stay where they were, on the GPU.
"""

from __future__ import annotations

import re
import secrets
import socketserver
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .config import CHUNK_MB
from .httpu import build_request, content_length_from_url, minimal_headers

# Upstream window size. YouTube refuses open-ended range requests, so every
# fetch is bounded; 10 MB is roughly what yt-dlp uses and keeps the request
# count low without stalling a seek.
CHUNK = CHUNK_MB * 1024 * 1024
# Floor for the shrink-and-retry loop; below this the request count is worse
# than the problem it solves.
MIN_WINDOW = 512 * 1024
COPY = 1 << 16
UPSTREAM_TIMEOUT = 30

# Headers we forward from mpv upstream. Everything else we supply ourselves.
_PASS_THROUGH = ("range", "if-range")
# If mpv opens without one, ask for the whole file explicitly. The request that
# minted these URLs was a ranged one, and some edges refuse unranged fetches.
_DEFAULT_RANGE = "bytes=0-"
# Response headers mpv needs to seek properly.
_ECHO_BACK = ("content-length", "content-range", "content-type",
              "accept-ranges", "last-modified", "etag")


class _Entry:
    __slots__ = ("url", "headers", "total", "window")

    def __init__(self, url: str, headers: dict) -> None:
        self.url = url
        self.headers = headers
        # Known before the first byte is fetched for googlevideo URLs.
        self.total = content_length_from_url(url)
        # Largest window this stream has actually served, so a shrink is paid
        # for once rather than on every chunk.
        self.window = CHUNK


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ChuuTube2Spout"

    def log_message(self, *args) -> None:  # noqa: D102 - silence the console
        pass

    def do_GET(self) -> None:
        self._relay(send_body=True)

    def do_HEAD(self) -> None:
        self._relay(send_body=False)

    # -- upstream -------------------------------------------------------------

    def _open(self, entry: "_Entry", extra: dict):
        """Fetch one window, retrying once with a leaner header set on refusal.

        Some of yt-dlp's headers belong to the API call that produced the URL
        rather than to fetching the media. If the full set is refused, the
        identity alone sometimes is not.
        """
        attempts = [("full", entry.headers)]
        lean = minimal_headers(entry.headers)
        if lean and lean != entry.headers:
            attempts.append(("minimal", lean))

        last_error = None
        for label, headers in attempts:
            req = build_request(entry.url, headers, extra)
            try:
                return urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT), None
            except urllib.error.HTTPError as exc:
                last_error = exc
                body = ""
                try:
                    body = exc.read(200).decode("utf-8", "replace").strip()
                except Exception:
                    pass
                print(f"[proxy] upstream {exc.code} on {label} headers "
                      f"({_describe(dict(req.header_items()))})"
                      + (f" body: {body}" if body else ""), file=sys.stderr)
                if exc.code not in (403, 401):
                    break
            except Exception as exc:
                last_error = exc
                print(f"[proxy] upstream unreachable: {exc}", file=sys.stderr)
                break
        return None, last_error

    # -- request handling -----------------------------------------------------

    def _fetch_window(self, entry: "_Entry", start: int, end: int):
        """Fetch bytes [start, end], shrinking the window if it is refused.

        A refused window is not always an access problem - it can simply be too
        large for what the server will hand out in one go. Halving before giving
        up turns a dead stream into a slower one.
        """
        window = min(end - start + 1, entry.window)
        while True:
            resp, error = self._open(
                entry, {"Range": f"bytes={start}-{start + window - 1}"})
            if resp is not None:
                entry.window = max(window, MIN_WINDOW)
                return resp, error
            if getattr(error, "code", None) != 403 or window <= MIN_WINDOW:
                return None, error
            window = max(MIN_WINDOW, window // 2)
            print(f"[proxy] retrying with a {window // 1024} KB window",
                  file=sys.stderr)

    def _relay(self, send_body: bool) -> None:
        token = self.path.lstrip("/").split("?", 1)[0]
        entry = self.server.entries.get(token)
        if entry is None:
            self.send_error(404, "unknown stream")
            return

        wanted = parse_range(self.headers.get("Range"))
        if wanted is None:
            self.send_error(416, "invalid range")
            return
        start, end = wanted

        total = entry.total
        if total is not None and start >= total:
            self.send_error(416, "range past end of resource")
            return

        # Never ask for a byte that does not exist: these servers answer a
        # range past the end with 403, which reads as an access failure and
        # sent this project chasing ghosts for an afternoon.
        hard_last = (total - 1) if total else None
        last = end if end is not None else hard_last
        if last is not None and hard_last is not None:
            last = min(last, hard_last)

        first_end = start + CHUNK - 1
        if last is not None:
            first_end = min(first_end, last)
        upstream, error = self._fetch_window(entry, start, first_end)
        if upstream is None:
            code = getattr(error, "code", 502)
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return

        with upstream:
            if total is None:
                total = content_length_of(entry.url, upstream)
                entry.total = total
                if total and last is None:
                    last = total - 1
            ranged = self.headers.get("Range") is not None

            self.send_response(206 if ranged and total else 200)
            self.send_header("Accept-Ranges", "bytes")
            ctype = upstream.headers.get("content-type")
            if ctype:
                self.send_header("Content-Type", ctype)
            if last is not None:
                self.send_header("Content-Length", str(last - start + 1))
                if ranged and total:
                    self.send_header("Content-Range",
                                     f"bytes {start}-{last}/{total}")
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()

            if not send_body:
                return

            try:
                pos = start + self._pump(upstream)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True
                return

        if last is None:
            return
        while pos <= last:
            window_end = min(pos + CHUNK - 1, last)
            nxt, _err = self._fetch_window(entry, pos, window_end)
            if nxt is None:
                # We promised a Content-Length we can no longer deliver; the
                # only honest thing left is to drop the connection so mpv
                # notices rather than decoding silence.
                self.close_connection = True
                return
            with nxt:
                try:
                    copied = self._pump(nxt)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True
                    return
            if copied <= 0:
                self.close_connection = True
                return
            pos += copied

    def _pump(self, response) -> int:
        """Copy a response body to the client. Returns bytes written."""
        written = 0
        while True:
            buf = response.read(COPY)
            if not buf:
                return written
            self.wfile.write(buf)
            written += len(buf)


def _describe(headers: dict) -> str:
    return ", ".join(f"{k}: {v[:60]}" for k, v in headers.items())


def parse_range(value: str | None) -> tuple[int, int | None] | None:
    """"bytes=10-19" -> (10, 19); "bytes=10-" -> (10, None); absent -> (0, None).

    Returns None for anything malformed or multi-range, which we answer with a
    416 rather than guessing.
    """
    if not value:
        return 0, None
    value = value.strip()
    if not value.startswith("bytes=") or "," in value:
        return None
    spec = value[len("bytes="):].strip()
    start_text, sep, end_text = spec.partition("-")
    if not sep or not start_text.isdigit():
        return None          # suffix ranges ("bytes=-500") are not needed here
    start = int(start_text)
    if not end_text:
        return start, None
    if not end_text.isdigit():
        return None
    end = int(end_text)
    return (start, end) if end >= start else None


def content_length_of(url: str, response) -> int | None:
    """Total size of the resource, from Content-Range, or the URL's own clen.

    googlevideo URLs carry `clen=` - the full length - which lets us answer
    mpv's first request with a correct Content-Length without an extra probe.
    """
    content_range = response.headers.get("content-range")
    if content_range and "/" in content_range:
        tail = content_range.rsplit("/", 1)[1].strip()
        if tail.isdigit():
            return int(tail)
    match = re.search(r"[?&]clen=(\d+)", url)
    if match:
        return int(match.group(1))
    length = response.headers.get("content-length")
    if length and length.isdigit() and not content_range:
        return int(length)
    return None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.entries: dict[str, _Entry] = {}

    def server_bind(self) -> None:
        """Bind without the inherited reverse-DNS lookup.

        HTTPServer.server_bind calls socket.getfqdn() purely to fill in a
        cosmetic server_name. On a machine with slow or unreachable DNS that
        blocks - which would stall the first video load for no reason at all.
        """
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class HeaderProxy:
    """Publishes remote URLs as loopback URLs carrying the right headers."""

    def __init__(self) -> None:
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            self._server = _Server(("127.0.0.1", 0), _Handler)
            self._thread = threading.Thread(
                target=self._server.serve_forever, kwargs={"poll_interval": 0.2},
                name="chuutube2spout-proxy", daemon=True)
            self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    def publish(self, url: str, headers: dict) -> str:
        """Return a loopback URL that fetches `url` with `headers` attached."""
        self.start()
        token = secrets.token_urlsafe(12)
        self._server.entries[token] = _Entry(url, dict(headers or {}))
        return f"http://127.0.0.1:{self.port}/{token}"

    def forget(self, tokens) -> None:
        if not self._server:
            return
        for token in tokens:
            self._server.entries.pop(token, None)

    def shutdown(self) -> None:
        with self._lock:
            if self._server is None:
                return
            server, self._server = self._server, None
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass


_proxy = HeaderProxy()


def get_proxy() -> HeaderProxy:
    return _proxy
