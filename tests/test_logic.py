# SPDX-License-Identifier: GPL-3.0-or-later
"""Logic tests that run without Qt, OpenGL, mpv or Windows.

    python tests/test_logic.py
"""

from __future__ import annotations

import ast
import collections
import contextlib
import ctypes
import inspect
import importlib
import io
import os
import pathlib
import re
import shutil
import socket
import sys
import sqlite3
import stat
import subprocess
import tempfile
import time
import types
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ROOT = Path(__file__).resolve().parent.parent

import stubs_bootstrap  # noqa: F401  (installs the fake modules)
import stubs

import chuutube2spout
from chuutube2spout import app as app_mod
from chuutube2spout import bootstrap
from chuutube2spout import firstrun
from chuutube2spout import headerproxy
from chuutube2spout import urlcheck
from chuutube2spout import config
from chuutube2spout import doctor
from chuutube2spout import downloader
from chuutube2spout import cookies as cookies_mod
from chuutube2spout import potprovider
from chuutube2spout.downloader import (DownloadError, LocalClip, js_runtime_arg,
                                 parse_progress, video_format)
from chuutube2spout import player as player_mod
from chuutube2spout import redact
from chuutube2spout import resolver
from chuutube2spout.urlcheck import _normalise
from chuutube2spout import headerproxy
from chuutube2spout.headerproxy import HeaderProxy, get_proxy, parse_range
from chuutube2spout.player import Player, mpv_list, mpv_quote
from chuutube2spout.resolver import ResolveError, Stream, is_direct, pick_audio, pick_video
from chuutube2spout import theme
from chuutube2spout import spout_out
from chuutube2spout.spout_out import Sender, SpoutOutput
from PySide6.QtCore import Qt
from chuutube2spout.surface import PreviewSurface, _fit
from chuutube2spout import ui as ui_mod
from chuutube2spout import surface as surface_mod
from chuutube2spout import logfile
from chuutube2spout.ui import MainWindow, fmt_time


@contextlib.contextmanager
def patched(module, name, value):
    """Swap a module-level constant for the duration of one test.

    The config values are read into module globals at import, so a test that
    wants a different chain has to set the global, not the config file.
    """
    saved = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, saved)


@contextlib.contextmanager
def audio_enabled():
    """FETCH_AUDIO is off by default; these cases are about the audio path."""
    saved = (player_mod.FETCH_AUDIO, downloader.FETCH_AUDIO)
    player_mod.FETCH_AUDIO = downloader.FETCH_AUDIO = True
    try:
        yield
    finally:
        player_mod.FETCH_AUDIO, downloader.FETCH_AUDIO = saved


# The suite must never shell out to deno. script_probe would run the real
# generator and install_script_deps would spend a minute building node_modules -
# once per MainWindow, and the tests build a lot of them. The real functions are
# kept so ScriptProbeTests can still exercise them directly.
_REAL_SCRIPT_PROBE = potprovider.script_probe
_REAL_INSTALL_DEPS = potprovider.install_script_deps
potprovider.script_probe = lambda home: (True, "stubbed for tests")
potprovider.install_script_deps = lambda home: (True, "")
# Same rule for the two things 0.9.9.12 added. A successful (stubbed) probe
# now starts the real token server, and the generator can now live under
# %LOCALAPPDATA% - neither may happen from a test on a developer's machine.
_REAL_AFTER_READY = potprovider.PotProvider._after_ready
_REAL_LOCAL_SERVER_ROOT = potprovider.local_server_root
potprovider.PotProvider._after_ready = lambda self: None
potprovider.local_server_root = lambda: None


class MpvQuotingTests(unittest.TestCase):
    """mpv list options are comma-separated; header values and signed URLs are
    full of commas. Getting the %LENGTH% escape wrong truncates them silently."""

    def test_length_prefix_counts_bytes(self):
        self.assertEqual(mpv_quote("abc"), "%3%abc")

    def test_value_with_commas_survives_intact(self):
        value = "User-Agent: Mozilla/5.0 (X11; Linux, x86_64)"
        out = mpv_quote(value)
        declared = int(out.split("%")[1])
        self.assertEqual(out.split("%", 2)[2], value)
        self.assertEqual(declared, len(value))

    def test_non_ascii_measured_in_bytes_not_characters(self):
        self.assertEqual(mpv_quote("\u00e9"), "%2%\u00e9")

    def test_list_joins_quoted_items(self):
        self.assertEqual(mpv_list(["a", "bb"]), "%1%a,%2%bb")

    def test_empty_list_is_empty_string(self):
        self.assertEqual(mpv_list([]), "")


class FitTests(unittest.TestCase):
    def test_wide_video_in_square_cell_letterboxes(self):
        x0, y0, x1, y1 = _fit((0, 0, 400, 400), 1920, 1080)
        self.assertEqual(x1 - x0, 400)
        self.assertEqual(y1 - y0, 225)
        self.assertEqual(y0, (400 - 225) // 2)   # vertically centred

    def test_tall_video_in_wide_cell_pillarboxes(self):
        x0, y0, x1, y1 = _fit((0, 0, 400, 200), 1080, 1920)
        self.assertEqual(y1 - y0, 200)
        self.assertEqual(x1 - x0, 112)
        self.assertEqual(x0, (400 - 112) // 2)

    def test_respects_cell_offset(self):
        x0, _, x1, _ = _fit((500, 0, 400, 400), 1000, 1000)
        self.assertEqual((x0, x1), (500, 900))

    def test_never_exceeds_the_cell(self):
        for vw, vh in ((3840, 2160), (640, 480), (1, 1000), (1000, 1)):
            x0, y0, x1, y1 = _fit((10, 20, 300, 150), vw, vh)
            self.assertLessEqual(x1 - x0, 300)
            self.assertLessEqual(y1 - y0, 150)
            self.assertGreaterEqual(x0, 10)
            self.assertGreaterEqual(y0, 20)

    def test_degenerate_input_does_not_explode(self):
        self.assertEqual(_fit((0, 0, 100, 100), 0, 0), (0, 0, 100, 100))


class TimeFormatTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(fmt_time(0), "0:00")
        self.assertEqual(fmt_time(9), "0:09")
        self.assertEqual(fmt_time(65), "1:05")
        self.assertEqual(fmt_time(3661), "1:01:01")

    def test_handles_none_and_negative(self):
        self.assertEqual(fmt_time(None), "0:00")
        self.assertEqual(fmt_time(-5), "0:00")


class _RecordingSender(Sender):
    def __init__(self, name):
        super().__init__(name)
        self.sent = []

    def send(self, texture_id, width, height, invert):
        if not self._enabled:
            return False
        self.sent.append((texture_id, width, height, invert))
        return True


class _FakeTarget:
    def __init__(self, tex, w=1920, h=1080):
        self.tex, self.w, self.h = tex, w, h

    @property
    def valid(self):
        return self.tex > 0


class SpoutRoutingTests(unittest.TestCase):
    def setUp(self):
        self.out = SpoutOutput()
        self.out.players = [_RecordingSender("A"), _RecordingSender("B")]
        self.targets = [_FakeTarget(11), _FakeTarget(22)]

    def test_both_players_publish_every_frame(self):
        self.out.publish(self.targets, False)
        self.assertEqual(self.out.players[0].sent[-1][0], 11)
        self.assertEqual(self.out.players[1].sent[-1][0], 22)

    def test_invalid_target_is_skipped_not_crashed(self):
        self.out.publish([_FakeTarget(0), self.targets[1]], False)
        self.assertEqual(self.out.players[0].sent, [])
        self.assertEqual(len(self.out.players[1].sent), 1)

    def test_only_changed_slots_are_sent(self):
        # A Spout send is a GPU copy on the GUI thread. Repeating it for a frame
        # that has not moved is what made the interface treacly.
        self.out.publish(self.targets, False, changed=[True, False])
        self.assertEqual(len(self.out.players[0].sent), 1)
        self.assertEqual(self.out.players[1].sent, [])

    def test_omitting_the_change_list_sends_everything(self):
        self.out.publish(self.targets, False)
        self.assertEqual(len(self.out.players[0].sent), 1)
        self.assertEqual(len(self.out.players[1].sent), 1)

    def test_two_senders_and_no_third(self):
        # The program sender and its LIVE button were removed in 0.9.9.20.
        self.assertEqual(SpoutOutput().names(),
                         [config.SENDER_A, config.SENDER_B])
        self.assertFalse(hasattr(SpoutOutput(), "program"))


class _SyncThread:
    """Runs a target inline so the async load path is deterministic."""

    def __init__(self, target=None, name=None, daemon=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _InlineThreading:
    """Stands in for the threading module inside player.py only."""

    Thread = _SyncThread


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def _compatible(value):
    """Force the plugin/server compatibility verdict for a test."""
    real = potprovider.PotProvider.compatible
    potprovider.PotProvider.compatible = lambda self: value
    try:
        yield
    finally:
        potprovider.PotProvider.compatible = real


@contextlib.contextmanager
def _listening(port):
    """A socket on `port`, standing in for an already-running token server."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(5)
    try:
        yield server
    finally:
        server.close()


@contextlib.contextmanager
def _window_limits(chunk, floor):
    """Shrink both the window and its floor so shrink-retry is testable."""
    original = (headerproxy.CHUNK, headerproxy.MIN_WINDOW)
    headerproxy.CHUNK, headerproxy.MIN_WINDOW = chunk, floor
    try:
        yield
    finally:
        headerproxy.CHUNK, headerproxy.MIN_WINDOW = original


@contextlib.contextmanager
def _chunk_size(size):
    """Shrink the upstream window so tests exercise real multi-window fetches."""
    original = headerproxy.CHUNK
    headerproxy.CHUNK = size
    try:
        yield
    finally:
        headerproxy.CHUNK = original


def _stream(**kw):
    base = dict(video_url="https://media/v", audio_url="https://media/a",
                headers={"User-Agent": "TestAgent/1.0", "Accept-Language": "en"},
                width=1920, height=1080, codec="avc1", client="tv")
    base.update(kw)
    return Stream(**base)


class PlayerLoadTests(unittest.TestCase):
    def setUp(self):
        self.p = Player("A")
        # Swap the module reference only - patching threading.Thread itself
        # would also hijack the proxy's own server thread and deadlock.
        self._real_threading = player_mod.threading
        player_mod.threading = _InlineThreading
        # The relay has its own tests; here we want the raw URLs and headers.
        self._real_proxy_flag = player_mod.USE_PROXY
        player_mod.USE_PROXY = False
        # These cover the streaming path specifically; download mode has its own.
        self._real_mode = player_mod.MODE
        player_mod.MODE = "stream"
        self.resolved = _stream()
        player_mod.resolve = lambda url, height, progress=None: self.resolved

    def tearDown(self):
        player_mod.threading = self._real_threading
        player_mod.USE_PROXY = self._real_proxy_flag
        player_mod.MODE = self._real_mode

    def test_starts_muted(self):
        self.assertEqual(self.p.mpv.options["mute"], "yes")

    def test_loops_by_default(self):
        self.assertEqual(self.p.mpv.options["loop_file"], "inf")

    def test_mpvs_own_ytdl_hook_is_disabled(self):
        # Two resolvers fighting over one URL is how you get mangled requests.
        self.assertFalse(self.p.mpv.options["ytdl"])

    def test_load_hands_mpv_the_resolved_url_not_the_page_url(self):
        self.p.load("https://www.youtube.com/watch?v=abc")
        self.assertEqual(self.p.mpv.loads[-1][0], "https://media/v")

    def test_headers_are_split_into_user_agent_and_the_rest(self):
        self.p.load("https://www.youtube.com/watch?v=abc")
        self.assertEqual(self.p.mpv.props["user-agent"], "TestAgent/1.0")
        fields = self.p.mpv.props["http-header-fields"]
        self.assertIn("Accept-Language: en", fields)
        # The UA must not be duplicated into the generic header list.
        self.assertFalse(any("User-Agent" in f for f in fields))

    def test_hop_by_hop_headers_are_not_forwarded(self):
        self.resolved = _stream(headers={"User-Agent": "X", "Accept-Encoding": "gzip",
                                         "Host": "evil", "Cookie": "k=v"})
        self.p.load("https://www.youtube.com/watch?v=abc")
        fields = self.p.mpv.props["http-header-fields"]
        self.assertEqual(fields, ["Cookie: k=v"])

    def test_separate_audio_track_is_attached(self):
        self.enterContext(audio_enabled())
        self.p.load("https://www.youtube.com/watch?v=abc")
        self.assertEqual(self.p.mpv.props["audio-files"], ["https://media/a"])

    def test_muxed_stream_attaches_no_audio_file(self):
        self.resolved = _stream(audio_url=None)
        self.p.load("https://www.youtube.com/watch?v=abc")
        self.assertEqual(self.p.mpv.props["audio-files"], [])

    def test_start_offset_is_cleared_for_a_fresh_load(self):
        self.p.load("https://www.youtube.com/watch?v=abc")
        self.assertEqual(self.p.mpv.props["start"], "none")

    def test_quality_change_reloads_at_the_same_position(self):
        self.p.load("https://www.youtube.com/watch?v=abc")
        self.p.mpv.props["time-pos"] = 42.0
        self.p.set_quality(720)
        self.assertEqual(self.p.mpv.props["start"], "42.000")
        self.assertEqual(self.p.mpv.loads[-1][0], "https://media/v")

    def test_quality_change_without_a_url_does_not_load(self):
        before = len(self.p.mpv.loads)
        self.p.set_quality(360)
        self.assertEqual(len(self.p.mpv.loads), before)

    def test_setting_the_same_quality_is_a_no_op(self):
        self.p.load("https://www.youtube.com/watch?v=abc")
        before = len(self.p.mpv.loads)
        self.p.set_quality(self.p.quality)
        self.assertEqual(len(self.p.mpv.loads), before)

    def test_a_superseded_resolve_is_discarded(self):
        # Slow resolve for URL 1 must not clobber a later load of URL 2.
        self.p.load("https://www.youtube.com/watch?v=one")
        stale_token = self.p._load_token
        self.p.load("https://www.youtube.com/watch?v=two")
        before = len(self.p.mpv.loads)
        self.p._on_resolved(stale_token, _stream(video_url="https://media/STALE"))
        self.assertEqual(len(self.p.mpv.loads), before)

    def test_failure_reports_the_reason(self):
        seen = []
        self.p.statusChanged.connect(seen.append)
        def boom(url, height, progress=None):
            raise ResolveError("every client refused (403)")
        player_mod.resolve = boom
        self.p.load("https://www.youtube.com/watch?v=abc")
        self.assertIn("403", seen[-1])
        self.assertEqual(self.p.mpv.loads, [])

    def test_stop_orphans_an_in_flight_resolve(self):
        self.p.load("https://www.youtube.com/watch?v=abc")
        token = self.p._load_token
        self.p.stop()
        before = len(self.p.mpv.loads)
        self.p._on_resolved(token, _stream())
        self.assertEqual(len(self.p.mpv.loads), before)

    def test_size_observers_drive_the_render_target(self):
        seen = []
        self.p.sizeChanged.connect(lambda w, h: seen.append((w, h)))
        self.p.mpv.fire("dwidth", 1920)
        self.p.mpv.fire("dheight", 1080)
        self.assertEqual(seen[-1], (1920, 1080))

    def test_seek_uses_exact_on_demand(self):
        self.p.seek(12.5, exact=True)
        self.p.seek(12.5, exact=False)
        self.assertEqual(self.p.mpv.seeks[0], (12.5, "absolute", "exact"))
        self.assertEqual(self.p.mpv.seeks[1], (12.5, "absolute", "keyframes"))

    def test_empty_url_is_ignored(self):
        self.p.load("   ")
        self.assertEqual(self.p.mpv.loads, [])


def _fmt(height=None, vcodec="avc1.4d", acodec="none", tbr=1000, url="u"):
    return {"url": url, "height": height, "vcodec": vcodec,
            "acodec": acodec, "tbr": tbr}


class FormatSelectionTests(unittest.TestCase):
    """'the selected quality, or the next highest' - literally."""

    def setUp(self):
        self.formats = [_fmt(360), _fmt(720), _fmt(1080), _fmt(2160),
                        _fmt(None, vcodec="none", acodec="opus", tbr=130)]

    def test_exact_match_wins(self):
        self.assertEqual(pick_video(self.formats, 1080)["height"], 1080)

    def test_picks_the_best_below_the_ceiling(self):
        self.assertEqual(pick_video(self.formats, 900)["height"], 720)

    def test_steps_up_when_nothing_fits(self):
        # Only 360p and above exist; asking for 240p should not fail.
        self.assertEqual(pick_video(self.formats, 240)["height"], 360)

    def test_no_ceiling_takes_the_best(self):
        self.assertEqual(pick_video(self.formats, None)["height"], 2160)

    def test_prefers_the_configured_codec_at_equal_height(self):
        formats = [_fmt(1080, vcodec="av01.0"), _fmt(1080, vcodec="avc1.4d")]
        self.assertTrue(pick_video(formats, 1080)["vcodec"].startswith("avc1"))

    def test_audio_only_formats_are_never_chosen_as_video(self):
        audio_only = [_fmt(None, vcodec="none", acodec="opus")]
        self.assertIsNone(pick_video(audio_only, 1080))

    def test_picks_the_richest_audio(self):
        formats = [{"url": "a", "vcodec": "none", "acodec": "opus", "abr": 70},
                   {"url": "b", "vcodec": "none", "acodec": "opus", "abr": 160}]
        self.assertEqual(pick_audio(formats)["url"], "b")

    def test_no_audio_stream_is_not_an_error(self):
        self.assertIsNone(pick_audio([_fmt(1080)]))


class DownloadFormatTests(unittest.TestCase):
    """Same "or the next highest" promise as the streaming path, expressed as a
    yt-dlp selector because yt-dlp is doing the choosing here."""

    def test_the_ceiling_is_the_only_thing_in_the_selector(self):
        fmt = video_format(1080)
        self.assertTrue(fmt.startswith("bestvideo[height<=?1080]"))

    def test_codec_is_never_a_veto_in_the_selector(self):
        # THE 480p BUG. `[vcodec^=avc1]` used to lead the expression, so H.264
        # at any height beat everything else at any height. A video whose avc1
        # ladder stopped at 480p played at 480p with 1080p VP9 and AV1 sitting
        # unused in the same format list.
        for height in (None, 480, 720, 1080, 2160):
            self.assertNotIn("vcodec", video_format(height),
                             "codec belongs in FORMAT_SORT, not in -f")

    def test_codec_preference_survives_as_a_tie_break(self):
        # Still H.264 where H.264 exists at the chosen resolution - resolution
        # is simply asked first.
        self.assertEqual(downloader.FORMAT_SORT, "res,vcodec:h264")
        self.assertTrue(downloader.FORMAT_SORT.startswith("res"))

    def test_the_sort_is_actually_passed_to_yt_dlp(self):
        # A sort that never reaches the command line is a comment.
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count('"-f", fmt, "-S", FORMAT_SORT,'), 2,
                         "both the fresh and the cached-description paths")

    def test_falls_back_past_the_ceiling_rather_than_failing(self):
        self.assertTrue(video_format(480).endswith("bestvideo/best"))

    def test_no_ceiling_asks_for_the_best_there_is(self):
        self.assertEqual(video_format(None), "bestvideo/best")

    def test_asks_for_video_only_so_no_ffmpeg_merge_is_needed(self):
        # Merging video+audio would require ffmpeg; mpv plays them separately.
        self.assertNotIn("+bestaudio", video_format(1080))


class CacheFilenameTests(unittest.TestCase):
    """Clips were named by video id alone, which is unreadable in a folder.

    Half of what this plays has a Japanese title, so the rules that matter are
    about length and legal characters, not about ASCII."""

    def test_the_title_comes_first_then_what_the_name_used_to_be(self):
        self.assertTrue(downloader.OUT_TEMPLATE.startswith("%(title)"))
        self.assertIn("%(id)s", downloader.OUT_TEMPLATE)
        self.assertIn("f%(format_id)s", downloader.OUT_TEMPLATE)
        self.assertTrue(downloader.OUT_TEMPLATE.endswith(".%(ext)s"))

    def test_the_title_is_truncated(self):
        # MAX_PATH, not tidiness. A cache folder a few directories deep plus a
        # full title reaches 260 characters and Windows refuses the write.
        self.assertRegex(downloader.OUT_TEMPLATE, r"%\(title\)\.\d+s")

    def test_the_id_survives_so_two_uploads_cannot_collide(self):
        # Titles repeat across uploads far more often than ids do.
        head = downloader.OUT_TEMPLATE.split("%(id)s")[0]
        self.assertIn("%(title)", head)

    def test_windows_sanitising_is_asked_for_explicitly(self):
        # The default only ON Windows. Being explicit means a build run from
        # anywhere produces the same names.
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count('"--windows-filenames"'), 2)

    def test_non_ascii_titles_are_never_flattened(self):
        # --restrict-filenames would turn every Japanese title into underscores
        # and leave a folder of clips nobody can tell apart.
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        # As an argument, not as the comment explaining why it is absent.
        self.assertNotIn('"--restrict-filenames"', source)


class DuplicateFetchTests(unittest.TestCase):
    """One URL, one download. The guard used to run in one direction only.

    load() has always yielded to a prefetch already in flight. prefetch() knew
    nothing about a load, so pressing Load inside the paste debounce started a
    second yt-dlp on the same output path - and --no-part means both processes
    are writing the same filename. Seen on a 560 MB clip, fetched twice."""

    def setUp(self):
        self.p = Player("A")

    def test_a_prefetch_stands_down_for_a_load_of_the_same_clip(self):
        url = "https://www.youtube.com/watch?v=IunYWyKl4hI"
        self.p._loading_key = (url, self.p._quality)
        self.p.prefetch(url)
        self.assertIsNone(self.p._job, "started a second fetch for one URL")

    def test_a_prefetch_of_a_different_clip_still_starts(self):
        self.p._loading_key = ("https://www.youtube.com/watch?v=aaaaaaaaaaa",
                               self.p._quality)
        self.p.prefetch("https://www.youtube.com/watch?v=bbbbbbbbbbb")
        self.assertIsNotNone(self.p._job)

    def test_the_claim_is_taken_before_the_thread_starts(self):
        # The debounce fires about a second after Load. Claiming inside the
        # worker would leave exactly that gap open.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source[source.index("def load(self"):source.index("def _hybrid")]
        self.assertLess(body.index("self._loading_key = (url, height)"),
                        body.index("threading.Thread("))

    def test_the_claim_is_released_even_when_the_load_fails(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source[source.index("def load(self"):source.index("def _hybrid")]
        self.assertIn("finally:", body)
        self.assertIn("self._loading_key = None", body)


class TransportGlyphTests(unittest.TestCase):
    """The pause button rendered as a colour emoji and looked stuck on."""

    def test_both_glyphs_ask_for_text_presentation(self):
        # U+FE0E. Without it Windows draws Segoe UI Emoji's own blue, which no
        # stylesheet colour can override.
        self.assertTrue(ui_mod.PLAY_GLYPH.endswith("\ufe0e"))
        self.assertTrue(ui_mod.PAUSE_GLYPH.endswith("\ufe0e"))

    def test_the_button_is_tagged_so_the_stylesheet_can_reach_it(self):
        w = MainWindow()
        self.assertEqual(w.panels[0].play_btn.property("role"), "transport")

    def test_it_is_not_the_colour_that_means_live(self):
        from chuutube2spout import theme
        self.assertIn('QPushButton[role="transport"]', theme.STYLESHEET)
        block = theme.STYLESHEET.split('QPushButton[role="transport"]')[1]
        block = block[:block.index("}")]
        self.assertIn(theme.WARN, block)
        self.assertNotIn(theme.ACCENT, block)


class ChildEncodingTests(unittest.TestCase):
    """A Japanese title in the cache silently disabled partial playback.

    Windows pipes default to the ANSI code page. yt-dlp's own debug line says
    `out cp1252`, so the Destination: line it printed had the brackets and the
    emoji stripped out before this end ever saw them - a path that does not
    exist, and therefore no early playback and no error. The file on disk was
    always correct, because --print-to-file writes UTF-8."""

    def test_the_child_is_told_to_speak_utf8(self):
        env = downloader._child_env()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUTF8"], "1")

    def test_the_rest_of_the_environment_survives(self):
        # PATH is how the subprocess finds deno and the plugins.
        env = downloader._child_env()
        for key in os.environ:
            if key not in ("PYTHONIOENCODING", "PYTHONUTF8"):
                self.assertIn(key, env)

    def test_yt_dlp_is_actually_launched_with_it(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        body = source[source.index("proc = subprocess.Popen("):]
        self.assertIn("env=_child_env()", body[:400])

    def test_a_destination_we_cannot_find_is_reported(self):
        # The failure that hid: no handover, no explanation.
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        body = source[source.index("if line.startswith(_DESTINATION):"):]
        self.assertIn("cannot see the destination folder", body[:600])


class PlayButtonSyncTests(unittest.TestCase):
    """The glyph disagreed with the player for the whole first clip.

    observe_property fires on a CHANGE. A clip that loads unpaused never
    changes `pause`, so no event arrived and the button kept the play arrow it
    was constructed with. The first click then looked inert: it set pause=True
    while the glyph was already showing play."""

    def setUp(self):
        self.w = MainWindow()
        self.panel = self.w.panels[0]

    def test_the_glyph_is_pushed_when_a_load_finishes(self):
        self.panel.play_btn.setText(ui_mod.PLAY_GLYPH)
        self.panel._on_loading(False)
        self.assertEqual(self.panel.play_btn.text(),
                         ui_mod.PLAY_GLYPH if self.panel.player.paused
                         else ui_mod.PAUSE_GLYPH)

    def test_nothing_is_pushed_while_still_loading(self):
        # Mid-load there is no clip to describe yet.
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source[source.index("def _on_loading"):]
        self.assertIn("if not loading:", body[:200])

    def test_the_sync_asks_the_player_rather_than_guessing(self):
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source[source.index("def _sync_play_button"):]
        self.assertIn("self.player.paused", body[:700])


class ProgressTextTests(unittest.TestCase):
    def test_reads_a_percentage_line(self):
        self.assertEqual(
            parse_progress("[download]  45.2% of   60.75MiB at    8.00MiB/s ETA 00:02"),
            "downloading 45% of 60.75MiB at 8.00MiB/s")

    def test_handles_an_estimated_size(self):
        text = parse_progress("[download]   1.0% of ~ 12.00MiB at 1.00MiB/s")
        self.assertEqual(text, "downloading 1% of 12.00MiB at 1.00MiB/s")

    def test_speed_is_optional(self):
        self.assertEqual(parse_progress("[download] 100.0% of 5.00MiB"),
                         "downloading 100% of 5.00MiB")

    def test_recognises_an_already_cached_clip(self):
        self.assertEqual(
            parse_progress("[download] /cache/x.mp4 has already been downloaded"),
            "cached")

    def test_says_nothing_about_unrelated_output(self):
        for line in ("[youtube] Extracting URL: x", "[info] Downloading 1 format",
                     "WARNING: something"):
            self.assertIsNone(parse_progress(line), line)


class JsRuntimeArgTests(unittest.TestCase):
    """Answering "is deno actually being used?" rather than assuming."""

    def test_points_at_deno_when_present(self):
        real = downloader.shutil.which
        downloader.shutil.which = lambda name: "C:/deno/deno.exe"
        try:
            self.assertEqual(js_runtime_arg(),
                             ["--js-runtimes", "deno:C:/deno/deno.exe"])
        finally:
            downloader.shutil.which = real

    def test_says_nothing_when_absent(self):
        real = downloader.shutil.which
        downloader.shutil.which = lambda name: None
        try:
            self.assertEqual(js_runtime_arg(), [])
        finally:
            downloader.shutil.which = real


class YtdlpSubprocessTests(unittest.TestCase):
    """Runs the real subprocess plumbing against a stand-in yt-dlp.

    This layer swallowed yt-dlp's own warnings for several rounds, so it is
    exercised for real rather than mocked."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="chuutube-fake-")
        source = Path(__file__).resolve().parent / "fake_ytdlp.py"
        cls.tools = {}
        for name in ("good_ytdlp", "failing_ytdlp", "old_ytdlp",
                     "muxed_ytdlp", "noaudio_ytdlp"):
            path = Path(cls.dir) / f"{name}.py"
            shutil.copy(source, path)
            os.chmod(path, 0o755)
            cls.tools[name] = str(path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="chuutube-cache-")
        self._real_find = downloader.ytdlp_command

    def tearDown(self):
        downloader.ytdlp_command = self._real_find
        shutil.rmtree(self.cache, ignore_errors=True)

    def _use(self, name):
        # Through the interpreter, not directly: a .py file with a shebang is
        # executable on Linux and "not a valid Win32 application" on Windows.
        downloader.ytdlp_command = lambda *a, **k: [sys.executable, self.tools[name]]

    def test_downloads_and_reports_the_file(self):
        self._use("good_ytdlp")
        clip = downloader.download("https://example/watch", 1080, self.cache)
        self.assertTrue(Path(clip.video_path).is_file())
        self.assertEqual((clip.height, clip.width), (1080, 1920))
        self.assertEqual(clip.title, "A Clip")

    def test_progress_reaches_the_caller(self):
        self._use("good_ytdlp")
        seen = []
        downloader.download("https://example/watch", 1080, self.cache,
                            progress=seen.append)
        self.assertTrue(any("downloading 50%" in m for m in seen), seen)

    def test_a_403_without_a_token_server_names_the_real_cause(self):
        # "Forbidden" is true but useless; the actionable fact is the missing
        # token server, so that is what the user is told.
        #
        # get_provider() is a module singleton, and any earlier test that built
        # a MainWindow has already start()ed it - so on a machine with a real
        # install this asserted against a provider that WAS active. Say what
        # this test is about instead of inheriting it.
        self._use("failing_ytdlp")
        real = downloader.get_provider

        class _Gated:
            active = False
            running = False

            def extractor_args(self):
                return []

        downloader.get_provider = lambda: _Gated()
        try:
            with self.assertRaises(DownloadError) as caught:
                downloader.download("https://example/watch", 1080, self.cache)
        finally:
            downloader.get_provider = real
        message = str(caught.exception)
        self.assertIn("403", message)
        self.assertIn("PO token", message)
        self.assertIn("setup.ps1", message)

    def test_other_failures_surface_yt_dlps_own_words(self):
        self._use("failing_ytdlp")
        real = downloader.get_provider
        class _Running:
            running = True
            active = True
            def extractor_args(self):
                return []
        downloader.get_provider = lambda: _Running()
        try:
            with self.assertRaises(DownloadError) as caught:
                downloader.download("https://example/watch", 1080, self.cache)
        finally:
            downloader.get_provider = real
        self.assertIn("unable to download video data", str(caught.exception))

    def test_retries_without_the_js_flag_on_an_older_yt_dlp(self):
        self._use("old_ytdlp")
        real = downloader.shutil.which
        downloader.shutil.which = lambda name: "/usr/bin/deno"
        try:
            clip = downloader.download("https://example/watch", 1080, self.cache)
        finally:
            downloader.shutil.which = real
        self.assertTrue(Path(clip.video_path).is_file())

    def test_every_strategy_is_tried_before_giving_up(self):
        # A single client failing must not end the attempt: YouTube keeps
        # moving which one works, so the fallback chain is the whole point.
        self._use("failing_ytdlp")
        seen = []
        real = downloader._strategies
        downloader._strategies = lambda: [("a", []), ("b", []), ("c", [])]
        real_run = downloader.subprocess.Popen
        try:
            with self.assertRaises(DownloadError):
                downloader.download("https://example/watch", 1080, self.cache,
                                    progress=seen.append)
        finally:
            downloader._strategies = real
        tried = [m for m in seen if m.startswith("fetching (")]
        self.assertEqual(len(tried), 3, tried)

    def test_a_later_strategy_can_still_succeed(self):
        real = downloader._strategies
        downloader._strategies = lambda: [("doomed", []), ("works", [])]
        tools = self.tools
        calls = {"n": 0}
        original_command = downloader._command

        def command(ytdlp, url, fmt, cache, print_file, extra, **kwargs):
            calls["n"] += 1
            which = tools["failing_ytdlp"] if calls["n"] == 1 else tools["good_ytdlp"]
            # A list, via the interpreter: same reason as _use() above.
            return original_command([sys.executable, which], url, fmt, cache,
                                    print_file, extra, **kwargs)

        downloader._command = command
        self._use("failing_ytdlp")
        try:
            clip = downloader.download("https://example/watch", 1080, self.cache)
        finally:
            downloader._strategies = real
            downloader._command = original_command
        self.assertTrue(Path(clip.video_path).is_file())

    def test_a_muxed_format_is_not_given_a_second_audio_track(self):
        # Format 18 already carries audio; attaching an external track on top
        # would leave mpv with two audio streams to choose between.
        self._use("muxed_ytdlp")
        clip = downloader.download("https://example/watch", 1080, self.cache)
        self.assertTrue(clip.has_audio)
        self.assertIsNone(clip.audio_path)

    def test_video_and_audio_arrive_in_one_invocation(self):
        self.enterContext(audio_enabled())
        # Two invocations meant extracting the page twice and minting two PO
        # tokens, which was most of the wait before a clip appeared.
        self._use("good_ytdlp")
        calls = {"n": 0}
        original = downloader._command
        def counted(*a, **k):
            calls["n"] += 1
            return original(*a, **k)
        downloader._command = counted
        try:
            clip = downloader.download("https://example/watch", 1080, self.cache)
        finally:
            downloader._command = original
        self.assertFalse(clip.has_audio)
        self.assertIsNotNone(clip.audio_path)
        self.assertTrue(Path(clip.audio_path).is_file())
        self.assertEqual(calls["n"], 1, "ran a second yt-dlp pass")

    def test_the_video_still_arrives_when_there_is_no_separate_audio(self):
        # A refused multi-format request must not work through the whole client
        # chain first - that is five slow ways of hearing the same thing.
        self._use("noaudio_ytdlp")
        seen = []
        clip = downloader.download("https://example/watch", 1080, self.cache,
                                   progress=seen.append)
        self.assertTrue(Path(clip.video_path).is_file())
        self.assertIsNone(clip.audio_path)
        tried = [m for m in seen if m.startswith("fetching (")]
        self.assertLessEqual(len(tried), 2, tried)

    def test_a_second_load_of_the_same_url_skips_yt_dlp(self):
        self._use("good_ytdlp")
        first = downloader.download("https://example/watch", 1080, self.cache)
        downloader.ytdlp_command = lambda *a, **k: None      # any call now fails loudly
        again = downloader.download("https://example/watch", 1080, self.cache)
        self.assertEqual(again.video_path, first.video_path)
        self.assertEqual(again.audio_path, first.audio_path)
        self.assertEqual(again.height, 1080)

    def test_a_different_quality_is_fetched_rather_than_reused(self):
        self._use("good_ytdlp")
        downloader.download("https://example/watch", 1080, self.cache)
        downloader.ytdlp_command = lambda *a, **k: None
        with self.assertRaises(DownloadError):
            downloader.download("https://example/watch", 480, self.cache)

    def test_a_manifest_pointing_at_deleted_files_is_ignored(self):
        self._use("good_ytdlp")
        clip = downloader.download("https://example/watch", 1080, self.cache)
        Path(clip.video_path).unlink()
        again = downloader.download("https://example/watch", 1080, self.cache)
        self.assertTrue(Path(again.video_path).is_file())

    def test_missing_yt_dlp_is_a_clear_message(self):
        downloader.ytdlp_command = lambda *a, **k: None
        with self.assertRaises(DownloadError) as caught:
            downloader.download("https://example/watch", 1080, self.cache)
        self.assertIn("setup.ps1", str(caught.exception))

class DownloadModeTests(unittest.TestCase):
    """The default path: yt-dlp fetches, mpv plays a local file."""

    def setUp(self):
        self.p = Player("A")
        self._real_threading = player_mod.threading
        player_mod.threading = _InlineThreading
        self._real_mode = player_mod.MODE
        player_mod.MODE = "download"
        self.clip = LocalClip(video_path="C:/cache/vid.mp4",
                              audio_path="C:/cache/aud.m4a",
                              height=1080, title="A Clip")
        # **kwargs, not a fixed list: download() grew on_partial and these
        # fakes silently became the wrong shape, which showed up as four
        # unrelated-looking IndexErrors.
        player_mod.download = lambda url, h, cache, **kw: self.clip

    def tearDown(self):
        player_mod.threading = self._real_threading
        player_mod.MODE = self._real_mode
        get_proxy().shutdown()

    def test_mpv_is_given_the_downloaded_file(self):
        self.p.load("https://www.youtube.com/watch?v=abc")
        self.assertEqual(self.p.mpv.loads[-1][0], "C:/cache/vid.mp4")

    def test_the_separate_audio_file_is_attached(self):
        self.enterContext(audio_enabled())
        self.p.load("https://www.youtube.com/watch?v=abc")
        self.assertEqual(self.p.mpv.props["audio-files"], ["C:/cache/aud.m4a"])

    def test_a_local_file_is_never_sent_through_the_relay(self):
        self.p.load("https://www.youtube.com/watch?v=abc")
        self.assertFalse(self.p.mpv.loads[-1][0].startswith("http://127.0.0.1"))
        self.assertEqual(self.p._proxy_tokens, [])

    def test_video_without_separate_audio_is_fine(self):
        self.clip = LocalClip(video_path="C:/cache/vid.mp4", audio_path=None)
        self.p.load("https://www.youtube.com/watch?v=abc")
        self.assertEqual(self.p.mpv.props["audio-files"], [])

    def test_download_failure_reaches_the_status_line(self):
        seen = []
        self.p.statusChanged.connect(seen.append)
        def boom(url, h, cache, **kw):
            raise DownloadError("Video unavailable")
        player_mod.download = boom
        self.p.load("https://www.youtube.com/watch?v=abc")
        self.assertIn("Video unavailable", seen[-1])
        self.assertEqual(self.p.mpv.loads, [])

    def test_progress_is_reported_while_fetching(self):
        seen = []
        self.p.statusChanged.connect(seen.append)
        def slow(url, h, cache, progress=None, **kw):
            progress("downloading 40%")
            return self.clip
        player_mod.download = slow
        self.p.load("https://www.youtube.com/watch?v=abc")
        self.assertIn("downloading 40%", seen)

    def test_a_local_path_skips_yt_dlp_entirely(self):
        called = []
        player_mod.download = lambda *a, **k: called.append(1) or self.clip
        player_mod.resolve = lambda *a, **k: Stream(video_url="C:/clips/a.mp4",
                                                    direct=True)
        self.p.load("C:/clips/a.mp4")
        self.assertEqual(called, [])
        self.assertEqual(self.p.mpv.loads[-1][0], "C:/clips/a.mp4")


class PotProviderTests(unittest.TestCase):
    """The token server is the difference between a whole video and the first
    few MB of one, so its plumbing is checked rather than assumed."""

    def setUp(self):
        self.provider = potprovider.PotProvider(port=_free_port())
        # Isolate from the machine. These tests are about the HTTP-server path,
        # and on any box where setup.ps1 has run, find_script_home() finds a
        # real bgutil-server/ and short-circuits it - so the suite passed only
        # where the app was not actually installed. build.ps1 runs the tests
        # before building, on a machine that by definition has it installed,
        # which is exactly where that assumption broke.
        self._saved = {
            "find_script_home": potprovider.find_script_home,
            "server_version": potprovider.server_version,
            "plugin_version": potprovider.plugin_version,
        }
        potprovider.find_script_home = lambda: None
        # start() asks these two directly, not through PotProvider.compatible,
        # so _compatible() alone does not isolate it. On a real install they
        # answer 0.8.1 and 1.3.1, start() takes the version-mismatch exit, and
        # every test here fails for a reason that has nothing to do with it.
        potprovider.server_version = lambda exe: "1.0.0"
        potprovider.plugin_version = lambda: "1.0.0"

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(potprovider, name, value)
        self.provider.stop()

    def test_contributes_nothing_to_the_command_when_not_running(self):
        self.assertEqual(self.provider.extractor_args(), [])

    def test_points_yt_dlp_at_the_server_once_running(self):
        with _listening(self.provider.port), _compatible(True):
            self.assertTrue(self.provider.start())
            self.assertEqual(
                self.provider.extractor_args(),
                ["--extractor-args",
                 f"youtubepot-bgutilhttp:base_url=http://127.0.0.1:{self.provider.port}"])

    def test_says_nothing_when_the_plugin_would_refuse_the_server(self):
        # Pointing yt-dlp at a server its plugin rejects is worse than pointing
        # it at nothing: a wall of warnings and the identical failure.
        with _listening(self.provider.port), _compatible(False):
            self.assertTrue(self.provider.start())
            self.assertEqual(self.provider.extractor_args(), [])

    def test_reuses_a_server_someone_else_is_already_running(self):
        with _listening(self.provider.port):
            self.assertTrue(self.provider.start())
            self.assertIn("already on port", self.provider.status)

    def test_reports_clearly_when_the_generator_is_missing(self):
        real = potprovider.find_executable
        potprovider.find_executable = lambda: None
        try:
            self.assertFalse(self.provider.start())
        finally:
            potprovider.find_executable = real
        self.assertIn("setup.ps1", self.provider.status)

    def test_stopping_is_safe_when_nothing_was_started(self):
        self.provider.stop()          # must not raise
        self.assertFalse(self.provider.running)

    def test_port_probe_says_no_for_a_closed_port(self):
        self.assertFalse(potprovider.port_is_open(_free_port()))

class PotVersionTests(unittest.TestCase):
    """A major-version gap makes the plugin decline to request a token, and the
    only symptom downstream is another 403 - exactly the kind of silent failure
    worth a check of its own."""

    def test_matching_majors_agree(self):
        self.assertTrue(potprovider.versions_agree("0.8.1", "0.8.1"))
        self.assertTrue(potprovider.versions_agree("0.8.3", "0.8.1"))

    def test_a_major_gap_is_caught(self):
        self.assertFalse(potprovider.versions_agree("1.3.1", "0.8.1"))
        self.assertFalse(potprovider.versions_agree("0.8.1", "1.3.1"))

    def test_unknown_versions_do_not_cry_wolf(self):
        self.assertTrue(potprovider.versions_agree(None, "0.8.1"))
        self.assertTrue(potprovider.versions_agree("1.3.1", None))
        self.assertTrue(potprovider.versions_agree(None, None))


class SettingsFileTests(unittest.TestCase):
    """Settings used to be commented-out `set` lines in run.bat. doctor.bat and
    check.bat are separate processes and never saw them, and every update
    overwrote the file - so a setting could look applied and do nothing."""

    def _load(self, text, environ=None):
        import importlib
        from chuutube2spout import config as cfg
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.txt"
            path.write_text(text, encoding="utf-8")
            real_path, real_env = cfg._settings_path, os.environ
            captured = dict(environ or {})
            cfg._settings_path = lambda: path
            os.environ = captured
            try:
                cfg.load_settings_file()
            finally:
                cfg._settings_path = real_path
                os.environ = real_env
            return captured

    def test_reads_plain_key_values(self):
        env = self._load("COOKIES_FROM_BROWSER=waterfox\n")
        self.assertEqual(env["CHUUTUBE2SPOUT_COOKIES_FROM_BROWSER"], "waterfox")

    def test_prefix_is_optional_but_accepted(self):
        env = self._load("CHUUTUBE2SPOUT_HWDEC=no\n")
        self.assertEqual(env["CHUUTUBE2SPOUT_HWDEC"], "no")

    def test_a_comment_after_the_value_is_not_part_of_it(self):
        # settings.txt.example had `#HWDEC=no   # black video...` lines; taken
        # off the leading #, the old parser stored the whole rest of the line.
        env = self._load("HWDEC=no              # black video or a crash\n"
                         "LOG_KEEP=30\t# kept\n")
        self.assertEqual(env["CHUUTUBE2SPOUT_HWDEC"], "no")
        self.assertEqual(env["CHUUTUBE2SPOUT_LOG_KEEP"], "30")

    def test_a_colour_is_not_mistaken_for_a_comment(self):
        env = self._load("SENDER_BACKGROUND=#101018\n"
                         "SENDER_A=#1 # note\n")
        self.assertEqual(env["CHUUTUBE2SPOUT_SENDER_BACKGROUND"], "#101018")
        self.assertEqual(env["CHUUTUBE2SPOUT_SENDER_A"], "#1")

    def test_every_example_line_parses_cleanly(self):
        # Uncomment every setting in the shipped example at once: no value may
        # carry text from a comment, and every key must be one the app reads.
        text = (ROOT / "settings.txt.example").read_text(encoding="utf-8")
        lines = [ln[1:] for ln in text.splitlines()
                 if re.match(r"#[A-Z_]+=", ln)]
        self.assertGreater(len(lines), 40)
        env = self._load("\n".join(lines) + "\n")
        source = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (ROOT / "chuutube2spout").glob("*.py"))
        for key, value in env.items():
            name = key.replace("CHUUTUBE2SPOUT_", "", 1)
            with self.subTest(name=name):
                self.assertNotIn(" ", value.strip(),
                                 f"{name}={value!r} carries a comment")
                self.assertIn(f'"{name}"', source,
                              f"{name} is documented but never read")

    def test_tolerates_a_line_pasted_from_a_batch_file(self):
        # "set X=y & rem comment" would otherwise store "y & rem comment".
        env = self._load("set COOKIES_FROM_BROWSER=waterfox & rem sign in\n")
        self.assertEqual(env["CHUUTUBE2SPOUT_COOKIES_FROM_BROWSER"], "waterfox")

    def test_ignores_comments_and_blanks(self):
        env = self._load("# COOKIES_FROM_BROWSER=chrome\n\n; HWDEC=no\nrem LOOP=0\n")
        self.assertEqual(env, {})

    def test_strips_quotes(self):
        env = self._load('SENDER_A="my sender"\n')
        self.assertEqual(env["CHUUTUBE2SPOUT_SENDER_A"], "my sender")

    def test_a_real_environment_variable_still_wins(self):
        env = self._load("HWDEC=no\n", environ={"CHUUTUBE2SPOUT_HWDEC": "auto-safe"})
        self.assertEqual(env["CHUUTUBE2SPOUT_HWDEC"], "auto-safe")

    def test_a_line_without_an_equals_is_skipped(self):
        env = self._load("nonsense\nHWDEC=no\n")
        self.assertEqual(env, {"CHUUTUBE2SPOUT_HWDEC": "no"})


class CookieResolutionTests(unittest.TestCase):
    """Waterfox and friends keep Firefox-format cookies somewhere yt-dlp will
    not look, so the profile has to be found and handed over explicitly."""

    def test_passes_known_browsers_through_untouched(self):
        for value in ("chrome", "firefox", "edge", "brave",
                      "firefox:C:/some/profile", "chrome::Default"):
            self.assertEqual(cookies_mod.resolve(value)[0], value)

    def test_empty_stays_empty(self):
        self.assertEqual(cookies_mod.resolve("")[0], "")
        self.assertEqual(cookies_mod.resolve("   ")[0], "")

    def test_waterfox_becomes_a_firefox_profile_path(self):
        real = cookies_mod.find_profile
        cookies_mod.find_profile = lambda name: "C:/Users/x/Waterfox/abc.default"
        try:
            arg, warning = cookies_mod.resolve("waterfox")
        finally:
            cookies_mod.find_profile = real
        self.assertEqual(arg, "firefox:C:/Users/x/Waterfox/abc.default")
        self.assertIsNone(warning)

    def test_an_explicit_waterfox_path_is_left_alone(self):
        value = "waterfox:C:/custom"
        self.assertEqual(cookies_mod.resolve(value)[0], value)

    def test_a_missing_profile_warns_rather_than_guessing(self):
        real = cookies_mod.find_profile
        cookies_mod.find_profile = lambda name: None
        try:
            arg, warning = cookies_mod.resolve("waterfox")
        finally:
            cookies_mod.find_profile = real
        self.assertEqual(arg, "")
        self.assertIn("waterfox", warning)

    def test_finds_the_most_recently_used_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "Waterfox" / "Profiles"
            for name, stamp in (("old.default", 1000), ("new.default", 9000)):
                profile = base / name
                profile.mkdir(parents=True)
                cookie = profile / "cookies.sqlite"
                cookie.write_bytes(b"x")
                os.utime(cookie, (stamp, stamp))
            real = cookies_mod.profile_roots
            cookies_mod.profile_roots = lambda folders: [base]
            try:
                found = cookies_mod.find_profile("waterfox")
            finally:
                cookies_mod.profile_roots = real
        self.assertTrue(found.endswith("new.default"), found)


class DirectUrlTests(unittest.TestCase):
    def test_media_files_bypass_ytdlp(self):
        for url in ("https://example.com/clip.mp4", "https://cdn/x.m3u8",
                    "rtsp://cam/live", "https://a/b.MKV"):
            self.assertTrue(is_direct(url), url)

    def test_page_urls_do_not(self):
        for url in ("https://www.youtube.com/watch?v=abc",
                    "https://vimeo.com/12345"):
            self.assertFalse(is_direct(url), url)


class _FakeUpstream(BaseHTTPRequestHandler):
    """Behaves like googlevideo: refuses open-ended ranges, serves bounded ones."""

    BODY = bytes(range(256)) * 40          # 10240 bytes, position-identifiable
    seen_headers = {}
    requests = []

    def log_message(self, *args):
        pass

    def _deny(self, code=403):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    max_window = None      # 403 any window larger than this, like a chunk cap

    def do_GET(self):
        type(self).seen_headers = dict(self.headers)
        rng = self.headers.get("Range")
        type(self).requests.append(rng)

        if self.headers.get("X-Reject"):
            return self._deny()
        if self.headers.get("User-Agent") != "ExpectedAgent/1.0":
            return self._deny()
        # The behaviour this whole relay exists for: no unbounded fetches.
        if not rng or rng.strip().endswith("-"):
            return self._deny()

        lo_text, _, hi_text = rng.replace("bytes=", "").partition("-")
        lo, hi = int(lo_text), int(hi_text)
        # Real media servers answer a range starting past the end with 403,
        # not 416 - which is what made an arithmetic bug look like a ban.
        if lo >= len(self.BODY):
            return self._deny()
        if self.max_window is not None and (hi - lo + 1) > self.max_window:
            return self._deny()
        hi = min(hi, len(self.BODY) - 1)
        chunk = self.BODY[lo:hi + 1]
        self.send_response(206)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Range",
                         f"bytes {lo}-{lo + len(chunk) - 1}/{len(self.BODY)}")
        self.end_headers()
        self.wfile.write(chunk)


class RangeParsingTests(unittest.TestCase):
    def test_absent_means_the_whole_file(self):
        self.assertEqual(parse_range(None), (0, None))

    def test_open_ended(self):
        self.assertEqual(parse_range("bytes=100-"), (100, None))

    def test_bounded(self):
        self.assertEqual(parse_range("bytes=10-19"), (10, 19))

    def test_rejects_malformed_and_multi_range(self):
        for bad in ("chunks=0-1", "bytes=0-1,5-6", "bytes=abc-", "bytes=20-10",
                    "bytes=-500", "bytes"):
            self.assertIsNone(parse_range(bad), bad)


class HeaderProxyTests(unittest.TestCase):
    """The relay is the thing standing between a signed URL and a 403, so it
    gets tested against a real socket rather than a mock."""

    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
        cls.thread = threading.Thread(target=cls.upstream.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.upstream.server_address[1]}/media"

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self):
        self.proxy = HeaderProxy()

    def tearDown(self):
        self.proxy.shutdown()

    def _get(self, url, headers=None):
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                # resp.headers is case-insensitive; HTTP header names are too.
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, b"", exc.headers

    def test_attaches_the_headers_the_url_was_issued_for(self):
        url = self.proxy.publish(self.base, {"User-Agent": "ExpectedAgent/1.0"})
        status, body, _ = self._get(url)
        self.assertIn(status, (200, 206))
        self.assertEqual(body, _FakeUpstream.BODY)

    def test_never_asks_upstream_for_an_open_ended_range(self):
        # The bug that caused every 403: a player opens a file with
        # "Range: bytes=0-" and the media server refuses it outright.
        _FakeUpstream.requests.clear()
        url = self.proxy.publish(self.base, {"User-Agent": "ExpectedAgent/1.0"})
        self._get(url)
        self.assertTrue(_FakeUpstream.requests)
        for sent in _FakeUpstream.requests:
            self.assertIsNotNone(sent)
            self.assertFalse(sent.strip().endswith("-"), sent)

    def test_whole_file_is_delivered_across_several_windows(self):
        _FakeUpstream.requests.clear()
        with _chunk_size(1024):
            url = self.proxy.publish(self.base, {"User-Agent": "ExpectedAgent/1.0"})
            status, body, headers = self._get(url)
        self.assertIn(status, (200, 206))
        self.assertEqual(body, _FakeUpstream.BODY)
        self.assertGreater(len(_FakeUpstream.requests), 1,
                           "expected the fetch to be split into windows")
        self.assertEqual(headers.get("Content-Length"),
                         str(len(_FakeUpstream.BODY)))

    def test_a_seek_into_the_middle_returns_the_right_bytes(self):
        with _chunk_size(1024):
            url = self.proxy.publish(self.base, {"User-Agent": "ExpectedAgent/1.0"})
            status, body, headers = self._get(url, {"Range": "bytes=4096-6143"})
        self.assertEqual(status, 206)
        self.assertEqual(body, _FakeUpstream.BODY[4096:6144])
        self.assertEqual(headers.get("Content-Range"),
                         f"bytes 4096-6143/{len(_FakeUpstream.BODY)}")

    def test_open_ended_client_range_still_reaches_the_end(self):
        with _chunk_size(1024):
            url = self.proxy.publish(self.base, {"User-Agent": "ExpectedAgent/1.0"})
            status, body, _ = self._get(url, {"Range": "bytes=8192-"})
        self.assertEqual(status, 206)
        self.assertEqual(body, _FakeUpstream.BODY[8192:])

    def test_never_asks_for_a_byte_past_the_end_of_the_resource(self):
        # clen is baked into googlevideo URLs, so the size is known before the
        # first fetch and there is no excuse for over-reading.
        _FakeUpstream.requests.clear()
        size = len(_FakeUpstream.BODY)
        url = self.proxy.publish(f"{self.base}?clen={size}",
                                 {"User-Agent": "ExpectedAgent/1.0"})
        status, body, _ = self._get(url)
        self.assertIn(status, (200, 206))
        self.assertEqual(body, _FakeUpstream.BODY)
        for sent in _FakeUpstream.requests:
            hi = int(sent.replace("bytes=", "").partition("-")[2])
            self.assertLess(hi, size, f"asked for {sent} of a {size}-byte file")

    def test_shrinks_the_window_when_a_big_one_is_refused(self):
        _FakeUpstream.max_window = 4096
        try:
            with _window_limits(chunk=len(_FakeUpstream.BODY), floor=1024):
                url = self.proxy.publish(self.base,
                                         {"User-Agent": "ExpectedAgent/1.0"})
                status, body, _ = self._get(url)
        finally:
            _FakeUpstream.max_window = None
        self.assertIn(status, (200, 206))
        self.assertEqual(body, _FakeUpstream.BODY)

    def test_a_shrink_is_remembered_rather_than_repeated(self):
        _FakeUpstream.max_window = 4096
        try:
            with _window_limits(chunk=len(_FakeUpstream.BODY), floor=1024):
                url = self.proxy.publish(self.base,
                                         {"User-Agent": "ExpectedAgent/1.0"})
                self._get(url)
                _FakeUpstream.requests.clear()
                self._get(url)
        finally:
            _FakeUpstream.max_window = None
        refused = [r for r in _FakeUpstream.requests
                   if int(r.replace("bytes=", "").partition("-")[2])
                   - int(r.replace("bytes=", "").partition("-")[0]) + 1 > 4096]
        self.assertEqual(refused, [], "re-learned the window size from scratch")

    def test_malformed_range_is_a_416_not_a_guess(self):
        url = self.proxy.publish(self.base, {"User-Agent": "ExpectedAgent/1.0"})
        status, _, _ = self._get(url, {"Range": "bytes=99-1"})
        self.assertEqual(status, 416)

    def test_mpvs_own_range_is_honoured(self):
        url = self.proxy.publish(self.base, {"User-Agent": "ExpectedAgent/1.0"})
        status, body, _ = self._get(url, {"Range": "bytes=5-9"})
        self.assertEqual(status, 206)
        self.assertEqual(body, _FakeUpstream.BODY[5:10])

    def test_accept_encoding_is_never_forwarded(self):
        # Media is already compressed, and the request that minted the URL did
        # not negotiate an encoding. This is the difference that made a URL
        # verify successfully and then 403 on playback.
        url = self.proxy.publish(self.base, {"User-Agent": "ExpectedAgent/1.0",
                                             "Accept-Encoding": "gzip, deflate, br"})
        self._get(url, {"Accept-Encoding": "gzip"})
        # urllib supplies "identity" itself; what matters is that neither
        # yt-dlp's nor mpv's compressed encodings reach the media server.
        self.assertEqual(_FakeUpstream.seen_headers.get("Accept-Encoding"),
                         "identity")

    def test_hop_by_hop_headers_are_never_forwarded(self):
        url = self.proxy.publish(self.base, {"User-Agent": "ExpectedAgent/1.0",
                                             "Connection": "keep-alive",
                                             "Host": "wrong.example"})
        self._get(url)
        self.assertNotEqual(_FakeUpstream.seen_headers.get("Host"),
                            "wrong.example")

    def test_retries_with_minimal_headers_when_the_full_set_is_refused(self):
        url = self.proxy.publish(self.base, {"User-Agent": "ExpectedAgent/1.0",
                                             "X-Reject": "yes"})
        status, body, _ = self._get(url)
        self.assertIn(status, (200, 206))
        self.assertEqual(body, _FakeUpstream.BODY)

    def test_without_the_relay_the_same_request_is_refused(self):
        # This is the failure the relay exists to prevent.
        status, _, _ = self._get(self.base)
        self.assertEqual(status, 403)

    def test_range_requests_pass_through_so_seeking_works(self):
        url = self.proxy.publish(self.base, {"User-Agent": "ExpectedAgent/1.0"})
        status, body, headers = self._get(url, {"Range": "bytes=10-19"})
        self.assertEqual(status, 206)
        self.assertEqual(body, _FakeUpstream.BODY[10:20])
        self.assertEqual(headers.get("Content-Range"),
                         f"bytes 10-19/{len(_FakeUpstream.BODY)}")

    def test_upstream_status_is_reported_not_swallowed(self):
        url = self.proxy.publish(self.base, {"User-Agent": "WrongAgent"})
        status, _, _ = self._get(url)
        self.assertEqual(status, 403)

    def test_unknown_token_is_a_404(self):
        self.proxy.start()
        status, _, _ = self._get(f"http://127.0.0.1:{self.proxy.port}/nope")
        self.assertEqual(status, 404)

    def test_each_publish_gets_an_unguessable_distinct_token(self):
        a = self.proxy.publish(self.base, {})
        b = self.proxy.publish(self.base, {})
        self.assertNotEqual(a, b)
        self.assertGreater(len(a.rsplit("/", 1)[-1]), 12)

    def test_forgetting_a_token_revokes_it(self):
        url = self.proxy.publish(self.base, {"User-Agent": "ExpectedAgent/1.0"})
        self.proxy.forget([url.rsplit("/", 1)[-1]])
        status, _, _ = self._get(url)
        self.assertEqual(status, 404)

    def test_binds_to_loopback_only(self):
        self.proxy.start()
        self.assertEqual(self.proxy._server.server_address[0], "127.0.0.1")


class ProxyWiringTests(unittest.TestCase):
    def setUp(self):
        self.p = Player("A")
        self._real_threading = player_mod.threading
        player_mod.threading = _InlineThreading
        self._real_mode = player_mod.MODE
        player_mod.MODE = "stream"

    def tearDown(self):
        player_mod.threading = self._real_threading
        player_mod.MODE = self._real_mode
        get_proxy().shutdown()

    def test_remote_urls_are_routed_through_the_relay(self):
        self.enterContext(audio_enabled())
        player_mod.resolve = lambda *a, **k: _stream()
        self.p.load("https://www.youtube.com/watch?v=abc")
        played = self.p.mpv.loads[-1][0]
        self.assertTrue(played.startswith("http://127.0.0.1:"), played)
        self.assertEqual(self.p.mpv.props["audio-files"][0][:17],
                         "http://127.0.0.1:")

    def test_local_files_are_not_proxied(self):
        player_mod.resolve = lambda *a, **k: Stream(
            video_url="C:/clips/a.mp4", direct=True)
        self.p.load("C:/clips/a.mp4")
        self.assertEqual(self.p.mpv.loads[-1][0], "C:/clips/a.mp4")

    def test_reloading_revokes_the_previous_tokens(self):
        player_mod.resolve = lambda *a, **k: _stream()
        self.p.load("https://www.youtube.com/watch?v=one")
        first = list(self.p._proxy_tokens)
        self.p.load("https://www.youtube.com/watch?v=two")
        self.assertTrue(first)
        for token in first:
            self.assertNotIn(token, get_proxy()._server.entries)


class BatchArgumentTests(unittest.TestCase):
    """cmd.exe splits arguments on '=' as well as spaces, so an unquoted
    check.bat URL arrives in pieces and silently probes the wrong thing."""

    def test_rejoins_a_url_split_on_equals(self):
        self.assertEqual(
            _normalise(["https://www.youtube.com/watch?v", "dQw4w9WgXcQ"]),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_leaves_an_intact_url_alone(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertEqual(_normalise([url]), url)

    def test_ignores_flags(self):
        self.assertEqual(_normalise(["--check", "https://x/y"]), "https://x/y")

    def test_no_url_is_empty(self):
        self.assertEqual(_normalise(["--check"]), "")


class ResolveFailureSummaryTests(unittest.TestCase):
    def test_all_403_is_reported_as_an_ip_block(self):
        msg = resolver._summarise(["tv: HTTP 403", "ios: HTTP 403"])
        self.assertIn("blocking this IP", msg)

    def test_mixed_failures_list_the_attempts(self):
        msg = resolver._summarise(["tv: HTTP 403", "ios: no video formats"])
        self.assertNotIn("blocking this IP", msg)
        self.assertIn("no video formats", msg)


class AudioRoutingTests(unittest.TestCase):
    """One slot audible at a time, by its own speaker button."""

    def setUp(self):
        self.w = MainWindow()

    def muted(self):
        return [p.muted for p in self.w.players]

    def test_everything_starts_muted(self):
        self.assertEqual(self.muted(), [True, True])

    def test_turning_a_slot_on_opens_only_that_one(self):
        self.w.panels[1].toggle_audio()
        self.assertEqual(self.muted(), [True, False])

    def test_turning_the_other_on_turns_the_first_off(self):
        self.w.panels[0].toggle_audio()
        self.w.panels[1].toggle_audio()
        self.assertEqual(self.muted(), [True, False])
        self.assertFalse(self.w.panels[0].audio_enabled)
        self.assertFalse(self.w.panels[0].mute_btn.isChecked())

    def test_turning_it_off_again_leaves_silence(self):
        self.w.panels[0].toggle_audio()
        self.w.panels[0].toggle_audio()
        self.assertEqual(self.muted(), [True, True])

    def test_selecting_a_slot_does_not_move_the_audio(self):
        # Audio used to follow LIVE. Selection is only where the keys go.
        self.w.panels[0].toggle_audio()
        self.w.select_slot(1)
        self.w.apply_audio()
        self.assertEqual(self.muted(), [False, True])


class FocusPolicyTests(unittest.TestCase):
    """A clicked button must not stay highlighted for the rest of the set.

    Once a transport button took focus, nothing else in the window wanted it
    back, so it sat there looking armed - and Space then went to the button
    instead of the play/pause shortcut, which mid-set is worse than cosmetic.
    """

    def setUp(self):
        self.w = MainWindow()

    def test_transport_controls_never_take_focus(self):
        for panel in self.w.panels:
            for name in ("start_btn", "play_btn", "mute_btn", "quality_box",
                         "load_btn", "clear_btn"):
                widget = getattr(panel, name)
                self.assertEqual(widget.focusPolicy(), Qt.NoFocus,
                                 f"{name} can still steal focus")

    def test_the_url_field_still_takes_focus(self):
        # It is the one control you actually type into.
        self.assertNotEqual(self.w.panels[0].url_edit.focusPolicy(), Qt.NoFocus)

    def test_the_status_bar_controls_never_take_focus(self):
        self.assertEqual(self.w.clear_cache_box.focusPolicy(), Qt.NoFocus)
        self.assertEqual(self.w.help_btn.focusPolicy(), Qt.NoFocus)


class ClearCacheCheckboxTests(unittest.TestCase):
    """The cache policy was two settings-file keys and no way to see either.

    Whether the folder survives closing the app is the sort of thing you want
    to change between sets, not between builds."""

    def setUp(self):
        self.w = MainWindow()

    def test_it_starts_from_the_configured_default(self):
        self.assertEqual(self.w.clear_cache_box.isChecked(),
                         config.CLEAR_CACHE_ON_EXIT)

    def test_ticking_it_is_remembered(self):
        self.w.clear_cache_box.setChecked(True)
        self.assertEqual(self.w.settings.value("clear_cache_on_exit"), True)
        self.w.clear_cache_box.setChecked(False)
        self.assertEqual(self.w.settings.value("clear_cache_on_exit"), False)

    def test_the_checkbox_decides_at_close_not_the_config_key(self):
        # Otherwise the tick would appear to do nothing until the next restart.
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source[source.index("def closeEvent"):]
        self.assertIn("clear_cache_box.isChecked()", body)
        self.assertNotIn("CLEAR_CACHE_ON_EXIT", body)

    def test_it_says_what_it_does(self):
        self.assertTrue(self.w.clear_cache_box.toolTip(),
                        "a checkbox that deletes files needs a tooltip")


class DragRepaintTests(unittest.TestCase):
    """Dragging the window left a trail: repaints queued behind Windows' own
    modal move loop and reached the compositor out of step with the move.

    The fix is to stop repainting the preview while the window is moving. What
    must NOT happen is Spout going quiet at the same time - Resolume is showing
    that output to a room."""

    def setUp(self):
        # Opt-in since 0.9.9.22; these test the opted-in behaviour.
        saved = surface_mod.DRAG_SETTLE_MS
        surface_mod.DRAG_SETTLE_MS = 220
        self.addCleanup(setattr, surface_mod, "DRAG_SETTLE_MS", saved)
        self.w = MainWindow()
        self.surface = self.w.surface

    def test_off_by_default(self):
        # K: no trail when moving with it off, the preview keeps playing, and
        # Spout no longer drops to ~30fps whenever the window moves.
        self.assertEqual(config.DRAG_SETTLE_MS, 0)
        surface_mod.DRAG_SETTLE_MS = 0
        from chuutube2spout.surface import TICK_INTERVAL_MS
        w = MainWindow()
        w.moveEvent(object())
        w.resizeEvent(object())
        self.assertFalse(w.surface._interacting)
        self.assertEqual(w.surface._timer.interval, TICK_INTERVAL_MS)

    def test_a_move_suspends_preview_repaints(self):
        self.assertFalse(self.surface._interacting)
        self.w.moveEvent(object())
        self.assertTrue(self.surface._interacting)

    def test_repaints_resume_once_the_window_settles(self):
        self.w.moveEvent(object())
        self.surface._end_interaction()
        self.assertFalse(self.surface._interacting)
        self.assertTrue(self.surface._force_tick,
                        "the first frame after a drag must be redrawn")

    def test_spout_keeps_publishing_during_a_drag(self):
        self.surface._gl_ready = True
        self.w.moveEvent(object())
        for player in self.surface.players:
            player.render = lambda: True
        sent = []
        self.surface.spout.publish = lambda *a, **k: sent.append(a)
        self.surface._dirty = True
        self.surface.tick()
        self.assertEqual(len(sent), 1,
                         "output stopped while the window was being moved")

    def test_resizing_counts_as_interaction_too(self):
        self.w.resizeEvent(object())
        self.assertTrue(self.surface._interacting)

    def test_the_render_loop_slows_down_during_a_drag(self):
        # Pausing only the repaint was not enough: the loop wakes 250 times a
        # second, and every wakeup is one more thing the modal move loop has to
        # get past before it can move the window.
        from chuutube2spout.config import DRAG_TICK_MS
        self.w.moveEvent(object())
        self.assertEqual(self.surface._timer.interval, DRAG_TICK_MS)

    def test_the_render_loop_speeds_back_up_afterwards(self):
        from chuutube2spout.surface import TICK_INTERVAL_MS
        self.w.moveEvent(object())
        self.surface._end_interaction()
        self.assertEqual(self.surface._timer.interval, TICK_INTERVAL_MS)

    def test_output_is_never_paused_by_default(self):
        # Freezing Spout during a drag is a diagnostic, not a default -
        # Resolume is showing that output to a room.
        from chuutube2spout import config
        self.assertFalse(config.DRAG_PAUSE_RENDER)


class CookieExportTests(unittest.TestCase):
    """Cookies are read from a copy, so the browser can stay open.

    That is not a nicety: Waterfox is where the URLs are being copied from, so
    "close your browser first" would mean closing the thing you are using.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="chuutube-cookietest-"))
        self.profile = self.dir / "abc.default"
        self.profile.mkdir()
        db = sqlite3.connect(str(self.profile / "cookies.sqlite"))
        db.execute("CREATE TABLE moz_cookies (id INTEGER, host TEXT, path TEXT,"
                   " isSecure INTEGER, expiry INTEGER, name TEXT, value TEXT)")
        db.executemany("INSERT INTO moz_cookies VALUES (?,?,?,?,?,?,?)", [
            (1, ".youtube.com", "/", 1, 2000000000, "SID", "abc123"),
            (2, "accounts.google.com", "/", 1, 2000000000, "SAPISID", "def456"),
            (3, ".my-bank.example", "/", 1, 2000000000, "session", "secret"),
        ])
        db.commit()
        db.close()
        self.dest = self.dir / "out" / "cookies.txt"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _export(self):
        return cookies_mod.export(f"firefox:{self.profile}", self.dest)

    def test_writes_a_netscape_file_yt_dlp_can_read(self):
        path, warning = self._export()
        self.assertIsNone(warning)
        text = path.read_text()
        self.assertTrue(text.startswith("# Netscape HTTP Cookie File"))
        row = [ln for ln in text.splitlines() if "SID\tabc123" in ln][0]
        fields = row.split("\t")
        self.assertEqual(len(fields), 7)
        self.assertEqual(fields[0], ".youtube.com")
        self.assertEqual(fields[1], "TRUE")     # leading dot = subdomains
        self.assertEqual(fields[3], "TRUE")     # secure

    def test_only_youtubes_cookies_are_written_out(self):
        # A plaintext dump of the user's whole session is not a fair price for
        # signing in to one site.
        path, _ = self._export()
        text = path.read_text()
        self.assertIn("SID\tabc123", text)      # the youtube.com one
        self.assertNotIn("my-bank", text)
        self.assertNotIn("secret", text)

    def test_google_account_cookies_are_not_exported(self):
        # A suffix match on "google.com" also matches accounts./mail./pay.,
        # which means SID, HSID, SSID and __Secure-*PSID - the master Google
        # account cookies. They authenticate Gmail and Drive, they satisfy an
        # already-2FA'd session on their own, and yt-dlp never reads them:
        # it only looks up the jar for youtube.com.
        path, _ = self._export()
        text = path.read_text()
        self.assertNotIn("SAPISID", text)
        self.assertNotIn("def456", text)
        self.assertNotIn("google.com", text)

    def test_the_domain_list_does_not_reach_beyond_youtube(self):
        for domain in cookies_mod.DEFAULT_DOMAINS:
            self.assertNotEqual(domain, "google.com",
                                "this pulls in Gmail and Drive sessions")

    @unittest.skipIf(sys.platform == "win32",
                     "Windows ignores POSIX modes; os.chmod only toggles "
                     "read-only. The jar is protected there by living in the "
                     "user's own %TEMP%, which is already ACL'd to them.")
    def test_the_jar_is_readable_only_by_its_owner(self):
        # A shared gig laptop is not a theoretical set of other users.
        path, _ = self._export()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_the_jar_is_created_through_the_private_writer(self):
        # The cross-platform half of the same claim: whatever the OS does with
        # the mode, we must not be using a plain write_text().
        source = Path(cookies_mod.__file__).read_text(encoding="utf-8")
        body = source[source.index("def export("):source.index("def _write_private")]
        self.assertIn("_write_private(dest", body)
        self.assertNotIn("dest.write_text", body)

    def test_the_profile_path_does_not_travel_inside_the_file(self):
        # It contains the Windows account name, and this header goes wherever
        # the file goes.
        path, _ = self._export()
        text = path.read_text()
        self.assertIn(self.profile.name, text)
        self.assertNotIn(str(self.profile.parent), text)

    def test_the_default_location_is_not_the_app_folder(self):
        # The README tells people to copy the whole app directory to another
        # machine. A cookie jar in cache/ gets copied, zipped and shared with
        # it, by someone with no reason to think it holds a live login.
        jar = cookies_mod.session_dir() / "cookies.txt"
        try:
            self.assertNotIn(str(Path(config.CACHE_DIR).resolve()),
                             str(jar.resolve()))
        finally:
            cookies_mod.cleanup()

    def test_cleanup_removes_the_jar(self):
        cookies_mod.ensure(f"firefox:{self.profile}")
        jar = cookies_mod.session_dir() / "cookies.txt"
        self.assertTrue(jar.is_file())
        cookies_mod.cleanup()
        self.assertFalse(jar.exists())

    def test_a_stranded_snapshot_from_a_crash_is_swept_up(self):
        # The snapshot is the WHOLE cookie store, not the filtered subset, so
        # one left in %TEMP% by a power cut is worse than the jar itself.
        stale = Path(tempfile.mkdtemp(prefix="chuutube-cookies-"))
        (stale / "cookies.sqlite").write_bytes(b"everything")
        old = time.time() - 48 * 3600
        os.utime(stale, (old, old))
        cookies_mod.sweep_stale()
        self.assertFalse(stale.exists())

    def test_a_snapshot_from_a_running_copy_is_left_alone(self):
        fresh = Path(tempfile.mkdtemp(prefix="chuutube-cookies-"))
        try:
            cookies_mod.sweep_stale()
            self.assertTrue(fresh.exists(), "clobbered another running instance")
        finally:
            shutil.rmtree(fresh, ignore_errors=True)

    def test_the_original_database_is_never_opened_in_place(self):
        # Reading the live store is what produces "database is locked" while the
        # browser is running.
        opened = []
        real = cookies_mod.sqlite3.connect
        cookies_mod.sqlite3.connect = lambda path, *a, **k: (
            opened.append(path), real(path, *a, **k))[1]
        try:
            self._export()
        finally:
            cookies_mod.sqlite3.connect = real
        self.assertTrue(opened)
        for path in opened:
            self.assertNotIn(str(self.profile), str(path))

    def test_the_write_ahead_log_is_copied_too(self):
        # Without it, a login from five minutes ago has not happened yet.
        wal = self.profile / "cookies.sqlite-wal"
        wal.write_bytes(b"")
        copied = []
        real = cookies_mod.shutil.copyfile
        cookies_mod.shutil.copyfile = lambda a, b: (copied.append(str(a)),
                                                    real(a, b))[1]
        try:
            self._export()
        finally:
            cookies_mod.shutil.copyfile = real
        self.assertTrue(any(name.endswith("-wal") for name in copied), copied)

    def test_a_profile_with_no_youtube_cookies_says_so(self):
        path, warning = cookies_mod.export(f"firefox:{self.profile}", self.dest,
                                           domains=("example.invalid",))
        self.assertIsNone(path)
        self.assertIn("signed in", warning)

    def test_a_chrome_value_is_left_to_yt_dlp(self):
        # Chrome-family stores are encrypted with a key held by the OS keyring.
        path, warning = cookies_mod.export("chrome", self.dest)
        self.assertIsNone(path)
        self.assertIsNone(warning)

    def test_a_fresh_export_is_reused_rather_than_repeated(self):
        cookies_mod.ensure(f"firefox:{self.profile}", self.dest.parent)
        calls = []
        real = cookies_mod.export
        cookies_mod.export = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
        try:
            cookies_mod.ensure(f"firefox:{self.profile}", self.dest.parent)
        finally:
            cookies_mod.export = real
        self.assertEqual(calls, [])

    def test_a_stale_export_is_refreshed(self):
        cookies_mod.ensure(f"firefox:{self.profile}", self.dest.parent)
        old = time.time() - 7200
        os.utime(self.dest, (old, old))
        path, _ = cookies_mod.ensure(f"firefox:{self.profile}",
                                     self.dest.parent, max_age_minutes=60)
        self.assertGreater(path.stat().st_mtime, old)

    def test_a_locked_profile_falls_back_to_the_last_export(self):
        # Losing cookies mid-set because a file was busy for a moment would be
        # a poor trade for freshness.
        cookies_mod.ensure(f"firefox:{self.profile}", self.dest.parent)
        old = time.time() - 7200
        os.utime(self.dest, (old, old))
        (self.profile / "cookies.sqlite").unlink()
        path, warning = cookies_mod.ensure(f"firefox:{self.profile}",
                                           self.dest.parent)
        self.assertEqual(path, self.dest)


class CookieArgumentTests(unittest.TestCase):
    """What yt-dlp is actually handed."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="chuutube-cookiearg-"))
        self._saved = (downloader.COOKIES_FROM_BROWSER, downloader.COOKIE_EXPORT)

    def tearDown(self):
        (downloader.COOKIES_FROM_BROWSER,
         downloader.COOKIE_EXPORT) = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_nothing_configured_means_no_flag(self):
        downloader.COOKIES_FROM_BROWSER = ""
        self.assertEqual(downloader.cookie_args(), [])

    def test_an_export_is_passed_as_a_file(self):
        downloader.COOKIES_FROM_BROWSER = "waterfox"
        downloader.COOKIE_EXPORT = True
        exported = self.dir / "cookies.txt"
        exported.write_text("# Netscape HTTP Cookie File\n")
        real = downloader.cookie_store.ensure
        downloader.cookie_store.ensure = lambda *a, **k: (exported, None)
        try:
            args = downloader.cookie_args()
        finally:
            downloader.cookie_store.ensure = real
        self.assertEqual(args, ["--cookies", str(exported)])

    def test_a_store_we_cannot_read_falls_back_to_yt_dlp(self):
        downloader.COOKIES_FROM_BROWSER = "chrome"
        downloader.COOKIE_EXPORT = True
        real = downloader.cookie_store.ensure
        downloader.cookie_store.ensure = lambda *a, **k: (None, None)
        try:
            args = downloader.cookie_args()
        finally:
            downloader.cookie_store.ensure = real
        self.assertEqual(args, ["--cookies-from-browser", "chrome"])


class ArgumentInjectionTests(unittest.TestCase):
    """A pasted URL must never be readable as a command-line option.

    yt-dlp has --exec (runs a command) and --config-locations (loads options
    from a path, including a UNC share). Both fit in a single argv token as
    `--opt=value`, so quoting does not help - one paste would be arbitrary code
    execution on the machine driving the show."""

    PAYLOADS = (
        "--exec=calc.exe",
        "--exec=cmd /c powershell -enc AAAA",
        "--config-locations=\\\\attacker\\share\\cfg",
        "--paths=C:\\Windows\\System32",
        "-oC:\\Users\\x\\Startup\\x.bat",
    )

    def test_the_url_is_refused_at_the_boundary(self):
        for payload in self.PAYLOADS:
            with self.assertRaises(resolver.UnsafeUrl, msg=payload):
                resolver.check_url(payload)

    def test_a_refused_url_never_reaches_a_subprocess(self):
        player = Player("A")
        real = player_mod.threading
        player_mod.threading = _InlineThreading
        seen = []
        player.statusChanged.connect(seen.append)
        try:
            player.load("--exec=calc.exe")
        finally:
            player_mod.threading = real
        self.assertEqual(player.url, "", "the payload was accepted")
        self.assertTrue(any("option" in s for s in seen), seen)

    def test_end_of_options_is_passed_even_so(self):
        # Defence in depth: if anything ever reaches _command anyway, "--" stops
        # yt-dlp parsing it as an option.
        cmd = downloader._command("yt-dlp", "--exec=calc.exe", "best",
                                  Path("/tmp"), Path("/tmp/p.txt"), [])
        self.assertEqual(cmd[-2:], ["--", "--exec=calc.exe"])

    def test_the_separator_comes_after_every_other_option(self):
        cmd = downloader._command("yt-dlp", "https://example/watch", "best",
                                  Path("/tmp"), Path("/tmp/p.txt"), ["-v"])
        self.assertEqual(cmd.count("--"), 1)
        self.assertEqual(cmd.index("--"), len(cmd) - 2)

    def test_ordinary_urls_still_load(self):
        for url in ("https://www.youtube.com/watch?v=abc",
                    "C:\\clips\\thing.mp4", "file:///tmp/x.mkv",
                    "rtmp://host/live"):
            self.assertEqual(resolver.check_url(url), url)

    def test_a_line_break_is_refused(self):
        # Pasting from a rich-text source can carry one, and argv splitting is
        # not somewhere to find out what happens next.
        with self.assertRaises(resolver.UnsafeUrl):
            resolver.check_url("https://example/watch\n--exec=calc.exe")


class RedactionTests(unittest.TestCase):
    """Verbose logs get pasted into bug reports. They must not carry secrets."""

    # Shaped exactly like a real yt-dlp -v line, with synthetic values
    # throughout: 203.0.113.0/24 is RFC 5737 TEST-NET-3, reserved for
    # documentation, so nothing here is anybody's session.
    URL = ('[debug] Invoking http downloader on "https://rr3---sn-4g5.google'
           'video.com/videoplayback?expire=1700000000&ei=EXAMPLE0'
           '&ip=203.0.113.9&id=o-EXAMPLE&itag=137'
           '&pot=EXAMPLEpotTOKENvalue0000'
           '&sig=EXAMPLEsignatureVALUE00&n=EXAMPLEnCHALLENGE"')

    def test_the_users_ip_is_removed(self):
        out = redact.scrub(self.URL)
        self.assertNotIn("203.0.113.9", out)
        self.assertIn("ip=<redacted>", out)

    def test_the_po_token_and_signature_are_removed(self):
        out = redact.scrub(self.URL)
        self.assertNotIn("EXAMPLEpotTOKENvalue0000", out)
        self.assertNotIn("EXAMPLEsignatureVALUE00", out)

    def test_the_line_is_still_diagnostic(self):
        # Knowing a request happened, to which host, for which format, is the
        # whole reason verbose output exists.
        out = redact.scrub(self.URL)
        self.assertIn("googlevideo.com/videoplayback", out)
        self.assertIn("itag=137", out)
        self.assertIn("expire=1700000000", out)

    def test_session_identifiers_in_json_are_removed(self):
        line = ('{"visitorData": "EXAMPLEvisitorDataAAA", "remoteHost": '
                '"203.0.113.9", "clientName": "MWEB"}')
        out = redact.scrub(line)
        self.assertNotIn("EXAMPLEvisitorDataAAA", out)
        self.assertNotIn("203.0.113.9", out)
        self.assertIn("MWEB", out, "over-redacted - this is a diagnostic")

    def test_a_cookie_header_is_removed_whole(self):
        out = redact.scrub("Cookie: SID=abc123; HSID=def456")
        self.assertNotIn("abc123", out)
        self.assertNotIn("def456", out)

    def test_loopback_and_private_addresses_are_left_alone(self):
        for address in ("127.0.0.1", "192.168.1.50", "10.0.0.1"):
            self.assertIn(address, redact.scrub(f"connecting to {address}:4416"))

    def test_ordinary_lines_pass_through_untouched(self):
        for line in ("[download] 100% of 70.52MiB",
                     "[youtube] Downloading mweb player API JSON",
                     "ERROR: unable to download video data: HTTP Error 403"):
            self.assertEqual(redact.scrub(line), line)

    def test_the_scrubber_runs_before_anything_is_quoted_back(self):
        # _explain() quotes the retained tail into DownloadError, which reaches
        # the status bar and any screenshot of it.
        tail = [redact.scrub(self.URL), "ERROR: HTTP Error 403: Forbidden"]
        message = downloader._explain(tail, 1)
        self.assertNotIn("203.0.113.9", message)


class TokenProviderStatusTests(unittest.TestCase):
    """"Is a token being issued?" is not "is a server process alive?".

    Conflating the two put a red "no PO token server" warning across the bottom
    of a window that was happily fetching 1080p - in script mode there is no
    server and never will be, because yt-dlp runs the generator itself."""

    def setUp(self):
        self.p = potprovider.PotProvider(port=4416)

    def test_script_mode_counts_as_active(self):
        self.p._script_home = "/somewhere/server"
        self.assertTrue(self.p.active)
        self.assertFalse(self.p.running, "script mode has no server process")

    def test_nothing_at_all_is_not_active(self):
        self.assertFalse(self.p.active)

    def test_a_running_server_is_active(self):
        self.p._external = True
        self.assertTrue(self.p.active)

    def test_stopping_clears_script_mode(self):
        self.p._script_home = "/somewhere/server"
        self.p.stop()
        self.assertFalse(self.p.active)

    def test_a_403_in_script_mode_does_not_blame_the_server(self):
        real = downloader.get_provider

        class _Script:
            active = True
            running = False

            def extractor_args(self):
                return []

        downloader.get_provider = lambda: _Script()
        try:
            message = downloader._explain(["ERROR: HTTP Error 403: Forbidden"], 1)
        finally:
            downloader.get_provider = real
        self.assertNotIn("PO token", message)


class BlankOnClearTests(unittest.TestCase):
    """Clearing a slot must black it out, not freeze on the last frame.

    mpv stops producing frames the instant it is told to stop, so the FBO - and
    therefore the Spout sender Resolume is watching - kept showing a clip that
    was no longer loaded."""

    def setUp(self):
        self.p = Player("A")

    def test_stopping_asks_for_a_blank_frame(self):
        self.p.stop()
        self.assertTrue(self.p._blank)

    def test_stopping_wakes_the_render_loop(self):
        # The loop only runs when there is a frame to draw, so a stop that did
        # not announce itself would never be acted on.
        woken = []
        self.p.frameReady.connect(lambda: woken.append(1))
        self.p.stop()
        self.assertEqual(len(woken), 1)

    def test_the_blank_is_drawn_and_published(self):
        self.p._render_ctx = object()          # never consulted on this path
        self.p.target.fbo, self.p.target.w, self.p.target.h = 1, 1920, 1080
        self.p.stop()
        self.assertTrue(self.p.render(), "a blank frame must be published")
        self.assertFalse(self.p._blank, "the blank must not repeat forever")

    def test_a_slot_that_never_played_needs_no_blanking(self):
        self.p._render_ctx = None
        self.p.stop()
        self.assertFalse(self.p.render())


class PreviewGeometryTests(unittest.TestCase):
    """The window should open at a size where two 16:9 clips fit exactly."""

    def setUp(self):
        self.w = MainWindow()

    def test_the_height_makes_both_cells_sixteen_by_nine(self):
        from chuutube2spout.surface import BORDER, GAP
        for width in (1300, 1000, 640):
            height = self.w.surface.height_for_width(width)
            cell_w = (width - GAP) // 2
            inner_w = cell_w - 2 * BORDER
            inner_h = height - 2 * BORDER
            self.assertAlmostEqual(inner_w / inner_h, 16 / 9, places=1,
                                   msg=f"at width {width}: {inner_w}x{inner_h}")

    def test_a_taller_aspect_asks_for_more_height(self):
        wide = self.w.surface.height_for_width(1300, aspect=16 / 9)
        tall = self.w.surface.height_for_width(1300, aspect=4 / 3)
        self.assertGreater(tall, wide)

    def test_showing_the_window_fits_it(self):
        # __init__ is too early: before the window is shown the surface has no
        # real size, so the arithmetic would run against a placeholder.
        before = len(self.w.resizes)
        self.w.showEvent(None)
        self.assertEqual(len(self.w.resizes), before + 1,
                         "the window was never fitted")

    def test_the_fit_moves_only_the_height(self):
        self.w.showEvent(None)
        width, height = self.w.resizes[-1]
        self.assertEqual(width, self.w.width())
        chrome = self.w.height() - self.w.surface.height()   # panels + status bar
        self.assertEqual(height - chrome,
                         self.w.surface.height_for_width(self.w.surface.width()))

    def test_it_fits_only_once(self):
        self.w.showEvent(None)
        settled = len(self.w.resizes)
        self.w.showEvent(None)      # e.g. minimised and restored
        self.assertEqual(len(self.w.resizes), settled)

    def test_a_restored_geometry_is_fitted_too(self):
        # A height left over from an older version is not a reason to start the
        # session letterboxed - but the width and screen position are kept.
        stubs.QSettings.seeded = {"geometry": b"something"}
        try:
            window = MainWindow()
            before = len(window.resizes)
            window.showEvent(None)
        finally:
            stubs.QSettings.seeded = {}
        self.assertEqual(len(window.resizes), before + 1)
        self.assertEqual(window.resizes[-1][0], window.width())

    def test_the_two_fits_are_inverses_of_each_other(self):
        surface = self.w.surface
        for width in (1320, 2640, 900):
            height = surface.height_for_width(width)
            self.assertAlmostEqual(surface.width_for_height(height), width,
                                   delta=4, msg=f"round trip at {width}")

    def test_a_restored_geometry_is_not_overwritten_by_a_default_size(self):
        stubs.QSettings.seeded = {"geometry": b"something"}
        try:
            window = MainWindow()
        finally:
            stubs.QSettings.seeded = {}
        self.assertEqual(window.resizes, [],
                         "the saved size was thrown away before it was used")


class WindowScaleTests(unittest.TestCase):
    """WINDOW_SCALE opens the window bigger; 1 is the size it has always been."""

    def setUp(self):
        self.w = MainWindow()
        self._saved = ui_mod.WINDOW_SCALE

    def tearDown(self):
        ui_mod.WINDOW_SCALE = self._saved

    def _fit_at(self, scale, screen=None):
        ui_mod.WINDOW_SCALE = scale
        if screen is not None:
            self.w._available_size = lambda: screen
        self.w.resizes.clear()
        self.w._first_fit()
        return self.w.resizes[-1] if self.w.resizes else None

    def test_two_x_doubles_the_startup_width(self):
        width, _ = self._fit_at(2.0)
        self.assertEqual(width, ui_mod.DEFAULT_WIDTH * 2)

    def test_one_x_leaves_the_width_where_it_was(self):
        # The default must not stomp on a width restored from last session.
        width, _ = self._fit_at(1.0)
        self.assertEqual(width, self.w.width())

    def test_the_height_follows_so_the_cells_stay_sixteen_by_nine(self):
        width, height = self._fit_at(2.0)
        chrome_w = self.w.width() - self.w.surface.width()
        chrome_h = self.w.height() - self.w.surface.height()
        self.assertEqual(height - chrome_h,
                         self.w.surface.height_for_width(width - chrome_w))

    def test_a_window_is_never_opened_wider_than_the_screen(self):
        width, _ = self._fit_at(4.0, screen=(1920, 1080))
        self.assertLessEqual(width, 1920)

    def test_a_short_screen_takes_it_out_of_the_width_instead(self):
        # Capping the height alone would put the black bars straight back.
        width, height = self._fit_at(4.0, screen=(4000, 700))
        self.assertLessEqual(height, 700)
        chrome_w = self.w.width() - self.w.surface.width()
        chrome_h = self.w.height() - self.w.surface.height()
        self.assertAlmostEqual(
            self.w.surface.height_for_width(width - chrome_w),
            height - chrome_h, delta=4)

    def test_max_fills_the_screen_instead_of_guessing_a_multiplier(self):
        saved = ui_mod.WINDOW_FILL_SCREEN
        ui_mod.WINDOW_FILL_SCREEN = True
        self.w._available_size = lambda: (1920, 1040)
        self.w.resizes.clear()
        try:
            self.w._first_fit()
        finally:
            ui_mod.WINDOW_FILL_SCREEN = saved
        self.assertEqual(self.w.resizes[-1][0], 1920)

    def test_max_is_recognised_by_several_names(self):
        for word in ("max", "fit", "screen", "full", "MAX"):
            with _env(CHUUTUBE2SPOUT_WINDOW_SCALE=word):
                self.assertTrue(config._fill_screen(), word)
                self.assertEqual(config._scale(), 1.0, word)

    def test_a_number_is_not_mistaken_for_max(self):
        with _env(CHUUTUBE2SPOUT_WINDOW_SCALE="2"):
            self.assertFalse(config._fill_screen())
            self.assertEqual(config._scale(), 2.0)

    def test_a_nonsense_value_is_ignored_rather_than_fatal(self):
        with _env(CHUUTUBE2SPOUT_WINDOW_SCALE="enormous"):
            self.assertEqual(config._scale(), 1.0)

    def test_an_absurd_value_is_clamped(self):
        # A typo here means a window whose controls you cannot reach.
        with _env(CHUUTUBE2SPOUT_WINDOW_SCALE="400"):
            self.assertLessEqual(config._scale(), 8.0)
        with _env(CHUUTUBE2SPOUT_WINDOW_SCALE="0.001"):
            self.assertGreaterEqual(config._scale(), 0.25)

    def test_the_primary_screen_answers_when_the_windows_own_does_not(self):
        # At the moment the fit runs the window may not be on a screen yet, and
        # a missing answer is the difference between "twice the size" and
        # "wider than the monitor".
        class _Rect:
            def __init__(self, w, h):
                self._w, self._h = w, h
            def width(self):
                return self._w
            def height(self):
                return self._h

        class _Screen:
            def __init__(self, rect):
                self._rect = rect
            def availableGeometry(self):
                if self._rect is None:
                    raise AttributeError("no screen yet")
                return self._rect

        self.w._screens = lambda: [_Screen(None), _Screen(_Rect(1920, 1040))]
        self.assertEqual(self.w._available_size(), (1920, 1040))

    def test_a_screen_reporting_nonsense_is_not_trusted(self):
        class _Screen:
            def availableGeometry(self):
                class R:
                    width = staticmethod(lambda: 0)
                    height = staticmethod(lambda: 0)
                return R()
        self.w._screens = lambda: [_Screen()]
        self.assertIsNone(self.w._available_size())

    def test_an_unknown_screen_size_does_not_stop_the_fit(self):
        self.w._available_size = lambda: None
        ui_mod.WINDOW_SCALE = 2.0
        self.w.resizes.clear()
        self.w._first_fit()
        self.assertTrue(self.w.resizes)


@contextlib.contextmanager
def _env(**values):
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


class UrlFieldTests(unittest.TestCase):
    """Clicking into a URL field should select the URL, ready to paste over."""

    def setUp(self):
        self.field = ui_mod.UrlEdit()
        self.field.setText("https://www.youtube.com/watch?v=abc")

    def test_clicking_in_from_elsewhere_selects_everything(self):
        self.field._focused = False
        self.field.mousePressEvent(None)
        self.assertTrue(self.field.selected)

    def test_clicking_again_leaves_the_caret_alone(self):
        # A second click is a deliberate act - putting the caret somewhere.
        self.field._focused = True
        self.field.mousePressEvent(None)
        self.assertFalse(self.field.selected)

    def test_an_empty_field_has_nothing_to_select(self):
        self.field.setText("")
        self.field._focused = False
        self.field.mousePressEvent(None)
        self.assertFalse(self.field.selected)

    def test_arriving_by_shortcut_selects_too(self):
        self.field.setFocus()
        self.assertTrue(self.field.selected)


class MpvLogNoiseTests(unittest.TestCase):
    """ffmpeg repeats some warnings once per frame; verbose must stay useful."""

    def setUp(self):
        self.p = Player("A")

    def _log(self, message, times=1):
        for _ in range(times):
            self.p._on_mpv_log("warn", "ffmpeg/video", message)

    def test_repeats_are_collapsed(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self._log("h264: Late SEI is not implemented", times=500)
            self._log("something else")
        text = out.getvalue()
        self.assertEqual(text.count("Late SEI"), 1)
        self.assertIn("repeated 499 more time(s)", text)

    def test_an_alternating_pair_is_collapsed_too(self):
        # THE REAL CASE, which the single-message test above missed for a
        # release. ffmpeg emits this warning as a PAIR, so the two lines
        # alternate and "same as the previous line" never matches either.
        # A real 4-minute clip produced several hundred of each.
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            for _ in range(200):
                self._log("h264: Late SEI is not implemented")
                self._log("h264: If you want to help, upload a sample")
            self._log("something else")
        text = out.getvalue()
        self.assertEqual(text.count("Late SEI"), 1)
        self.assertEqual(text.count("upload a sample"), 1)
        self.assertIn("repeated 398 more time(s)", text)

    def test_errors_are_never_swallowed(self):
        # They are rare, they drive the status line, and losing the second one
        # to save a console line is a bad trade.
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.p._on_mpv_log("error", "ffmpeg/video", "boom")
            self.p._on_mpv_log("error", "ffmpeg/video", "boom")
        self.assertEqual(out.getvalue().count("boom"), 2)

    def test_distinct_lines_all_survive(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self._log("first")
            self._log("second")
            self._log("third")
        text = out.getvalue()
        for word in ("first", "second", "third"):
            self.assertIn(word, text)


class SettingsPrefixTests(unittest.TestCase):
    """Settings are CHUUTUBE2SPOUT_<KEY>, with the old prefix still honoured."""

    def test_the_new_prefix_is_read(self):
        with _env(CHUUTUBE2SPOUT_HWDEC="d3d11va-copy"):
            self.assertEqual(config._text("HWDEC", "auto-safe"), "d3d11va-copy")

    def test_the_old_prefix_still_works(self):
        # Someone's run.bat or shell profile should not break on a rename.
        with _env(YT2SPOUT_HWDEC="no"):
            self.assertEqual(config._text("HWDEC", "auto-safe"), "no")

    def test_the_new_prefix_wins_when_both_are_set(self):
        with _env(CHUUTUBE2SPOUT_HWDEC="new", YT2SPOUT_HWDEC="old"):
            self.assertEqual(config._text("HWDEC", "auto-safe"), "new")

    def test_the_default_survives_an_unset_key(self):
        self.assertEqual(config._text("NOT_A_REAL_KEY_XYZ", "fallback"),
                         "fallback")

    def test_flags_read_both_prefixes_too(self):
        with _env(YT2SPOUT_LOOP="0"):
            self.assertFalse(config._flag("LOOP", True))
        with _env(CHUUTUBE2SPOUT_LOOP="0"):
            self.assertFalse(config._flag("LOOP", True))


class CookieDomainWiringTests(unittest.TestCase):
    """config.COOKIE_DOMAINS is what the exporter is actually handed.

    Narrowing cookies.DEFAULT_DOMAINS was not enough on its own: config had its
    own copy of the list, still containing google.com, and that is the one the
    downloader passes down. The fix silently did nothing on the real path, and
    the existing test did not catch it because it called export() directly."""

    def test_config_and_cookies_agree(self):
        self.assertEqual(list(config.COOKIE_DOMAINS),
                         list(cookies_mod.DEFAULT_DOMAINS))

    def test_the_wired_default_does_not_include_google(self):
        self.assertNotIn("google.com", config.COOKIE_DOMAINS)

    def test_the_downloader_passes_configs_list_not_the_modules(self):
        # Prove the wiring, so a future edit to one list cannot quietly bypass
        # the other.
        seen = {}
        real = downloader.cookie_store.ensure
        downloader.cookie_store.ensure = lambda *a, **k: (seen.update(args=a), (None, None))[1]
        saved = downloader.COOKIES_FROM_BROWSER
        downloader.COOKIES_FROM_BROWSER = "waterfox"
        try:
            downloader.cookie_args()
        finally:
            downloader.cookie_store.ensure = real
            downloader.COOKIES_FROM_BROWSER = saved
        domains = seen["args"][3]
        self.assertNotIn("google.com", domains)


class FrozenBuildTests(unittest.TestCase):
    """A PyInstaller build has no .venv and no yt-dlp.exe.

    sys.executable is the app's own exe there, so the obvious fallback -
    [sys.executable, "-m", "yt_dlp"] - would launch a second copy of the GUI
    instead of a downloader. The exe re-enters itself with a flag instead."""

    def setUp(self):
        bootstrap._YTDLP_COMMAND = None
        self._frozen = getattr(sys, "frozen", None)
        self._runs = bootstrap._runs

    def tearDown(self):
        bootstrap._YTDLP_COMMAND = None
        bootstrap._runs = self._runs
        if self._frozen is None:
            if hasattr(sys, "frozen"):
                del sys.frozen
        else:
            sys.frozen = self._frozen

    def test_a_frozen_build_re_enters_itself(self):
        sys.frozen = True
        bootstrap._runs = lambda cmd: True
        self.assertEqual(bootstrap.ytdlp_command(refresh=True),
                         [sys.executable, bootstrap.YTDLP_FLAG])

    def test_it_never_falls_back_to_dash_m_when_frozen(self):
        # That fallback is correct from source and catastrophic when frozen:
        # "-m yt_dlp" against our own exe just opens the app again.
        sys.frozen = True
        bootstrap._runs = lambda cmd: True
        self.assertNotIn("-m", bootstrap.ytdlp_command(refresh=True))

    def test_a_build_without_yt_dlp_says_so_rather_than_hanging(self):
        sys.frozen = True
        bootstrap._runs = lambda cmd: False
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertIsNone(bootstrap.ytdlp_command(refresh=True))
        self.assertIn("collect-all", out.getvalue())

    def test_the_flag_is_answered_before_anything_heavy_loads(self):
        # The child must not touch Qt, mpv or Spout - it is a downloader.
        source = Path(app_mod.__file__).read_text(encoding="utf-8")
        body = source[source.index("def main("):]
        flag_at = body.index("YTDLP_FLAG")
        for heavy in ("prepare_native_paths()", "QApplication", "from .ui"):
            if heavy in body:
                self.assertLess(flag_at, body.index(heavy),
                                f"{heavy} runs before the yt-dlp handover")

    def test_running_from_source_is_unaffected(self):
        if hasattr(sys, "frozen"):
            del sys.frozen
        bootstrap._runs = lambda cmd: cmd[0] != "/nope"
        bootstrap.find_ytdlp = lambda: "/usr/bin/yt-dlp"
        try:
            self.assertEqual(bootstrap.ytdlp_command(refresh=True),
                             ["/usr/bin/yt-dlp"])
        finally:
            del bootstrap.find_ytdlp


class CachePruneTests(unittest.TestCase):
    """The cache had no deletion policy at all - it grew until someone noticed."""

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp(prefix="chuutube-prune-"))

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def _clip(self, name, mb, age_minutes):
        f = self.cache / name
        f.write_bytes(b"\0" * int(mb * 1024 * 1024))
        when = time.time() - age_minutes * 60
        os.utime(f, (when, when))
        return f

    def test_nothing_happens_below_the_limit(self):
        a = self._clip("a.mp4", 1, 120)
        removed, _ = downloader.prune(self.cache, max_gb=1.0)
        self.assertEqual(removed, 0)
        self.assertTrue(a.exists())

    def test_the_oldest_go_first(self):
        old = self._clip("old.mp4", 2, 600)
        mid = self._clip("mid.mp4", 2, 300)
        new = self._clip("new.mp4", 2, 120)
        downloader.prune(self.cache, max_gb=5.0 / 1024, min_age_minutes=30)
        self.assertFalse(old.exists(), "the oldest should have gone first")
        self.assertTrue(new.exists(), "the newest should have survived")
        self.assertTrue(mid.exists() or not old.exists())

    def test_a_fresh_clip_is_never_evicted(self):
        # The one just downloaded, and whatever is on screen.
        fresh = self._clip("fresh.mp4", 8, 1)
        downloader.prune(self.cache, max_gb=1.0 / 1024, min_age_minutes=30)
        self.assertTrue(fresh.exists())

    def test_a_file_that_will_not_delete_is_left_alone(self):
        # On Windows this is exactly the file mpv currently has open, which is
        # what makes pruning safe to do mid-set.
        stuck = self._clip("playing.mp4", 8, 600)
        real = pathlib.Path.unlink
        def refuse(self_, *a, **k):
            if self_.name == "playing.mp4":
                raise OSError("in use")
            return real(self_, *a, **k)
        pathlib.Path.unlink = refuse
        try:
            downloader.prune(self.cache, max_gb=1.0 / 1024, min_age_minutes=30)
        finally:
            pathlib.Path.unlink = real
        self.assertTrue(stuck.exists())

    def test_zero_means_no_size_limit(self):
        # max_gb=0 switches off the size cap only. The age limit is a separate
        # policy and still applies - passing 0 for both is "keep everything".
        old = self._clip("old.mp4", 8, 6000)
        removed, _ = downloader.prune(self.cache, max_gb=0, max_age_hours=0)
        self.assertEqual(removed, 0)
        self.assertTrue(old.exists())

    def test_a_clip_past_the_age_limit_goes_even_with_room_to_spare(self):
        # This is the one that bounds the cache when the app is left closed:
        # the size cap never fires because nothing is being downloaded.
        stale = self._clip("stale.mp4", 1, 60 * 25)
        removed, mb = downloader.prune(self.cache, max_gb=0, max_age_hours=24)
        self.assertFalse(stale.exists())
        self.assertEqual(removed, 1)
        self.assertGreater(mb, 0)

    def test_a_clip_inside_the_age_limit_stays(self):
        keep = self._clip("keep.mp4", 1, 60 * 23)
        removed, _ = downloader.prune(self.cache, max_gb=0, max_age_hours=24)
        self.assertTrue(keep.exists())
        self.assertEqual(removed, 0)

    def test_the_age_limit_does_not_wait_for_the_grace_period(self):
        # CACHE_MIN_AGE_MIN protects a clip from being evicted to make room.
        # It is not a reprieve from expiry - an old clip is old either way.
        stale = self._clip("stale.mp4", 1, 60 * 30)
        downloader.prune(self.cache, max_gb=10, min_age_minutes=60 * 24 * 7,
                         max_age_hours=24)
        self.assertFalse(stale.exists())

    def test_an_expired_clip_that_will_not_delete_is_left_alone(self):
        # Same protection as the size sweep: on Windows the file mpv has open
        # refuses to unlink, and a clip on screen at 25 hours old is still a
        # clip on screen.
        stuck = self._clip("playing.mp4", 1, 60 * 48)
        real = pathlib.Path.unlink
        def refuse(self_, *a, **k):
            if self_.name == "playing.mp4":
                raise OSError("in use")
            return real(self_, *a, **k)
        pathlib.Path.unlink = refuse
        try:
            removed, _ = downloader.prune(self.cache, max_gb=0, max_age_hours=24)
        finally:
            pathlib.Path.unlink = real
        self.assertTrue(stuck.exists())
        self.assertEqual(removed, 0)

    def test_the_age_limit_defaults_to_a_day(self):
        self.assertEqual(config.CACHE_MAX_AGE_H, 24.0)
        self.assertEqual(
            inspect.signature(downloader.prune).parameters[
                "max_age_hours"].default,
            config.CACHE_MAX_AGE_H)

    def test_the_age_limit_is_settable(self):
        with _env(CHUUTUBE2SPOUT_CACHE_MAX_AGE_H="72"):
            self.assertEqual(float(config._num("CACHE_MAX_AGE_H", "24")), 72.0)

    def test_a_manifest_whose_files_are_gone_is_cleaned_up(self):
        clip = self._clip("gone.mp4", 1, 600)
        manifest = downloader.manifest_path(self.cache, "https://x/watch", 1080)
        downloader.write_manifest(manifest, LocalClip(video_path=str(clip)))
        clip.unlink()
        downloader.prune(self.cache, max_gb=0.001, min_age_minutes=0)
        self.assertFalse(manifest.exists())

    def test_a_manifest_still_pointing_at_a_file_survives(self):
        clip = self._clip("kept.mp4", 1, 1)
        manifest = downloader.manifest_path(self.cache, "https://y/watch", 1080)
        downloader.write_manifest(manifest, LocalClip(video_path=str(clip)))
        downloader.prune(self.cache, max_gb=10, min_age_minutes=30)
        self.assertTrue(manifest.exists())

    def test_a_missing_cache_directory_is_not_an_error(self):
        shutil.rmtree(self.cache, ignore_errors=True)
        self.assertEqual(downloader.prune(self.cache, max_gb=1), (0, 0.0))


class VersionNumberingTests(unittest.TestCase):
    """1.0 means published. Nothing else is allowed to mean it.

    The failure this prevents is dull and entirely plausible: 0.9.9 ships,
    the upload slips a few weeks, someone bumps the number out of habit, and
    now there is a 1.0 on a hard disk that nobody can download. After 0.9.9 the
    numbers grow a fourth part instead."""

    def test_the_version_is_all_numbers_and_dots(self):
        # The git tag, the zip name and PyInstaller all carry this string.
        self.assertRegex(chuutube2spout.__version__, r"^\d+(\.\d+)+$")

    def test_a_four_part_version_parses(self):
        self.assertEqual(chuutube2spout.version_tuple("0.9.9.1"), (0, 9, 9, 1))

    def test_the_fourth_part_sorts_between_its_neighbours(self):
        step = chuutube2spout.version_tuple
        self.assertLess(step("0.9.9"), step("0.9.9.1"))
        self.assertLess(step("0.9.9.1"), step("0.9.9.2"))
        self.assertLess(step("0.9.9.2"), step("1.0"))

    def test_the_ordering_string_comparison_gets_wrong(self):
        # "0.9.10" < "0.9.9" as strings, which is the whole reason the helper
        # exists rather than the numbers being compared where they are used.
        step = chuutube2spout.version_tuple
        self.assertLess(step("0.9.9"), step("0.9.10"))
        self.assertTrue("0.9.10" < "0.9.9", "the string trap this avoids")

    def test_the_current_version_is_below_one_until_it_is_published(self):
        # build.ps1 runs the suite before it builds, so this is the guard.
        if not chuutube2spout.PUBLISHED:
            self.assertLess(chuutube2spout.version_tuple(), (1, 0),
                            "1.0 is reserved for the release that actually "
                            "gets uploaded - use 0.9.9.1, 0.9.9.2, and on")

    def test_published_since_one_point_oh(self):
        # 1.0 is the published release (K, 1.0). From here on every version
        # is at least 1.0, and the flag stays True.
        self.assertTrue(chuutube2spout.PUBLISHED)
        self.assertGreaterEqual(chuutube2spout.version_tuple(), (1, 0))

    def test_the_rule_is_written_down_where_the_number_is(self):
        # A convention nobody can find is not a convention.
        source = Path(chuutube2spout.__file__).read_text(encoding="utf-8")
        head = source[:source.index("__version__ =")]
        self.assertIn("0.9.9.1", head)
        self.assertIn("PUBLISHED", head)


class StartupExpiryTests(unittest.TestCase):
    """prune() only ran after a download, so a cache left closed never aged.

    The case that matters: a set on Friday, clear-on-exit off, the app opened
    again on Monday. Nothing had run in between, so the folder was exactly as
    it was left - and stayed that way until the next clip finished downloading.
    """

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp(prefix="chuutube-startup-"))

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def _clip(self, name, hours):
        f = self.cache / name
        f.write_bytes(b"\0" * 4096)
        when = time.time() - hours * 3600
        os.utime(f, (when, when))
        return f

    @contextlib.contextmanager
    def _cache_is(self, **overrides):
        old = {k: getattr(config, k) for k in overrides}
        for k, v in overrides.items():
            setattr(config, k, v)
        try:
            yield
        finally:
            for k, v in old.items():
                setattr(config, k, v)

    def test_an_old_clip_is_gone_by_the_time_the_window_opens(self):
        stale = self._clip("stale.mp4", 30)
        with self._cache_is(CACHE_DIR=str(self.cache), CACHE_MAX_AGE_H=24.0):
            app_mod.expire_cache()
        self.assertFalse(stale.exists())

    def test_a_recent_clip_survives_startup(self):
        keep = self._clip("keep.mp4", 2)
        with self._cache_is(CACHE_DIR=str(self.cache), CACHE_MAX_AGE_H=24.0):
            app_mod.expire_cache()
        self.assertTrue(keep.exists())

    def test_the_age_limit_off_means_startup_touches_nothing(self):
        stale = self._clip("stale.mp4", 500)
        with self._cache_is(CACHE_DIR=str(self.cache), CACHE_MAX_AGE_H=0.0):
            app_mod.expire_cache()
        self.assertTrue(stale.exists())

    def test_a_cache_that_is_not_there_yet_is_not_an_error(self):
        # First run: the folder is created by the first download.
        shutil.rmtree(self.cache, ignore_errors=True)
        with self._cache_is(CACHE_DIR=str(self.cache), CACHE_MAX_AGE_H=24.0):
            app_mod.expire_cache()          # must not raise

    def test_startup_does_not_apply_the_size_cap(self):
        # Nothing is playing yet, so "least recently used" has no protection
        # from the file about to go on screen. Size is the download path's job.
        source = Path(app_mod.__file__).read_text(encoding="utf-8")
        body = source[source.index("def expire_cache"):]
        self.assertIn("max_gb=0", body.split("def ")[1])

    def test_it_runs_before_the_window_is_built(self):
        source = Path(app_mod.__file__).read_text(encoding="utf-8")
        body = source[source.index("def main"):]
        self.assertLess(body.index("expire_cache()"),
                        body.index("QApplication"))

    def test_a_failure_to_tidy_does_not_stop_the_app(self):
        # A locked cache folder is not a reason to refuse to start.
        source = Path(app_mod.__file__).read_text(encoding="utf-8")
        body = source[source.index("def main"):]
        after = body[body.index("expire_cache()"):]
        self.assertTrue(after.lstrip().startswith("expire_cache()"))
        self.assertIn("except Exception:", after[:120])


class ClearOnExitTests(unittest.TestCase):
    """Closing the app empties the cache, unless told not to."""

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp(prefix="chuutube-exit-"))
        (self.cache / "a.mp4").write_bytes(b"\0" * 2048)
        (self.cache / "b.webm").write_bytes(b"\0" * 2048)

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_everything_goes(self):
        removed, mb = downloader.clear(self.cache)
        self.assertEqual(removed, 2)
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertGreater(mb, 0)

    def test_a_locked_file_does_not_break_shutdown(self):
        # Windows can hold a handle a moment after mpv is torn down. Failing a
        # shutdown over that would be worse than leaving one file behind - the
        # size cap takes it next run.
        real = pathlib.Path.unlink
        def refuse(self_, *a, **k):
            if self_.name == "a.mp4":
                raise OSError("still open")
            return real(self_, *a, **k)
        pathlib.Path.unlink = refuse
        try:
            removed, _ = downloader.clear(self.cache)
        finally:
            pathlib.Path.unlink = real
        self.assertEqual(removed, 1)
        self.assertTrue((self.cache / "a.mp4").exists())

    def test_a_missing_cache_is_not_an_error(self):
        shutil.rmtree(self.cache, ignore_errors=True)
        self.assertEqual(downloader.clear(self.cache), (0, 0.0))

    def test_the_setting_is_off_by_default_and_can_be_turned_on(self):
        # It used to default on, because nothing else bounded the folder.
        # CACHE_MAX_AGE_H does that now, and a cache that survives the app is
        # what makes the second load of a clip instant.
        self.assertFalse(config.CLEAR_CACHE_ON_EXIT)
        with _env(CHUUTUBE2SPOUT_CLEAR_CACHE_ON_EXIT="1"):
            self.assertTrue(config._flag("CLEAR_CACHE_ON_EXIT", False))

    def test_the_size_cap_defaults_to_ten_gb(self):
        self.assertEqual(config.CACHE_MAX_GB, 10.0)

    def test_the_size_cap_is_settable(self):
        with _env(CHUUTUBE2SPOUT_CACHE_MAX_GB="2.5"):
            self.assertEqual(float(config._num("CACHE_MAX_GB", "10")), 2.5)

    def test_clearing_happens_after_mpv_is_shut_down(self):
        # Order matters: before surface.shutdown() every file is still open and
        # nothing would delete.
        # encoding= is not optional here: ui.py contains the transport
        # glyphs, and read_text() on Windows defaults to cp1252.
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source[source.index("def closeEvent"):]
        self.assertLess(body.index("surface.shutdown()"),
                        body.index("clear_cache_box.isChecked()"))


class NonAsciiTitleTests(unittest.TestCase):
    """Half the material this plays has a Japanese title.

    write_text() on Windows defaults to the locale codec, and an encoding
    failure raises UnicodeEncodeError - a ValueError, not an OSError - so a
    plain `except OSError` around it would not catch it and the download would
    die on the title rather than on anything to do with video."""

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp(prefix="chuutube-title-"))

    def tearDown(self):
        shutil.rmtree(self.cache, ignore_errors=True)

    def test_a_japanese_title_round_trips(self):
        clip = LocalClip(video_path=str(self.cache / "v.mp4"),
                         title="ハロー・ワールド 【MV】", height=1080)
        Path(clip.video_path).write_bytes(b"x")
        manifest = downloader.manifest_path(self.cache, "https://x/watch", 1080)
        downloader.write_manifest(manifest, clip)
        back = downloader.read_manifest(manifest)
        self.assertIsNotNone(back)
        self.assertEqual(back.title, "ハロー・ワールド 【MV】")

    def test_emoji_and_rtl_survive_too(self):
        clip = LocalClip(video_path=str(self.cache / "w.mp4"),
                         title="عربى 🎛️ mix", height=720)
        Path(clip.video_path).write_bytes(b"x")
        manifest = downloader.manifest_path(self.cache, "https://y/watch", 720)
        downloader.write_manifest(manifest, clip)
        self.assertEqual(downloader.read_manifest(manifest).title,
                         "عربى 🎛️ mix")

    def test_an_unwritable_manifest_does_not_kill_the_download(self):
        clip = LocalClip(video_path=str(self.cache / "z.mp4"), title="x")
        Path(clip.video_path).write_bytes(b"x")
        bad = self.cache / "no-such-dir" / "m.json"
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            downloader.write_manifest(bad, clip)      # must not raise
        self.assertIn("could not write cache manifest", out.getvalue())


class FrozenLayoutTests(unittest.TestCase):
    """settings.txt and cache/ must sit next to the exe, not inside _internal.

    Path(__file__).parent.parent is the project root when running from source
    and PyInstaller's private `_internal` folder when frozen. The first build
    resolved both files that way, so the shipped exe read its settings from a
    directory no user would ever put a file in, and wrote a 10 GB cache into a
    folder that the next unzip replaces. Both silent.
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.saved_env = os.environ.get("CHUUTUBE2SPOUT_CACHE")
        os.environ.pop("CHUUTUBE2SPOUT_CACHE", None)

    def tearDown(self):
        for attr in ("frozen", "_MEIPASS"):
            if hasattr(sys, attr):
                delattr(sys, attr)
        if self.saved_env is not None:
            os.environ["CHUUTUBE2SPOUT_CACHE"] = self.saved_env
        importlib.reload(config)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _freeze(self):
        """Pretend to be a PyInstaller onedir build living in self.tmp."""
        exe = self.tmp / "ChuuTube2Spout.exe"
        exe.write_bytes(b"MZ")
        sys.frozen = True
        sys._MEIPASS = str(self.tmp / "_internal")
        return exe

    def test_the_exe_folder_is_searched_before_the_package_folder(self):
        exe = self._freeze()
        saved = sys.executable
        sys.executable = str(exe)
        try:
            importlib.reload(config)
            self.assertEqual(config._roots()[0], self.tmp)
        finally:
            sys.executable = saved

    def test_settings_next_to_the_exe_are_found(self):
        exe = self._freeze()
        (self.tmp / "settings.txt").write_text("WINDOW_SCALE=1.2\n",
                                               encoding="utf-8")
        saved = sys.executable
        sys.executable = str(exe)
        # The real lookup, for once: stubs_bootstrap turns it off so the
        # developer's own settings.txt cannot reach the other tests.
        override = os.environ.pop("CHUUTUBE2SPOUT_SETTINGS_FILE", None)
        try:
            importlib.reload(config)
            self.assertEqual(pathlib.Path(config.SETTINGS_FILE),
                             self.tmp / "settings.txt")
            self.assertEqual(os.environ.get("CHUUTUBE2SPOUT_WINDOW_SCALE"),
                             "1.2")
        finally:
            sys.executable = saved
            os.environ.pop("CHUUTUBE2SPOUT_WINDOW_SCALE", None)
            if override is not None:
                os.environ["CHUUTUBE2SPOUT_SETTINGS_FILE"] = override
            importlib.reload(config)

    def test_the_cache_lands_beside_the_exe(self):
        if config._raw("CACHE"):
            self.skipTest("this checkout's settings.txt pins CACHE explicitly")
        exe = self._freeze()
        saved = sys.executable
        sys.executable = str(exe)
        try:
            importlib.reload(config)
            self.assertEqual(pathlib.Path(config.CACHE_DIR),
                             self.tmp / "cache")
            self.assertNotIn("_internal", config.CACHE_DIR)
        finally:
            sys.executable = saved

    def test_an_explicit_cache_setting_still_wins(self):
        exe = self._freeze()
        saved = sys.executable
        sys.executable = str(exe)
        os.environ["CHUUTUBE2SPOUT_CACHE"] = str(self.tmp / "elsewhere")
        try:
            importlib.reload(config)
            self.assertEqual(pathlib.Path(config.CACHE_DIR),
                             self.tmp / "elsewhere")
        finally:
            sys.executable = saved
            os.environ.pop("CHUUTUBE2SPOUT_CACHE", None)

    def test_running_from_source_is_unchanged(self):
        if config._raw("CACHE"):
            self.skipTest("this checkout's settings.txt pins CACHE explicitly")
        importlib.reload(config)
        root = pathlib.Path(config.__file__).resolve().parent.parent
        self.assertEqual(config._roots()[0], root)
        self.assertEqual(pathlib.Path(config.CACHE_DIR), root / "cache")


class DoctorYtdlpTests(unittest.TestCase):
    """doctor must report the yt-dlp the app would actually run.

    find_ytdlp() falls through to shutil.which(). In a frozen build that names
    whatever copy happens to be installed on the machine - a different version,
    without the PO token plugin - while the app runs the one inside the exe. On
    a clean machine it reports FAIL for a build that works.
    """

    def test_doctor_asks_bootstrap_how_to_invoke_ytdlp(self):
        source = Path(doctor.__file__).read_text(encoding="utf-8")
        self.assertIn("ytdlp = ytdlp_command()", source)
        self.assertNotIn("ytdlp = find_ytdlp()", source)

    def test_the_version_and_help_probes_expand_the_prefix(self):
        source = Path(doctor.__file__).read_text(encoding="utf-8")
        self.assertIn('[*ytdlp, "--version"]', source)
        self.assertIn('[*ytdlp, "--help"]', source)

    def test_a_bundled_copy_is_named_as_such(self):
        self.assertIn("bundled",
                      doctor._where(["C:\\app\\x.exe", "--yt-dlp"]))

    def test_a_plain_path_is_reported_verbatim(self):
        self.assertEqual(doctor._where(["C:\\tools\\yt-dlp.exe"]),
                         "C:\\tools\\yt-dlp.exe")


class ScriptModeVerificationTests(unittest.TestCase):
    """Script mode must be proven to work before it is advertised.

    3.4.2 shipped a node_modules that existed, weighed 533 MB and resolved
    nothing: deno lays the tree out as symlinks into `.deno/`, and Copy-Item
    and Compress-Archive both dereference symlinks, so every package became a
    real directory sitting nowhere near its own dependencies. deno reported
    "Could not find package 'form-data'", the old warm-up discarded that exit
    code and printed "warmed up", and every download was capped at 360p with
    the app insisting the generator was ready.
    """

    def setUp(self):
        self.provider = potprovider.PotProvider()
        self.saved = {name: getattr(potprovider, name)
                      for name in ("script_probe", "install_script_deps")}

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(potprovider, name, value)

    # --- extractor_args ----------------------------------------------------
    def test_a_verified_script_home_is_advertised(self):
        self.provider._script_home = "S"
        self.provider._script_ok = True
        self.provider._prepared.set()
        self.assertIn("youtubepot-bgutilscript:server_home=S",
                      self.provider.extractor_args())

    def test_a_broken_script_home_is_not_advertised(self):
        self.provider._script_home = None
        self.provider._script_ok = False
        self.provider._prepared.set()
        args = " ".join(self.provider.extractor_args())
        self.assertNotIn("bgutilscript", args)

    def test_an_unproven_script_home_is_not_advertised(self):
        self.provider._script_home = "S"
        self.provider._script_ok = None
        self.provider._prepared.set()          # do not block the suite
        args = " ".join(self.provider.extractor_args())
        self.assertNotIn("bgutilscript", args)

    def test_active_is_false_once_script_mode_is_proven_broken(self):
        self.provider._script_home = "S"
        self.provider._script_ok = False
        self.assertFalse(self.provider.active)

    def test_active_stays_true_while_still_preparing(self):
        self.provider._script_home = "S"
        self.provider._script_ok = None
        self.assertTrue(self.provider.active)

    # --- _prepare_script ---------------------------------------------------
    def test_a_working_generator_needs_no_install(self):
        calls = []
        potprovider.script_probe = lambda home: (True, "1.3.1")
        potprovider.install_script_deps = lambda home: calls.append(home) or (True, "")
        self.provider._prepare_script("S")
        self.assertTrue(self.provider._script_ok)
        self.assertEqual(calls, [])

    def test_a_missing_tree_is_built_once_and_then_rechecked(self):
        results = iter([(False, "Could not find package 'form-data'"),
                        (True, "1.3.1")])
        installs = []
        potprovider.script_probe = lambda home: next(results)
        potprovider.install_script_deps = lambda home: (installs.append(home)
                                                        or (True, ""))
        self.provider._prepare_script("S")
        self.assertTrue(self.provider._script_ok)
        self.assertEqual(installs, ["S"])
        self.assertEqual(self.provider._script_home, "S")

    def test_a_generator_that_cannot_be_repaired_gives_up_and_falls_back(self):
        fell_back = []
        potprovider.script_probe = lambda home: (False, "still broken")
        potprovider.install_script_deps = lambda home: (False, "no network")
        self.provider._start_server = lambda: fell_back.append(True) or False
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.provider._prepare_script("S")
        self.assertFalse(self.provider._script_ok)
        self.assertIsNone(self.provider._script_home)
        self.assertEqual(fell_back, [True])
        self.assertIn("still broken", out.getvalue())

    def test_preparation_always_releases_anything_waiting_on_it(self):
        potprovider.script_probe = lambda home: (False, "broken")
        potprovider.install_script_deps = lambda home: (False, "nope")
        self.provider._start_server = lambda: False
        with contextlib.redirect_stderr(io.StringIO()):
            self.provider._prepare_script("S")
        self.assertTrue(self.provider._prepared.is_set())


class ScriptProbeTests(unittest.TestCase):
    """script_probe reports the exit code. That is the whole point of it."""

    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        (self.home / "src").mkdir()
        (self.home / "src" / "generate_once.ts").write_text("//", encoding="utf-8")
        self.saved_which = potprovider.shutil.which
        self.saved_run = potprovider.subprocess.run

    def tearDown(self):
        potprovider.shutil.which = self.saved_which
        potprovider.subprocess.run = self.saved_run
        shutil.rmtree(self.home, ignore_errors=True)

    def _deno(self, returncode, stderr=""):
        potprovider.shutil.which = lambda name: "deno.exe"

        class Done:
            pass
        done = Done()
        done.returncode, done.stdout, done.stderr = returncode, "1.3.1", stderr
        potprovider.subprocess.run = lambda *a, **k: done

    def test_exit_zero_is_ready(self):
        self._deno(0)
        ok, detail = _REAL_SCRIPT_PROBE(str(self.home))
        self.assertTrue(ok)

    def test_a_failed_run_is_reported_with_its_last_words(self):
        self._deno(1, "some noise\nerror: Could not find package 'form-data'")
        ok, detail = _REAL_SCRIPT_PROBE(str(self.home))
        self.assertFalse(ok)
        self.assertIn("form-data", detail)

    def test_no_deno_is_not_ready(self):
        potprovider.shutil.which = lambda name: None
        ok, detail = _REAL_SCRIPT_PROBE(str(self.home))
        self.assertFalse(ok)
        self.assertIn("deno", detail)

    def test_a_missing_script_is_not_ready(self):
        potprovider.shutil.which = lambda name: "deno.exe"
        (self.home / "src" / "generate_once.ts").unlink()
        ok, detail = _REAL_SCRIPT_PROBE(str(self.home))
        self.assertFalse(ok)


class UrlCheckReportTests(unittest.TestCase):
    """check.bat is what a tester runs when something is already wrong.

    So it being wrong is expensive twice over: once for the time, and once
    because its advice is followed by someone who has no way to tell it is bad.
    All three of these came out of one tester's report.
    """

    def test_default_is_listed_once(self):
        # It is an ordinary entry in CLIENT_CHAIN now; prepending None on top
        # of it printed the row twice and ended "Playable via: default, default".
        clients = urlcheck._clients()
        self.assertEqual(clients.count(None), 1)

    def test_the_auto_spellings_collapse_into_it(self):
        with patched(urlcheck, "CLIENT_CHAIN", ["default", "auto", "mweb"]):
            clients = urlcheck._clients()
        self.assertEqual(clients.count(None), 1)
        self.assertIn("mweb", clients)

    def test_a_chain_without_it_still_gets_it(self):
        with patched(urlcheck, "CLIENT_CHAIN", ["mweb", "tv"]):
            clients = urlcheck._clients()
        self.assertIn(None, clients)

    def test_the_order_is_the_one_the_app_uses(self):
        # The report is only useful if it walks the chain the app walks.
        with patched(urlcheck, "CLIENT_CHAIN", ["default", "mweb", "tv"]):
            clients = urlcheck._clients()
        self.assertEqual(clients, [None, "mweb", "tv"])

    def test_it_never_advises_pinning_default(self):
        # PLAYER_CLIENT=default would pass player_client=default to yt-dlp and
        # there is no such client - the advice would break a working setup.
        source = Path(urlcheck.__file__).read_text(encoding="utf-8")
        self.assertIn('pinnable = [p for p in playable if p != "default"]',
                      source)
        self.assertIn("nothing to set", source)

    def test_a_probe_past_the_end_is_not_a_refusal(self):
        # 416 on a short file means the byte does not exist, not that it is
        # being withheld. It was printed as an error next to a perfectly
        # playable 360p stream.
        source = Path(urlcheck.__file__).read_text(encoding="utf-8")
        self.assertIn('_PAST_END in detail', source)
        self.assertIn('past end', source)

    def test_past_the_end_does_not_count_against_the_verdict(self):
        source = Path(urlcheck.__file__).read_text(encoding="utf-8")
        body = source.split("for offset in points:")[1].split("size =")[0]
        # the continue must come before all_ok is touched
        self.assertLess(body.index("continue"), body.index("all_ok = all_ok"))

    def test_yt_dlp_does_not_print_its_own_error_over_the_summary(self):
        # quiet suppresses info but not errors, so the raw ERROR block landed
        # above check.bat's tidy one-line version of the same thing.
        source = Path(urlcheck.__file__).read_text(encoding="utf-8")
        self.assertIn("quiet_errors=True", source)
        resolver_source = Path(resolver.__file__).read_text(encoding="utf-8")
        self.assertIn("class _Silent:", resolver_source)

    def test_the_app_still_sees_yt_dlp_errors(self):
        # Only the checker silences them; silencing them everywhere would take
        # away the one place a resolve failure explains itself.
        resolver_source = Path(resolver.__file__).read_text(encoding="utf-8")
        self.assertIn("quiet_errors: bool = False", resolver_source)


class ProgramSenderRemovedTests(unittest.TestCase):
    """LIVE and the program sender are gone; nothing should still mention them."""

    def setUp(self):
        self.w = MainWindow()

    def test_no_live_button(self):
        # vars(), not hasattr(): the test stubs answer any attribute at all.
        for panel in self.w.panels:
            self.assertNotIn("program_btn", vars(panel))
        self.assertNotIn("program_send_box", vars(self.w))

    def test_no_setting_for_it(self):
        self.assertFalse(hasattr(config, "SENDER_PROGRAM"))

    def test_nothing_in_the_window_talks_about_it(self):
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        shown = "\n".join(line for line in source.splitlines()
                          if not line.lstrip().startswith("#"))
        for word in ("LIVE", "program sender", "SENDER_PROGRAM", "program_index"):
            self.assertNotIn(word, shown)

    def test_the_status_bar_lists_two_senders(self):
        self.assertNotIn("program", self.w.senders_label.full_text())


class VideoTitleTests(unittest.TestCase):
    """The title is the only part of a status line a VJ recognises.

    "1080p avc1" is true of everything in the set. The title has been carried
    on LocalClip all along and was never shown.
    """

    def _stream(self, title):
        return resolver.Stream(video_url="/x.mp4", direct=True, client="local",
                               height=1080, title=title)

    def test_the_title_is_in_the_status(self):
        text = player_mod._describe(self._stream("Breaking Dimensions"))
        self.assertIn("Breaking Dimensions", text)

    def test_a_clip_with_no_title_is_described_as_before(self):
        stream = self._stream("")
        self.assertEqual(player_mod._describe(stream), stream.label)

    def test_whitespace_is_not_a_title(self):
        stream = self._stream("   ")
        self.assertEqual(player_mod._describe(stream), stream.label)

    def test_a_long_title_does_not_widen_the_panel(self):
        # A QLabel's minimum width is its full text, and two panels sit side by
        # side - so one clip with a paragraph for a name would push the window
        # wider than the screen.
        panel = MainWindow().panels[0]
        panel._on_status("x" * 4000)
        self.assertLessEqual(panel.status_label.minimumWidth(), 200)
        self.assertLess(len(panel.status_label.text()), 4000,
                        "the text has to be shortened, not just tooltipped")

    def test_the_whole_title_is_still_available(self):
        panel = MainWindow().panels[0]
        panel._on_status("A Very Long Clip Name That Gets Cut Off")
        self.assertEqual(panel.status_label.toolTip(),
                         "A Very Long Clip Name That Gets Cut Off")

    def test_it_is_re_elided_when_the_panel_resizes(self):
        # The panel used to do this itself, against a plain QLabel. It lives in
        # ElidedLabel now - the label is the thing that knows its own width, so
        # it is the thing that should decide how much text fits.
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source.split("class ElidedLabel")[1].split("\nclass ")[0]
        self.assertIn("def resizeEvent", body)
        after = body.split("def resizeEvent")[1]
        self.assertIn("_re_elide", after)

    def test_the_panel_no_longer_keeps_its_own_copy(self):
        # Two places holding the full text is how one of them goes stale.
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_status_full", source)


class UnusableModeTests(unittest.TestCase):
    """A setting that cannot work must refuse, not fail quietly.

    MODE is user-settable and two of its three documented values do not work
    for YouTube: stream and hybrid both resolve through yt-dlp's Python API,
    which has no PO token provider, so they are offered only an HLS manifest
    whose segments 403. Someone who reads the list and tries one gets an app
    that extracts, plays nothing, and explains itself in a log they are not
    watching. That is a trap, not an option.
    """

    def test_the_working_mode_is_accepted(self):
        with patched(config, "MODE", "download"):
            self.assertIsNone(config.mode_problem())

    def test_stream_is_refused_with_the_reason(self):
        with patched(config, "MODE", "stream"):
            problem = config.mode_problem()
        self.assertIsNotNone(problem)
        self.assertIn("PO token", problem)
        self.assertIn("settings.txt", problem)

    def test_hybrid_is_refused_too(self):
        with patched(config, "MODE", "hybrid"):
            self.assertIsNotNone(config.mode_problem())

    def test_a_typo_is_refused_and_lists_what_works(self):
        with patched(config, "MODE", "dowload"):
            problem = config.mode_problem()
        self.assertIn("download", problem)

    def test_config_still_imports_with_a_bad_mode(self):
        # Raising here would take doctor, the yt-dlp re-entry path and the whole
        # test suite down with it - everything that has to keep working in order
        # to diagnose the problem.
        source = Path(config.__file__).read_text(encoding="utf-8")
        body = source.split("def mode_problem")[1].split("\n\n\n")[0]
        self.assertNotIn("raise", body)

    def test_the_app_refuses_before_doing_anything_expensive(self):
        source = Path(app_mod.__file__).read_text(encoding="utf-8")
        body = source.split("problem = mode_problem()")[1]
        self.assertIn("return 2", body)
        # and before the window is built
        self.assertLess(source.index("problem = mode_problem()"),
                        source.index("from .ui import MainWindow"))

    def test_the_dormant_relay_says_so_at_the_top(self):
        # The liability was never the code - it was a confident docstring that
        # would convince a reader (including a future me) that stream mode works.
        source = Path(headerproxy.__file__).read_text(encoding="utf-8")
        head = source[:1200]
        self.assertIn("DORMANT", head)
        self.assertIn("never called", head)

    def test_the_revival_trigger_is_specific(self):
        # "Retire when mpv's curl migration settles" was not a task: no date, no
        # trigger, no first step - and curl is only half the problem.
        source = Path(headerproxy.__file__).read_text(encoding="utf-8")
        # Whitespace-normalised: the sentence wraps, and a test that breaks on
        # reflowing a comment is a test nobody keeps.
        flat = " ".join(source.split())
        self.assertIn("yt-dlp Python API gains a PO token provider", flat)
        self.assertIn("no trigger and no first step", flat)


class FirstRunInstallTests(unittest.TestCase):
    """The app fetches its own runtime rather than sending you to find a .bat.

    The release ships no libmpv on purpose, so a first run with no engine is
    the expected path, not a fault. Someone who just double-clicked the exe
    already has a window open; asking them to go back to the folder and find a
    batch file is a poor first impression of a tool they are trying at a venue.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="chuutube-first-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_it_runs_the_installer_that_ships_with_the_release(self):
        (self.dir / "setup.ps1").write_text("x", encoding="utf-8")
        cmd = firstrun.runtime_command(self.dir)
        self.assertIn("-RuntimeOnly", cmd)
        self.assertIn(str(self.dir / "setup.ps1"), cmd)

    def test_it_does_not_inherit_the_users_powershell_profile(self):
        # A profile that prints a banner or changes directory is not something
        # an installer should pick up.
        (self.dir / "setup.ps1").write_text("x", encoding="utf-8")
        self.assertIn("-NoProfile", firstrun.runtime_command(self.dir))

    def test_no_installer_means_no_offer(self):
        self.assertIsNone(firstrun.runtime_command(self.dir))

    def test_it_is_only_offered_from_a_release(self):
        # From source, plain setup.ps1 is the right command and does more;
        # offering the cut-down one there would point at the wrong thing.
        self.assertFalse(firstrun.should_offer(),
                         "running from source - must not offer")

    def test_the_fetch_is_not_reimplemented_in_python(self):
        # setup.ps1 already knows the projects, the checksums, the trusted
        # hosts and the archive layouts, and it is the version that has been
        # tested. Two implementations of a security check is how one rots.
        source = Path(firstrun.__file__).read_text(encoding="utf-8")
        for forbidden in ("urlopen", "requests.get", "Invoke-WebRequest",
                          "sha256"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_a_failed_import_is_actually_retried(self):
        # Python drops a module whose import raised, but the packages around it
        # can be left pointing at nothing - so the second attempt has to start
        # clean or it hands back the wreckage of the first.
        source = Path(firstrun.__file__).read_text(encoding="utf-8")
        body = source.split("def _forget_failed_imports")[1].split("\nclass ")[0]
        self.assertIn('sys.modules.pop(name, None)', body)
        self.assertIn('name == "mpv"', body)

    def test_the_failure_note_points_at_the_log_that_is_actually_there(self):
        # The log box is added to the layout after the explanation, so it
        # renders below it. Telling someone to look above sends them to the
        # headline, which says nothing useful.
        source = Path(firstrun.__file__).read_text(encoding="utf-8")
        self.assertIn("The log below says why", source)
        self.assertNotIn("log above", source)

    def test_the_dll_search_path_is_redone_after_installing(self):
        # add_dll_directory was called before the DLL existed; the retry needs
        # it called again or the file is there and still not found.
        source = Path(firstrun.__file__).read_text(encoding="utf-8")
        self.assertIn("prepare_native_paths()", source)

    def test_the_entry_point_does_not_loop_on_a_failed_install(self):
        source = Path(app_mod.__file__).read_text(encoding="utf-8")
        body = source.split("from .firstrun import offer_runtime_install")[1]
        self.assertIn("Do not loop", body)

    def test_declining_falls_back_to_the_written_instructions(self):
        source = Path(app_mod.__file__).read_text(encoding="utf-8")
        body = source.split("from .firstrun import offer_runtime_install")[1]
        self.assertIn("mpv_import_error_help(exc)", body)

    def test_the_batch_file_is_still_there_as_a_fallback(self):
        # When the GUI cannot start at all, or the download fails and the error
        # needs reading in a console.
        source = (Path(__file__).resolve().parent.parent / "build.ps1"
                  ).read_text(encoding="utf-8")
        self.assertIn("setup-runtime.bat", source)


class ReleasePackagingTests(unittest.TestCase):
    """The zip carries our own work, and nobody else's binaries.

    libmpv's Windows builds are GPL because of their codec components, and both
    halves of the PO token generator are GPL-3.0. Putting them in the zip means
    redistributing them, which makes whoever publishes the release answerable
    for terms their authors set - including, for libmpv, a licence its
    distributor does not state. Each user fetches those from the projects
    themselves instead, verified against checksums.txt.

    This also retires an entire class of bug: deno lays node_modules out as a
    farm of symlinks into .deno\\, and both Copy-Item and Compress-Archive
    dereference symlinks - so the copy looked complete, weighed 533 MB,
    resolved nothing, and capped the release at 360p. It shipped that way once.
    """

    def _build(self):
        return (Path(__file__).resolve().parent.parent / "build.ps1")

    def test_no_native_binary_is_copied_into_the_release(self):
        # This used to assert the names did not appear in build.ps1 at all,
        # which was a proxy for "nothing copies them in" and stopped being one
        # the moment the script grew a guard that has to NAME them to refuse
        # them. Checking the property directly instead: no line that mentions
        # one of these may also be a copy into the build.
        source = self._build().read_text(encoding="utf-8")
        moves = ("Copy-Item", "Move-Item", "Compress-Archive", "New-Item")
        for name in ("mpv-2.dll", "libmpv-2.dll", "deno.exe", "bgutil-pot.exe"):
            for line in source.splitlines():
                stripped = line.lstrip()
                if stripped.startswith(("#", "REM")) or name not in line:
                    continue
                with self.subTest(name=name, line=stripped[:60]):
                    self.assertFalse(
                        any(verb in line for verb in moves),
                        f"build.ps1 puts {name} into the release")

    def test_the_release_ships_settings_txt_made_from_the_template(self):
        # K, 0.9.9.30: one settings file in the zip, not a .example to copy.
        source = self._build().read_text(encoding="utf-8")
        self.assertIn('$template = Join-Path $Root "settings.txt.example"',
                      source)
        self.assertIn("Copy-Item $template $shipped -Force", source)
        docs = source.split('Say "Adding the documents"')[1].split("}")[0]
        self.assertNotIn("settings.txt.example", docs,
                         "the template itself is not shipped any more")

    def test_your_own_settings_never_reach_the_zip(self):
        # The settings.txt in the project folder holds the maintainer's browser
        # choice. Nothing may copy it, and the shipped one must hash-match the
        # template or the build stops.
        source = self._build().read_text(encoding="utf-8")
        self.assertNotIn('Join-Path $Root "settings.txt")', source)
        check = source.split("$want = (Get-FileHash $template")[1]
        check = check.split('Say "Nothing in the build')[0]
        self.assertIn("if ($have -ne $want)", check)
        self.assertIn("Die ", check)

    def test_the_shipped_template_sets_only_the_output_size(self):
        # Shipped as settings.txt, so any uncommented line is a setting every
        # user gets without asking for it. Since 0.9.9.40 exactly three, on
        # purpose (K): a fixed 1920x1080 with transparent bars, because
        # Resolume squashes a clip whose size changes after it was fitted.
        text = (ROOT / "settings.txt.example").read_text(encoding="utf-8")
        live = [ln for ln in text.splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
        self.assertEqual(live, ["SENDER_WIDTH=1920", "SENDER_HEIGHT=1080",
                                "SENDER_BACKGROUND=transparent"])

    def test_each_setting_appears_once(self):
        # The parser keeps the first value it sees; a second line for the same
        # key would look like it works and do nothing.
        text = (ROOT / "settings.txt.example").read_text(encoding="utf-8")
        keys = [m.group(1) for m in
                re.finditer(r"^#?([A-Z_]+)=", text, re.MULTILINE)]
        doubled = sorted({k for k in keys if keys.count(k) > 1})
        self.assertEqual(doubled, [])

    def test_the_generator_source_is_not_copied_either(self):
        # GPL-3.0, and setup.ps1 -RuntimeOnly fetches the copy that matches the
        # installed plugin anyway - a stale one in the zip would be worse.
        source = self._build().read_text(encoding="utf-8")
        self.assertNotIn("robocopy $server", source)

    def test_the_user_is_given_one_command_to_fetch_them(self):
        # A .bat, because the people this is for are VJs rather than PowerShell
        # users, and the default execution policy blocks running a .ps1 directly.
        source = self._build().read_text(encoding="utf-8")
        self.assertIn("setup-runtime.bat", source)
        self.assertIn("setup.ps1 -RuntimeOnly", source)

    def test_setup_and_checksums_travel_with_the_release(self):
        # Without both, setup-runtime.bat has nothing to run and no hashes to
        # check what it downloads against.
        source = self._build().read_text(encoding="utf-8")
        self.assertIn('@("setup.ps1", "checksums.txt")', source)

    def test_the_reason_is_written_down_next_to_it(self):
        source = self._build().read_text(encoding="utf-8")
        self.assertIn("redistributing them", source)

    def test_the_missing_engine_reads_as_step_one_not_a_crash(self):
        # It is the expected first run of a release, so the message has to say
        # what to do rather than report a ctypes failure.
        source = Path(bootstrap.__file__).read_text(encoding="utf-8")
        body = source.split("def mpv_import_error_help")[1]
        self.assertIn("setup-runtime.bat", body)
        self.assertIn('getattr(sys, "frozen", False)', body)


class LoadDebounceTests(unittest.TestCase):
    """One press, one fetch.

    A 1080p load takes seconds and the button looked idle throughout, so
    testers pressed it again. Each press started a full download; the earlier
    results were then discarded as stale, so the only visible effect was that
    loading took two or three times as long.
    """

    def setUp(self):
        self.w = MainWindow()
        self.panel = self.w.panels[0]
        self.calls = []
        self.panel.player.load = lambda url, *a, **k: self.calls.append(url)

    def test_a_second_press_while_loading_is_ignored(self):
        self.panel.url_edit.setText("https://example/watch")
        self.panel.load()
        self.panel.load()
        self.panel.load()
        self.assertEqual(len(self.calls), 1)

    def test_the_guard_does_not_wait_for_the_queued_signal(self):
        # Two clicks can land in one event-loop turn, before loadingChanged has
        # been delivered - so load() must set the flag itself.
        self.panel.url_edit.setText("https://example/watch")
        self.panel.load()
        self.assertTrue(self.panel._loading)

    def test_the_button_says_what_it_is_doing(self):
        self.panel._on_loading(True)
        self.assertFalse(self.panel.load_btn.isEnabled())
        self.assertEqual(self.panel.load_btn.text(), "Loading")
        self.panel._on_loading(False)
        self.assertTrue(self.panel.load_btn.isEnabled())
        self.assertEqual(self.panel.load_btn.text(), "Load")

    def test_a_finished_load_frees_the_button_again(self):
        self.panel.url_edit.setText("https://example/watch")
        self.panel.load()
        self.panel._on_loading(False)
        self.panel.load()
        self.assertEqual(len(self.calls), 2)

    def test_an_empty_field_starts_nothing(self):
        self.panel.url_edit.setText("   ")
        self.panel.load()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.panel._loading)

    def test_every_exit_from_a_load_reports_itself_finished(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        # A path that greys the button out and never emits False strands it.
        # Five since partial playback: a picture from the half-written file
        # is a finished load as far as the button is concerned.
        self.assertEqual(source.count("loadingChanged.emit(False)"), 5)


class SeekBarTests(unittest.TestCase):
    """The bar must survive mpv clearing `duration` mid-clip.

    mpv sets duration to None around loads, seeks and end-of-file. Those were
    passed on as 0.0, which disabled the bar and set its range to zero - and
    nothing re-enabled it, because the next real duration event never came.
    Reported as "the slider cannot be moved".
    """

    def setUp(self):
        self.w = MainWindow()
        self.panel = self.w.panels[0]

    def test_a_real_duration_enables_the_bar(self):
        self.panel._on_duration(212.0)
        self.assertTrue(self.panel.slider.isEnabled())
        self.assertEqual(self.panel.slider.maximum(), 212000)

    def test_a_transient_zero_does_not_strand_it(self):
        self.panel._on_duration(212.0)
        self.panel._on_duration(0.0)
        self.assertTrue(self.panel.slider.isEnabled())
        self.assertEqual(self.panel.slider.maximum(), 212000)

    def test_a_genuinely_live_stream_has_no_bar(self):
        self.panel._on_duration(0.0)
        self.assertFalse(self.panel.slider.isEnabled())

    def test_a_new_load_lets_the_bar_go_back_to_live(self):
        self.panel._on_duration(212.0)
        self.panel.player.load = lambda *a, **k: None
        self.panel.url_edit.setText("https://example/watch")
        self.panel.load()
        self.panel._on_duration(0.0)
        self.assertFalse(self.panel.slider.isEnabled())

    def test_clearing_a_slot_also_resets_it(self):
        self.panel._on_duration(212.0)
        self.panel.clear()
        self.panel._on_duration(0.0)
        self.assertFalse(self.panel.slider.isEnabled())

    def test_the_player_never_forwards_a_none_duration(self):
        seen = []
        player = self.w.players[0]
        player.durationChanged.connect(seen.append)
        player._on_duration("duration", None)
        self.assertEqual(seen, [])
        player._on_duration("duration", 90.0)
        self.assertEqual(seen, [90.0])


class CachedDescriptionTests(unittest.TestCase):
    """Changing quality must not re-pay for the extraction.

    Extraction, the PO token and the JS challenge come to four to eight seconds
    of a load, and none of them depend on which format was asked for - every
    format was listed the first time. --load-info-json replays that answer and
    goes straight to the transfer.
    """

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp(prefix="chuutube-info-"))
        self.addCleanup(shutil.rmtree, self.cache, True)
        self.url = "https://example/watch?v=abc"

    def _write(self, age_minutes=0.0):
        path = downloader.info_path(self.cache, self.url)
        path.write_text("{}", encoding="utf-8")
        if age_minutes:
            when = time.time() - age_minutes * 60
            os.utime(path, (when, when))
        return path

    def test_it_is_keyed_on_the_url_alone(self):
        # The whole point is reuse across qualities, so height must not be in
        # the key - unlike the manifest, which names one downloaded file.
        a = downloader.info_path(self.cache, self.url)
        self.assertEqual(a, downloader.info_path(self.cache, self.url))
        self.assertNotEqual(
            a, downloader.info_path(self.cache, self.url + "&x=1"))

    def test_a_recent_description_is_offered(self):
        self._write()
        self.assertIsNotNone(downloader.fresh_info(self.cache, self.url))

    def test_a_stale_one_is_not(self):
        # The media URLs in it are signed and expire; replaying a dead one
        # costs an attempt.
        self._write(age_minutes=downloader.INFO_CACHE_MIN + 1)
        self.assertIsNone(downloader.fresh_info(self.cache, self.url))

    def test_a_missing_one_is_not_an_error(self):
        self.assertIsNone(downloader.fresh_info(self.cache, self.url))

    def test_the_window_is_well_short_of_the_signature_lifetime(self):
        # YouTube's own expire= is about six hours. Sitting near it would turn
        # the shortcut into a coin flip.
        self.assertLess(config.INFO_CACHE_MIN, 120)

    def test_the_replay_command_skips_everything_to_do_with_youtube(self):
        cmd = downloader._info_command(
            "yt-dlp", self.cache / "x.info.json", "best", self.cache,
            Path("print.txt"))
        joined = " ".join(cmd)
        self.assertIn("--load-info-json", joined)
        for gone in ("--cookies", "player_client", "--remote-components",
                     "youtubepot"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, joined)

    def test_the_replay_still_writes_where_the_downloader_looks(self):
        cmd = downloader._info_command(
            "yt-dlp", self.cache / "x.info.json", "best", self.cache,
            Path("print.txt"))
        self.assertIn("--print-to-file", cmd)
        self.assertIn(str(self.cache / downloader.OUT_TEMPLATE), cmd)

    def test_the_replay_keeps_no_part_so_partial_playback_still_works(self):
        cmd = downloader._info_command(
            "yt-dlp", self.cache / "x.info.json", "best", self.cache,
            Path("print.txt"))
        self.assertIn("--no-part", cmd)

    def test_the_normal_path_writes_one_for_next_time(self):
        cmd = downloader._command("yt-dlp", self.url, "best", self.cache,
                                  Path("print.txt"), [])
        self.assertIn("--write-info-json", cmd)
        self.assertIn(f"infojson:{downloader.info_stem(self.cache, self.url)}",
                      cmd)

    def test_the_template_and_the_lookup_name_the_same_file(self):
        # They did not. yt-dlp appends ".info.json" to the template, so passing
        # the full filename wrote ".info-<key>.info.info.json" while
        # fresh_info() looked for ".info-<key>.info.json" - the cache was
        # written diligently on every load and read never.
        stem = downloader.info_stem(self.cache, self.url)
        self.assertEqual(str(downloader.info_path(self.cache, self.url)),
                         f"{stem}.info.json")

    def test_the_written_file_is_the_one_found(self):
        # End to end over the naming, with a file on disk rather than a string
        # comparison that could agree with itself while both are wrong.
        stem = downloader.info_stem(self.cache, self.url)
        Path(f"{stem}.info.json").write_text("{}", encoding="utf-8")
        self.assertIsNotNone(downloader.fresh_info(self.cache, self.url))

    def test_the_video_template_is_still_the_unprefixed_one(self):
        # Two -o now. yt-dlp reads the prefixed one as "where the info json
        # goes"; anything that took the last -o would put the clip there.
        cmd = downloader._command("yt-dlp", self.url, "best", self.cache,
                                  Path("print.txt"), [])
        outs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-o"]
        self.assertIn(str(self.cache / downloader.OUT_TEMPLATE), outs)

    def test_a_failed_replay_falls_through_to_a_real_fetch(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        body = source.split("warm = fresh_info(cache, url)")[1].split(
            'progress("fetching video + audio')[0]
        self.assertIn("except DownloadError as exc:", body)
        self.assertIn("warm.unlink(missing_ok=True)", body)

    def test_a_cancelled_replay_is_not_treated_as_a_failed_one(self):
        # Cancelling is the user changing their mind, not the shortcut being
        # wrong - re-extracting after it would be the opposite of what was asked.
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        body = source.split("warm = fresh_info(cache, url)")[1].split(
            'progress("fetching video + audio')[0]
        self.assertIn("except DownloadCancelled:", body)
        self.assertIn("raise", body)

    def test_the_replay_is_a_single_attempt_with_no_client_chain(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        self.assertIn('Attempt("cached description", [], allow_hls=True)', source)

    def test_expired_descriptions_are_swept_up(self):
        # Each one holds a PO token and the IP the request came from.
        self._write(age_minutes=downloader.INFO_CACHE_MIN + 5)
        downloader.prune(self.cache, max_gb=0)
        self.assertEqual(list(self.cache.glob(".info-*.info.json")), [])

    def test_a_live_description_survives_the_sweep(self):
        path = self._write()
        downloader.prune(self.cache, max_gb=0)
        self.assertTrue(path.exists())


class FirstFrameTimingTests(unittest.TestCase):
    """"Ready" and "what the room saw" stopped being the same number.

    Since partial playback the file can be on screen most of a minute before
    the last byte lands, and the log reported only the download. Twice in
    testing that made it impossible to tell from a log whether a load was slow
    or the reporting was.
    """

    def test_the_ready_line_carries_both_when_they_differ(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        body = source.split("def _finish(")[1].split("\ndef ")[0]
        self.assertIn("first frame at", body)
        self.assertIn("ready in", body)

    def test_a_load_with_no_hand_over_says_nothing_extra(self):
        # Most loads never hand over - PARTIAL_AFTER_S sees to that - and an
        # empty "(first frame at ...)" on every line would be noise.
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        body = source.split("def _finish(")[1].split("\ndef ")[0]
        self.assertIn('"" if first_frame is None', body)

    def test_the_hand_over_time_is_taken_from_the_load_clock(self):
        # Not from the player: this is measured against the same `started` the
        # rest of the line uses, or the two numbers would not be comparable.
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        body = source.split("def download(")[1].split("\ndef ")[0]
        self.assertIn("seen_at.append(time.monotonic())", body)
        self.assertIn("first_frame - started", 
                      source.split("def _finish(")[1].split("\ndef ")[0])

    def test_the_caller_still_gets_its_callback(self):
        # The wrapper must not swallow it - partial playback depends on it.
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        body = source.split("def download(")[1].split("\ndef ")[0]
        self.assertIn("_inner(path)", body)

    def test_a_load_without_partial_playback_is_unaffected(self):
        # on_partial is None in stream mode and when PARTIAL_PLAY is off; the
        # wrapper must not be installed at all then.
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        body = source.split("def download(")[1].split("\ndef ")[0]
        self.assertIn("if on_partial is not None:", body)


class ProbeFlagTests(unittest.TestCase):
    """The probe has to ask a question the generator understands.

    2.0.0's main.ts is the server CLI - it has -p and -H and no --version, so
    probing with --version answered "error: unknown option '--version'" and the
    diagnostics printed a red FAIL and "will not work correctly yet" over a
    setup that was working perfectly.
    """

    def test_the_2_x_generator_is_asked_for_help_not_a_version(self):
        source = Path(potprovider.__file__).read_text(encoding="utf-8")
        self.assertIn('probe_flag = "--help" if script.name == "main.ts" '
                      'else "--version"', source)

    def test_the_probe_still_runs_the_thing_rather_than_looking_at_it(self):
        # A release once shipped a node_modules that existed, weighed half a
        # gigabyte and resolved nothing, because zipping dereferenced deno's
        # symlinks. Only running it catches that.
        source = Path(potprovider.__file__).read_text(encoding="utf-8")
        body = source.split("def script_probe(")[1].split("\ndef ")[0]
        self.assertIn("subprocess.run", body)

    def test_the_server_kind_is_readable_without_reaching_inside(self):
        # The diagnostics have to report what the app will actually use.
        p = potprovider.PotProvider()
        self.assertIsNone(p.server_kind)
        p._server_kind = "deno"
        self.assertEqual(p.server_kind, "deno")


class PrefetchLoggingTests(unittest.TestCase):
    """A prefetch is nobody waiting, and must not read like a slow load.

    A pasted URL fetched for 16s and the Load that followed was instant - but
    the log said "ready in 16.4s", which is the number you would quote if you
    thought the tool was slow.
    """

    def test_a_prefetch_says_so(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        body = source.split("def _finish(")[1].split("\ndef ")[0]
        self.assertIn('"prefetched in" if ahead else "ready in"', body)

    def test_the_player_marks_its_background_fetches(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def prefetch(self")[1].split("def cancel_prefetch")[0]
        self.assertIn("ahead=True", body)

    def test_a_real_load_is_not_marked(self):
        # The load path must keep saying "ready" - that is the number a VJ
        # actually waited through.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def load(self, url")[1].split("def _hybrid")[0]
        self.assertNotIn("ahead=True", body)


class DoctorReflectsWhatIsUsedTests(unittest.TestCase):
    """The diagnostics must describe the app that exists, not the old one."""

    def test_it_asks_the_provider_rather_than_assuming_script_mode(self):
        source = Path(doctor.__file__).read_text(encoding="utf-8")
        self.assertIn("provider.server_kind", source)
        self.assertNotIn("script mode is in use", source)

    def test_a_broken_spare_is_not_fatal_when_a_server_is_up(self):
        source = Path(doctor.__file__).read_text(encoding="utf-8")
        self.assertIn("not usable as a spare", source)

    def test_it_puts_the_provider_back_as_it_found_it(self):
        # Diagnostics that leave a server running would change the thing they
        # were asked to inspect.
        source = Path(doctor.__file__).read_text(encoding="utf-8")
        self.assertIn("if started_here:", source)
        self.assertIn("provider.stop()", source)


class VersionMatchedServerTests(unittest.TestCase):
    """The warm server, from the same release as the plugin that talks to it.

    bgutil-pot.exe is a separate Rust port on its own numbering - it decoupled
    from upstream at 0.8.x - so against plugin 2.x the compatibility check
    refuses it, and the whole warm-server path silently reverted to spawning
    deno per token. 2.0.0 merged the generator and the server into one main.ts,
    so the server can now be that same file: version-matched by construction.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="chuutube-srv-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = self.dir / "server"
        (self.home / "src").mkdir(parents=True)
        self._which = potprovider.shutil.which
        potprovider.shutil.which = lambda name: "/usr/bin/deno" if name == "deno" else None
        self.addCleanup(setattr, potprovider.shutil, "which", self._which)

    def _generator(self, name):
        (self.home / "src" / name).write_text("x", encoding="utf-8")

    def test_a_2_x_tree_can_serve(self):
        self._generator("main.ts")
        cmd = potprovider.script_server_command(str(self.home))
        self.assertIsNotNone(cmd)
        self.assertTrue(cmd[-1].endswith("main.ts"))

    def test_a_1_x_tree_cannot(self):
        # generate_once.ts answers one request and exits - running it as a
        # server would hang the startup wait and then look like a failure.
        self._generator("generate_once.ts")
        self.assertIsNone(potprovider.script_server_command(str(self.home)))

    def test_no_deno_means_no_server(self):
        self._generator("main.ts")
        potprovider.shutil.which = lambda name: None
        self.assertIsNone(potprovider.script_server_command(str(self.home)))

    def test_it_runs_with_the_same_permissions_as_the_plugin_would(self):
        # Scoped, not --allow-all: this is third-party TypeScript either way.
        self._generator("main.ts")
        cmd = potprovider.script_server_command(str(self.home))
        self.assertIn("--allow-net", cmd)
        self.assertNotIn("--allow-all", cmd)
        self.assertTrue(any(a.startswith("--allow-ffi=") for a in cmd))

    def test_the_deno_server_needs_no_version_check(self):
        # There is no pair of versions to compare - it is the same release.
        p = potprovider.PotProvider()
        p._server_kind = "deno"
        self.assertTrue(p.compatible())

    def test_the_exe_is_still_version_checked(self):
        source = Path(potprovider.__file__).read_text(encoding="utf-8")
        body = source.split("def _start_server_locked")[1].split("def stop")[0]
        self.assertIn("versions_agree(plugin_version(),", body)

    def test_the_generator_server_is_preferred_over_the_exe(self):
        source = Path(potprovider.__file__).read_text(encoding="utf-8")
        body = source.split("def _start_server_locked")[1].split("def stop")[0]
        self.assertLess(body.index("script_server_command"),
                        body.index("find_executable()"))

    def test_a_mismatched_exe_now_says_script_mode_not_360p(self):
        # It used to say "Only 360p will be available", which was wrong: script
        # mode still issues tokens perfectly well.
        source = Path(potprovider.__file__).read_text(encoding="utf-8")
        body = source.split("def _start_server_locked")[1].split("def stop")[0]
        self.assertIn("Script mode will be used instead.", body)

    def test_stopping_forgets_which_kind_was_running(self):
        # Otherwise compatible() would keep answering True for a deno server
        # that is no longer there.
        p = potprovider.PotProvider()
        p._server_kind = "deno"
        p.stop()
        self.assertIsNone(p._server_kind)


class GeneratorEntryPointTests(unittest.TestCase):
    """bgutil 2.0.0 renamed the script-mode entry point.

    Up to 1.3.1 the one-shot generator was `server/src/generate_once.ts`; 2.0.0
    merged it into the server's own `server/src/main.ts`. Hardcoding either
    name means a tree that is perfectly good gets declared missing - and the
    only downstream symptom of no token provider is a 360p download, which is
    indistinguishable from a dozen other things.

    Both are accepted deliberately: an installed tree is whatever setup.ps1
    last fetched, and testers are running builds from several weeks apart.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="chuutube-gen-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.src = self.dir / "server" / "src"
        self.src.mkdir(parents=True)

    def test_the_2_x_entry_point_is_found(self):
        (self.src / "main.ts").write_text("x", encoding="utf-8")
        found = potprovider.generator_script(self.dir / "server")
        self.assertEqual(found.name, "main.ts")

    def test_the_1_x_entry_point_still_is(self):
        (self.src / "generate_once.ts").write_text("x", encoding="utf-8")
        found = potprovider.generator_script(self.dir / "server")
        self.assertEqual(found.name, "generate_once.ts")

    def test_the_newer_name_wins_when_both_exist(self):
        # A tree upgraded in place could carry both. 2.0.0's main.ts is the one
        # its plugin will call.
        (self.src / "main.ts").write_text("x", encoding="utf-8")
        (self.src / "generate_once.ts").write_text("x", encoding="utf-8")
        self.assertEqual(
            potprovider.generator_script(self.dir / "server").name, "main.ts")

    def test_an_empty_tree_is_not_a_generator(self):
        self.assertIsNone(potprovider.generator_script(self.dir / "server"))

    def test_a_missing_directory_is_not_an_error(self):
        self.assertIsNone(potprovider.generator_script(self.dir / "nope"))

    def test_the_probe_names_both_spellings_when_it_finds_neither(self):
        # "generate_once.ts is missing" sent someone looking for a file that
        # their release was never supposed to have.
        # The real one: the suite replaces script_probe with a stub so no test
        # ever shells out to deno.
        ok, detail = _REAL_SCRIPT_PROBE(str(self.dir / "server"))
        self.assertFalse(ok)
        if "deno" not in detail:
            self.assertIn("main.ts", detail)
            self.assertIn("generate_once.ts", detail)

    def test_nothing_still_hardcodes_the_old_name(self):
        source = Path(potprovider.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                          if not line.lstrip().startswith("#"))
        self.assertNotIn('"src" / "generate_once.ts"', code)


class TokenServerPreferenceTests(unittest.TestCase):
    """The same token, without paying for a deno process to mint it.

    Script mode runs the generator per request; the timing line calls that
    about 1.3s of every cold load. The server has already started, so the same
    token is an HTTP round trip on loopback.
    """

    def test_the_server_is_tried_first(self):
        source = Path(potprovider.__file__).read_text(encoding="utf-8")
        body = source.split("def start(self)")[1].split("def _prepare_script")[0]
        self.assertIn("if TOKEN_SERVER and self._start_server():", body)
        self.assertLess(body.index("_start_server()"),
                        body.index("find_script_home()"))

    def test_script_mode_is_still_the_fallback(self):
        # It needs no separate executable, and a version-mismatched server is
        # refused - so this must degrade to script mode, not to no token.
        source = Path(potprovider.__file__).read_text(encoding="utf-8")
        body = source.split("def start(self)")[1].split("def _prepare_script")[0]
        self.assertIn("find_script_home()", body)
        self.assertIn("_prepare_script", body)

    def test_the_flags_follow_whichever_is_actually_up(self):
        source = Path(potprovider.__file__).read_text(encoding="utf-8")
        body = source.split("def extractor_args(self)")[1].split("def start(self)")[0]
        self.assertIn("if self.running and self.compatible():", body)
        self.assertLess(body.index("bgutilhttp"), body.index("bgutilscript"))

    def test_a_mismatched_server_is_not_started(self):
        # Pointing the plugin at a server it refuses gives a warning per
        # request and no token - worse than pointing it at nothing.
        source = Path(potprovider.__file__).read_text(encoding="utf-8")
        self.assertIn("versions_agree(plugin_version(),", source)

    def test_it_can_be_switched_off(self):
        self.assertTrue(config.TOKEN_SERVER)
        source = Path(potprovider.__file__).read_text(encoding="utf-8")
        self.assertIn("TOKEN_SERVER", source)


class PrefetchJobTests(unittest.TestCase):
    """The background fetch, and the one collision it has to avoid.

    --no-part means yt-dlp writes straight into the destination. Two fetches of
    the same URL are therefore two processes writing one file, which is the very
    collision the partial-playback work exists to prevent - so load() must join
    a running prefetch rather than start its own.
    """

    def _player(self, quality=1080):
        p = player_mod.Player.__new__(player_mod.Player)
        # What Player.__init__ sets for the status line; the stub base
        # class would answer anything missing with a truthy placeholder.
        p._headline, p._fetch_note, p._size_note = "idle", "", ""
        p.name = "A"
        p._load_token, p._destroyed = 0, False
        p._quality = quality
        p._job, p._job_lock = None, threading.Lock()
        p.states = []
        p._prefetch_state = type("S", (), {
            "emit": lambda _s, v: p.states.append(v)})()
        return p

    def test_a_job_carries_its_own_result(self):
        job = player_mod.PrefetchJob("https://example/x", 1080)
        job.clip = downloader.LocalClip(video_path="/cache/x.mp4")
        job.done.set()
        self.assertIs(job.result(1.0).video_path, job.clip.video_path)

    def test_a_job_re_raises_what_the_fetch_raised(self):
        job = player_mod.PrefetchJob("https://example/x", 1080)
        job.error = downloader.DownloadError("nope")
        job.done.set()
        with self.assertRaises(downloader.DownloadError):
            job.result(1.0)

    def test_waiting_on_a_job_that_never_lands_gives_up(self):
        # Better a load that reports a timeout than a GUI thread parked forever.
        job = player_mod.PrefetchJob("https://example/x", 1080)
        with self.assertRaises(downloader.DownloadError):
            job.result(0.01)

    def test_load_claims_a_prefetch_of_the_same_url(self):
        p = self._player()
        p._job = player_mod.PrefetchJob("https://example/x", 1080)
        self.assertIsNotNone(p._claim_prefetch("https://example/x", 1080))

    def test_a_different_quality_is_a_different_fetch(self):
        # The output path is keyed on format, so 1080 and 480 are two files.
        p = self._player()
        p._job = player_mod.PrefetchJob("https://example/x", 1080)
        self.assertIsNone(p._claim_prefetch("https://example/x", 480))

    def test_a_cancelled_job_is_not_claimed(self):
        p = self._player()
        p._job = player_mod.PrefetchJob("https://example/x", 1080)
        p._job.cancel.set()
        self.assertIsNone(p._claim_prefetch("https://example/x", 1080))

    def test_clearing_calls_the_fetch_off(self):
        p = self._player()
        p._job = player_mod.PrefetchJob("https://example/x", 1080)
        p.cancel_prefetch()
        self.assertTrue(p._job is None)
        self.assertEqual(p.states[-1], "")

    def test_stop_calls_it_off_too(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def stop(self)")[1].split("def ")[0]
        self.assertIn("self.cancel_prefetch()", body)

    def test_load_waits_instead_of_starting_a_second_yt_dlp(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def load(self, url")[1].split("def _hybrid")[0]
        self.assertIn("warm = self._claim_prefetch(url, height)", body)
        self.assertIn("clip = warm.result()", body)

    def test_a_cancelled_fetch_is_not_reported_as_a_failure(self):
        # It was cancelled because a newer URL replaced it. Putting "cancelled"
        # on the status line would read as an error the user did not cause.
        self.assertFalse(issubclass(downloader.DownloadCancelled,
                                    downloader.DownloadError))
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("except DownloadCancelled:", source)


class PrefetchCancellationTests(unittest.TestCase):
    """A fetch nobody wants any more must stop competing for the link."""

    def test_the_download_loop_checks_the_cancel_flag(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        self.assertIn("if cancel is not None and cancel.is_set():", source)
        self.assertIn("proc.terminate()", source)

    def test_it_is_checked_per_line_not_per_attempt(self):
        # A 70 MB prefetch is most of a minute; waiting for it to finish before
        # noticing is the same as not cancelling at all.
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        loop = source.split("for line in proc.stdout:")[1].split("proc.wait()")[0]
        self.assertIn("cancel.is_set()", loop)

    def test_cancel_reaches_the_subprocess_from_download(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        # Split on a top-level def: download() has nested ones now.
        head = source.split("def download(")[1].split("\ndef ")[0]
        self.assertIn("cancel=cancel", head)

    def test_a_new_url_supersedes_the_old_fetch(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def prefetch(self")[1].split("def cancel_prefetch")[0]
        self.assertIn("job.cancel.set()", body)

    def test_prefetch_is_only_for_the_download_path(self):
        # Nothing to warm in stream mode, and hybrid's resolve is the fast half.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def prefetch(self")[1].split("def cancel_prefetch")[0]
        self.assertIn('MODE != "download"', body)
        self.assertIn("if not PREFETCH", body)

    def test_an_unsafe_url_is_declined_quietly(self):
        # load() will explain it. Two complaints for one paste is noise.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def prefetch(self")[1].split("def cancel_prefetch")[0]
        self.assertIn("except UnsafeUrl:", body)

    def test_it_is_on_by_default(self):
        self.assertTrue(config.PREFETCH, "off by default, nobody would find it")


class UrlRowButtonsTests(unittest.TestCase):
    """Paste is the button that gets pressed every time a clip changes.

    Load and Clear are occasional; Paste is the whole gesture. It is the
    prominent one, and it does the fetch as well as the paste - a VJ pasting
    the next clip has already decided to play it.
    """

    def setUp(self):
        self.w = MainWindow()
        self.panel = self.w.panels[0]
        qtwidgets = sys.modules["PySide6.QtWidgets"]
        self.clipboard = qtwidgets.QApplication.clipboard()
        self.clipboard.setText("")

    def test_each_button_carries_its_own_role(self):
        self.assertEqual(self.panel.paste_btn.property("role"), "paste")
        self.assertEqual(self.panel.load_btn.property("role"), "load")
        self.assertEqual(self.panel.clear_btn.property("role"), "clear")

    def test_the_stylesheet_actually_distinguishes_them(self):
        # A property nothing styles is a property that does nothing.
        for role in ("paste", "load", "clear"):
            with self.subTest(role=role):
                self.assertIn(f'QPushButton[role="{role}"]', theme.STYLESHEET)

    def test_the_three_colours_are_all_different(self):
        blues = (theme.BLUE, theme.BLUE_DIM)
        greens = (theme.ACCENT, theme.ACCENT_DIM)
        reds = (theme.RED, theme.RED_DIM)
        self.assertEqual(len({*blues, *greens, *reds}), 6)

    def test_only_paste_is_filled_in(self):
        # Three filled buttons side by side is a traffic light, not a
        # hierarchy - Paste is the one pressed every time a clip changes.
        rules = theme.STYLESHEET
        paste = rules.split('QPushButton[role="paste"] {')[1].split("}")[0]
        load = rules.split('QPushButton[role="load"] {')[1].split("}")[0]
        clear = rules.split('QPushButton[role="clear"] {')[1].split("}")[0]
        self.assertIn("background:", paste)
        self.assertNotIn("background:", load)
        self.assertNotIn("background:", clear)

    def test_disabled_beats_the_colours(self):
        # Load greys out while a fetch runs. A green "Loading" that cannot be
        # pressed reads as available.
        rules = theme.STYLESHEET
        self.assertLess(rules.index('QPushButton[role="load"]'),
                        rules.index("QPushButton:disabled {"),
                        "the disabled rule has to come last to win in Qt")

    def test_paste_overwrites_whatever_was_there(self):
        self.panel.url_edit.setText("https://example/old")
        self.clipboard.setText("https://example/new")
        self.panel.paste()
        self.assertEqual(self.panel.url_edit.text(), "https://example/new")

    def test_paste_trims_the_clipboard(self):
        # Copying from a browser's address bar routinely brings whitespace.
        self.clipboard.setText("  https://example/x \n")
        self.panel.paste()
        self.assertEqual(self.panel.url_edit.text(), "https://example/x")

    def test_an_empty_clipboard_leaves_the_box_alone(self):
        self.panel.url_edit.setText("https://example/keep")
        self.clipboard.setText("   ")
        self.panel.paste()
        self.assertEqual(self.panel.url_edit.text(), "https://example/keep")
        self.assertIn("clipboard", self.panel.status_label.text())

    def test_clear_empties_the_box(self):
        self.panel.url_edit.setText("https://example/x")
        self.panel.clear()
        self.assertEqual(self.panel.url_edit.text(), "")

    def test_clear_also_stops_the_slot(self):
        # An empty box over a still-playing slot would be a lie, and this is
        # the only control that blanks a slot.
        seen = []
        self.panel.player.stop = lambda: seen.append(True)
        self.panel.clear()
        self.assertEqual(seen, [True])

    def test_load_plays_what_is_in_the_box(self):
        seen = []
        self.panel.player.load = lambda url, **k: seen.append(url)
        self.panel.url_edit.setText("https://example/play")
        self.panel.load()
        self.assertEqual(seen, ["https://example/play"])


class PrefetchOnPasteTests(unittest.TestCase):
    """The five seconds before a byte moves do not need Load to have happened.

    Measured on a clean load: startup 2.0s, extract 1.6s, token 1.3s, challenge
    1.0s - about five seconds of page extraction and cryptography before the
    transfer even starts, none of which depends on anyone having decided to play
    the clip. Spending it while the previous clip is still on screen is free.
    """

    def setUp(self):
        self.w = MainWindow()
        self.panel = self.w.panels[0]
        self.asked = []
        self.panel.player.prefetch = lambda url: self.asked.append(url)
        qtwidgets = sys.modules["PySide6.QtWidgets"]
        self.clipboard = qtwidgets.QApplication.clipboard()

    def test_pasting_starts_the_fetch_immediately(self):
        self.clipboard.setText("https://example/one")
        self.panel.paste()
        self.assertEqual(self.asked, ["https://example/one"])

    def test_paste_does_not_wait_for_the_debounce(self):
        # The clipboard hands over a complete URL; there is nothing to settle.
        self.clipboard.setText("https://example/two")
        self.panel.paste()
        self.assertFalse(getattr(self.panel._prefetch_timer, "running", False))

    def test_typing_is_debounced_rather_than_fetched_per_keystroke(self):
        for text in ("h", "ht", "htt", "http"):
            self.panel._on_url_edited(text)
        self.assertEqual(self.asked, [], "a fetch per character would be absurd")
        self.assertTrue(self.panel._prefetch_timer.running)

    def test_the_settled_url_is_the_one_fetched(self):
        self.panel._on_url_edited("https://example/part")
        self.panel.url_edit.setText("https://example/whole")
        self.panel._prefetch_now()
        self.assertEqual(self.asked, ["https://example/whole"])

    def test_an_empty_box_is_not_fetched(self):
        self.panel.url_edit.setText("   ")
        self.panel._prefetch_now()
        self.assertEqual(self.asked, [])

    def test_clearing_calls_off_a_pending_fetch(self):
        self.panel._on_url_edited("https://example/x")
        self.panel.clear()
        self.assertFalse(self.panel._prefetch_timer.running)


class PrefetchStateDisplayTests(unittest.TestCase):
    """Say that Load will be instant, without shouting about it."""

    def setUp(self):
        self.w = MainWindow()
        self.panel = self.w.panels[0]

    def test_ready_marks_the_load_button(self):
        self.panel._set_prefetch_state("ready")
        self.assertTrue(self.panel.load_btn.property("ready"))
        self.assertIn("immediately", self.panel.load_btn.toolTip())

    def test_ready_fills_the_button_it_belongs_to(self):
        # Rather than a separate indicator: the button that will be instant is
        # the one that should look it.
        self.assertIn('QPushButton[role="load"][ready="true"]', theme.STYLESHEET)

    def test_the_mark_comes_off_again(self):
        self.panel._set_prefetch_state("ready")
        self.panel._set_prefetch_state("")
        self.assertFalse(self.panel.load_btn.property("ready"))

    def test_an_idle_slot_says_so_in_the_status_line(self):
        self.panel.player.stream = None
        self.panel._set_prefetch_state("preparing")
        self.assertEqual(self.panel.status_label.text(), "fetching ahead...")

    def test_a_playing_slot_keeps_its_own_status(self):
        # The status line belongs to the clip on screen. Overwriting it with
        # news about the next one is how a VJ loses track of what is live.
        self.panel.player.stream = object()
        self.panel.status_label.setText("1080p (cached)")
        self.panel._set_prefetch_state("ready")
        self.assertEqual(self.panel.status_label.text(), "1080p (cached)")

    def test_a_loading_slot_keeps_its_own_status_too(self):
        self.panel.player.stream = None
        self.panel._set_loading(True)
        self.panel.status_label.setText("fetching (default)...")
        self.panel._set_prefetch_state("ready")
        self.assertEqual(self.panel.status_label.text(), "fetching (default)...")


class SlotSelectionTests(unittest.TestCase):
    """Which slot Space, M, the arrows and Home act on."""

    def setUp(self):
        self.w = MainWindow()

    def test_a_is_selected_to_begin_with(self):
        self.assertEqual(self.w.selected_index, 0)
        self.assertTrue(self.w.panels[0].property("selected"))
        self.assertFalse(self.w.panels[1].property("selected"))

    def test_selecting_moves_the_highlight(self):
        self.w.select_slot(1)
        self.assertFalse(self.w.panels[0].property("selected"))
        self.assertTrue(self.w.panels[1].property("selected"))
        self.assertTrue(self.w.panels[1].slot_label.property("selected"))

    def test_using_a_slot_selects_it(self):
        for action in ("toggle_pause", "jump_to_start", "toggle_audio",
                       "paste", "load"):
            with self.subTest(action):
                self.w.select_slot(0)
                getattr(self.w.panels[1], action)()
                self.assertEqual(self.w.selected_index, 1)

    def test_focusing_the_url_field_selects_its_slot(self):
        self.w.select_slot(0)
        self.w.panels[1].url_edit.focused.emit()
        self.assertEqual(self.w.selected_index, 1)

    def test_the_keys_follow_the_selection(self):
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _install_shortcuts")[1].split("\n    def ")[0]
        self.assertIn('add("1", lambda: self.select_slot(0))', body)
        self.assertIn('add("2", lambda: self.select_slot(1))', body)
        for key in ("Space", "M", "Left", "Right", "Home"):
            line = next(l for l in body.splitlines() if f'add("{key}"' in l)
            self.assertIn("self.selected_index", line)

    def test_the_selection_is_remembered(self):
        self.w.select_slot(1)
        self.w._save_settings()
        self.assertEqual(int(self.w.settings.value("selected", 0)), 1)

    def test_a_nonsense_saved_value_falls_back_to_a(self):
        self.w.settings.setValue("selected", "banana")
        self.w._restore_settings()
        self.assertEqual(self.w.selected_index, 0)


class HybridLoadTests(unittest.TestCase):
    """Show a frame now, cache underneath, swap to the file.

    The tool this replaced ran `yt-dlp -g -f "best[height<=720]/best"` and
    handed the printed URL straight to a player - no download at all, so
    playback started in a second or two. We downloaded the whole clip first and
    showed nothing until it landed. Hybrid takes the start time from one and
    keeps the local copy from the other.
    """

    def setUp(self):
        self.p = player_mod.Player.__new__(player_mod.Player)
        # What Player.__init__ sets for the status line; the stub base
        # class would answer anything missing with a truthy placeholder.
        self.p._headline, self.p._fetch_note, self.p._size_note = "idle", "", ""
        self.p._load_token = 1
        self.p._destroyed = False
        self.events = []
        for name in ("_resolved", "_failed", "_progress", "_cached"):
            self.events.append(name)
        self.emitted = []

        class Emitter:
            def __init__(self, log, name):
                self.log, self.name = log, name

            def emit(self, *args):
                self.log.append((self.name, args))

        for name in ("_resolved", "_failed", "_progress", "_cached"):
            setattr(self.p, name, Emitter(self.emitted, name))
        self.saved = (player_mod.resolve, player_mod.download)

    def tearDown(self):
        player_mod.resolve, player_mod.download = self.saved

    def _run(self, resolve_result, download_result):
        def fake_resolve(url, height, progress=None):
            if isinstance(resolve_result, Exception):
                raise resolve_result
            return resolve_result

        def fake_download(url, height, cache, progress=None):
            if isinstance(download_result, Exception):
                raise download_result
            return download_result

        player_mod.resolve = fake_resolve
        player_mod.download = fake_download
        self.p._hybrid(1, "https://example/watch", 1080, lambda msg: None)
        return [name for name, _ in self.emitted]

    def test_the_stream_is_shown_before_the_download_is_asked_for(self):
        order = self._run(Stream(video_url="https://remote/v.mp4"),
                          LocalClip(video_path="C:/cache/v.mp4", title="t"))
        self.assertEqual(order, ["_resolved", "_cached"])

    def test_the_cached_swap_carries_the_local_file(self):
        self._run(Stream(video_url="https://remote/v.mp4"),
                  LocalClip(video_path="C:/cache/v.mp4", title="t"))
        name, args = self.emitted[-1]
        self.assertEqual(name, "_cached")
        self.assertEqual(args[1].video_url, "C:/cache/v.mp4")
        self.assertTrue(args[1].direct)

    def test_a_stream_that_will_not_resolve_still_gets_downloaded(self):
        order = self._run(ResolveError("no formats"),
                          LocalClip(video_path="C:/cache/v.mp4", title="t"))
        # One _resolved carrying the local file, and no _cached - there was
        # nothing playing to swap out. (The "cannot stream" note goes through
        # the `say` callback, which this harness discards.)
        self.assertEqual(order, ["_resolved"])

    def test_a_download_that_fails_does_not_kill_what_is_playing(self):
        order = self._run(Stream(video_url="https://remote/v.mp4"),
                          DownloadError("disk full"))
        self.assertIn("_resolved", order)
        self.assertNotIn("_failed", order)
        self.assertEqual(order[-1], "_progress")

    def test_both_halves_failing_is_a_load_failure(self):
        order = self._run(ResolveError("no formats"), DownloadError("disk full"))
        self.assertEqual(order[-1], "_failed")

    def test_a_superseded_load_is_not_swapped_underneath(self):
        # _on_cached must respect the token, or pressing Load again mid-cache
        # would yank the new clip out and put the old one back.
        self.p._load_token = 9
        swapped = []
        self.p.stream = None
        self.p._prepare_urls = lambda s: swapped.append(s) or (s.video_url, None)
        self.p._on_cached(1, Stream(video_url="C:/cache/old.mp4"))
        self.assertEqual(swapped, [])


class NoAudioByDefaultTests(unittest.TestCase):
    """Audio is off unless asked for: this feeds Resolume, not a PA."""

    def test_the_default_is_off(self):
        self.assertFalse(config.FETCH_AUDIO)

    def test_the_download_asks_for_video_only(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        self.assertIn("if want_audio and SINGLE_PASS_AUDIO else video_fmt",
                      source)
        self.assertIn("if want_audio and not clip.has_audio", source)

    def test_a_resolved_stream_drops_its_audio_url(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("if not FETCH_AUDIO:", source)
        self.assertIn("replace(stream, audio_url=None)", source)

    def test_the_mute_button_goes_with_it(self):
        w = MainWindow()
        for panel in w.panels:
            self.assertEqual(panel.mute_btn.isVisible(), config.FETCH_AUDIO)

    def test_a_silent_cache_entry_is_not_reused_once_audio_is_wanted(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        self.assertIn("cached is not None and FETCH_AUDIO", source)


class DoctorImportScopeTests(unittest.TestCase):
    """A name imported at module level must not also be imported inside run().

    Python makes a name local to the whole function if it is bound anywhere in
    it, so a late `from .potprovider import versions_agree` turned an earlier
    call to the module-level one into UnboundLocalError - and it surfaced as a
    traceback out of `doctor.bat`, which is the one command someone runs when
    something is already wrong.
    """

    def _tree(self, module):
        return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

    def _module_level_imports(self, tree):
        names = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update(a.asname or a.name for a in node.names)
        return names

    def _function_imports(self, tree):
        found = {}
        for func in [n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef)]:
            for node in ast.walk(func):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        found.setdefault(alias.asname or alias.name,
                                         set()).add(func.name)
        return found

    def test_doctor_never_shadows_its_own_module_imports(self):
        tree = self._tree(doctor)
        clash = self._module_level_imports(tree) & set(
            self._function_imports(tree))
        self.assertEqual(clash, set(),
                         f"imported at module level and again inside a "
                         f"function: {sorted(clash)}")

    def test_the_whole_package_is_checked_the_same_way(self):
        package = Path(doctor.__file__).parent
        offenders = {}
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            clash = self._module_level_imports(tree) & set(
                self._function_imports(tree))
            if clash:
                offenders[path.name] = sorted(clash)
        self.assertEqual(offenders, {})


class ResolverCookieTests(unittest.TestCase):
    """resolve() must speak the same cookie language as download().

    It passed the raw config value to yt-dlp's `cookiesfrombrowser`, which only
    knows yt-dlp's own browser names. "waterfox" produced `unsupported browser`
    on every client in the chain, so the streaming half of hybrid mode failed
    on every load and quietly fell through to a plain download - the exact
    behaviour hybrid exists to avoid, with no error anyone would notice.
    """

    def setUp(self):
        self.saved = (resolver.COOKIES_FROM_BROWSER, resolver.COOKIE_EXPORT)

    def tearDown(self):
        resolver.COOKIES_FROM_BROWSER, resolver.COOKIE_EXPORT = self.saved

    def test_no_cookies_configured_means_no_cookie_options(self):
        resolver.COOKIES_FROM_BROWSER = ""
        self.assertEqual(resolver.cookie_opts(), {})

    def test_the_exported_jar_is_preferred(self):
        resolver.COOKIES_FROM_BROWSER = "waterfox"
        resolver.COOKIE_EXPORT = True
        jar = Path("C:/tmp/c.txt")
        saved = resolver.cookie_store.ensure
        resolver.cookie_store.ensure = lambda *a, **k: (jar, None)
        try:
            self.assertEqual(resolver.cookie_opts(), {"cookiefile": str(jar)})
        finally:
            resolver.cookie_store.ensure = saved

    def test_a_browser_name_is_never_passed_through_raw(self):
        resolver.COOKIES_FROM_BROWSER = "waterfox"
        resolver.COOKIE_EXPORT = False
        saved = resolver.resolve_cookies
        resolver.resolve_cookies = lambda value: ("firefox:C:/profiles/x", None)
        try:
            opts = resolver.cookie_opts()
        finally:
            resolver.resolve_cookies = saved
        self.assertEqual(opts["cookiesfrombrowser"],
                         ("firefox", "C:/profiles/x", None, None))
        self.assertNotIn("waterfox", str(opts))

    def test_a_browser_with_no_profile_still_works(self):
        resolver.COOKIES_FROM_BROWSER = "chrome"
        resolver.COOKIE_EXPORT = False
        saved = resolver.resolve_cookies
        resolver.resolve_cookies = lambda value: ("chrome", None)
        try:
            opts = resolver.cookie_opts()
        finally:
            resolver.resolve_cookies = saved
        self.assertEqual(opts["cookiesfrombrowser"], ("chrome", None, None, None))

    def test_extract_asks_for_them_rather_than_building_its_own(self):
        source = Path(resolver.__file__).read_text(encoding="utf-8")
        self.assertIn("opts.update(cookie_opts())", source)
        self.assertNotIn('opts["cookiesfrombrowser"] = (COOKIES_FROM_BROWSER,)',
                         source)


class IgnoreConfigTests(unittest.TestCase):
    """The user's yt-dlp config must not change what the app does.

    A config carrying --cookies-from-browser was re-reading the whole browser
    cookie store on top of the narrow jar this app exports - 1267 cookies where
    110 were intended, undoing the domain narrowing in cookies.py from outside
    the code that implements it.
    """

    def _cmd(self):
        return downloader._command(
            "yt-dlp", "https://example/watch", "best",
            Path("C:/cache"), Path("C:/tmp/print.txt"), [])

    def test_the_flag_is_passed(self):
        self.assertIn("--ignore-config", self._cmd())

    def test_it_comes_before_the_url(self):
        cmd = self._cmd()
        self.assertLess(cmd.index("--ignore-config"), cmd.index("--"))

    def test_the_url_is_still_last_and_still_fenced(self):
        cmd = self._cmd()
        self.assertEqual(cmd[-2:], ["--", "https://example/watch"])

    def test_the_reason_is_recorded_where_someone_would_look(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        self.assertIn("--ignore-config", source.split("def _command")[0])


class ReleaseHelperScriptTests(unittest.TestCase):
    """A release must ship the helper scripts, built against the exe.

    3.4.2 shipped none of them - testers reported "check.bat doesn't exist in
    the build" - and copying the source ones would have been no better: they all
    run ".venv\\Scripts\\python.exe run.py", which a release does not have.
    """

    def _build(self):
        return (Path(__file__).resolve().parent.parent
                / "build.ps1").read_text(encoding="utf-8")

    def test_every_helper_is_written_into_the_release(self):
        source = self._build()
        for name in ("doctor.bat", "check.bat", "clear-cache.bat",
                     "update-ytdlp.bat"):
            self.assertIn(name, source, f"{name} is not put in the build")

    def test_they_call_the_exe_not_the_virtualenv(self):
        source = self._build()
        made = source.split("Adding the helper scripts")[1].split(
            "Adding the documents")[0]
        self.assertIn("ChuuTube2Spout.exe --doctor", made)
        self.assertIn("ChuuTube2Spout.exe --check %*", made)
        self.assertIn("ChuuTube2Spout.exe --clear-cache", made)
        self.assertNotIn(".venv", made)
        self.assertNotIn("run.py", made)

    def test_the_release_updater_does_not_pretend_to_run_pip(self):
        # A frozen build has no pip and no site-packages: yt-dlp is inside the
        # exe. A script that looked like it updated and did nothing would be
        # worse than shipping none.
        made = self._build().split("Adding the helper scripts")[1].split(
            "Adding the documents")[0]
        self.assertNotIn("pip install", made)
        self.assertIn("--yt-dlp --version", made)

    def test_the_flags_they_use_all_exist(self):
        source = Path(app_mod.__file__).read_text(encoding="utf-8")
        for flag in ("--doctor", "--check", "--clear-cache"):
            self.assertIn(f'"{flag}"', source, f"app.py does not handle {flag}")


class LoadTimingTests(unittest.TestCase):
    """Say where the seconds went, so the next optimisation is chosen not guessed.

    Three measured loads: 2-7s of transfer against a flat ~11-12s in front of
    it, independent of file size. That overhead happens inside yt-dlp, so the
    only way to attribute it from outside is to stamp the clock as each phase
    announces itself in the output we are already reading.
    """

    def _timed(self, lines):
        timer = downloader.PhaseTimer()
        for line in lines:
            timer.line(line)
        return timer

    FULL = [
        "[debug] Loaded 1744 extractors",
        "[youtube] abc: Downloading webpage",
        "[youtube] [pot:bgutil:script-deno] Generating a gvs PO Token",
        "[youtube] [jsc:deno] Solving JS challenges using deno",
        "[youtube] abc: Downloading player 06ab6907-main",
        "[download] Sleeping 3.00 seconds as required by the site...",
        "[download] Destination: C:/cache/abc.f137.mp4",
    ]

    def test_every_phase_it_saw_is_reported(self):
        out = self._timed(self.FULL).summary()
        for phase in ("startup", "extract", "token", "challenge", "player",
                      "transfer", "total"):
            self.assertIn(phase, out)

    def test_a_phase_that_never_happened_is_not_invented(self):
        out = self._timed(["[youtube] abc: Downloading webpage",
                           "[download] Destination: x"]).summary()
        self.assertNotIn("token", out)
        self.assertNotIn("challenge", out)

    def test_throttle_sleeps_are_counted_separately(self):
        timer = self._timed(self.FULL)
        self.assertEqual(timer.slept, 3.0)
        self.assertIn("throttle 3.0s", timer.summary())

    def test_a_throttle_is_taken_off_the_phase_that_contained_it(self):
        """The first real measurement read "challenge 5.1s" for a step that took
        1.1s and happened to straddle a 4s throttle - and nearly sent us
        optimising the JS solver instead of the token. Waiting because YouTube
        said to is not a phase being slow."""
        timer = downloader.PhaseTimer()
        timer.line("[youtube] x: Downloading webpage")
        timer.line("[youtube] [jsc:deno] Solving JS challenges using deno")
        timer.line("[download] Sleeping 0.20 seconds as required by the site...")
        time.sleep(0.30)
        timer.line("[download] Destination: x.mp4")
        seconds = float(timer.summary().split("challenge ")[1].split("s")[0])
        self.assertLess(seconds, 0.25, "throttle still charged to the phase")

    def test_the_subtraction_can_never_go_negative(self):
        # yt-dlp announces a sleep before taking it, so a run that dies
        # mid-sleep can subtract more than actually elapsed.
        timer = downloader.PhaseTimer()
        timer.line("[youtube] x: Downloading webpage")
        timer.line("[download] Sleeping 99.00 seconds as required by the site...")
        timer.line("[download] Destination: x.mp4")
        self.assertNotIn("-", timer.summary())

    def test_a_repeated_marker_does_not_restart_the_phase(self):
        # The client chain retries, so "Downloading webpage" appears once per
        # attempt. Only the first should open the phase.
        timer = self._timed(["[youtube] abc: Downloading webpage"] * 4)
        self.assertEqual(len(timer.marks), 1)

    def test_nothing_recognised_still_reports_a_total(self):
        out = self._timed(["something entirely unexpected"]).summary()
        self.assertIn("total", out)

    def test_it_is_wired_into_the_line_loop_and_can_be_silenced(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        self.assertIn("timer.line(line)", source)
        self.assertIn("if LOAD_TIMING and not hls_only:", source)
        self.assertTrue(config.LOAD_TIMING, "on by default, or nobody sees it")


class PartialPlaybackTests(unittest.TestCase):
    """Start on the half-written file. Venue wifi is the whole reason.

    Timed on a fast line the transfer is 1.5-6.7s; the same 106 MB clip on a
    venue's 2 Mbps is minutes. Waiting for the last byte before showing the
    first frame is what makes that unusable rather than merely slow.
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- when to hand over --------------------------------------------------
    def test_a_file_that_does_not_exist_yet_does_not_fire(self):
        self.assertFalse(downloader._big_enough(self.tmp / "nothing.mp4"))

    def test_a_few_kilobytes_are_not_enough(self):
        clip = self.tmp / "clip.mp4"
        clip.write_bytes(b"x" * 1024)
        self.assertFalse(downloader._big_enough(clip))

    def test_past_the_threshold_it_fires(self):
        clip = self.tmp / "clip.mp4"
        clip.write_bytes(b"x" * int(config.PARTIAL_START_MB * 1024 * 1024 + 1))
        self.assertTrue(downloader._big_enough(clip))

    def test_the_threshold_is_a_size_not_a_percentage(self):
        # A percentage would ask for 40 MB of a long clip and 200 KB of a short
        # one, and be wrong both times. mpv needs a header and some frames.
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        self.assertIn("PARTIAL_START_MB * 1024 * 1024", source)

    # --- what it does to the command ---------------------------------------
    def _cmd(self):
        return downloader._command("yt-dlp", "https://example/watch", "best",
                                   Path("C:/cache"), Path("C:/tmp/p.txt"), [])

    def test_it_writes_straight_to_the_destination(self):
        # Windows will not rename a file another process has open, and that
        # process would be our own mpv. --part and partial playback cannot
        # coexist.
        self.assertIn("--no-part", self._cmd())

    def test_no_overwrites_goes_with_it(self):
        # Together they would treat a truncated file from an interrupted run as
        # already downloaded and skip it - a broken clip that never repairs.
        self.assertNotIn("--no-overwrites", self._cmd())

    def test_the_url_is_still_last_and_still_fenced(self):
        self.assertEqual(self._cmd()[-2:], ["--", "https://example/watch"])

    # --- handing it to mpv --------------------------------------------------
    def _player(self, token=1, partial=None):
        p = player_mod.Player.__new__(player_mod.Player)
        # What Player.__init__ sets for the status line; the stub base
        # class would answer anything missing with a truthy placeholder.
        p._headline, p._fetch_note, p._size_note = "idle", "", ""
        p._load_token = token
        p._destroyed = False
        p._partial_token = partial
        p.loaded = []
        p.statuses = []
        p.mpv = type("M", (), {
            "loadfile": lambda _s, path, *a: p.loaded.append(path),
            "__setitem__": lambda *a: None})()
        p.statusChanged = type("S", (), {
            "emit": lambda _s, text: p.statuses.append(text)})()
        p.loadingChanged = type("L", (), {"emit": lambda *a: None})()
        return p

    def test_the_partial_file_is_played(self):
        p = self._player()
        p._on_partial(1, "C:/cache/half.mp4")
        self.assertEqual(p.loaded, ["C:/cache/half.mp4"])
        self.assertEqual(p._partial_token, 1)

    def test_a_superseded_load_is_not_started_underneath(self):
        p = self._player(token=9)
        p._on_partial(1, "C:/cache/old.mp4")
        self.assertEqual(p.loaded, [])
        self.assertIsNone(p._partial_token)

    def test_mpv_refusing_the_half_file_is_not_fatal(self):
        # The download continues either way, and _on_cached still fires with
        # the finished file.
        p = self._player()
        p.mpv = type("M", (), {
            "loadfile": lambda *a: (_ for _ in ()).throw(RuntimeError("moov?")),
            "__setitem__": lambda *a: None})()
        p._on_partial(1, "C:/cache/half.mp4")
        self.assertIsNone(p._partial_token)
        self.assertTrue(any("waiting for the file" in s for s in p.statuses))

    def test_completing_after_a_partial_play_swaps_at_the_same_instant(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        # Not _resolved: that would restart the clip from zero just as it got
        # going. _on_cached already reloads at the current time-pos.
        self.assertIn("if self._partial_token == token:", source)
        self.assertIn("self._cached.emit(token, local_stream(clip))", source)


class RoadmapStaysInternalTests(unittest.TestCase):
    """ROADMAP.md is working notes, not a document for release users.

    It names unfixed bugs, records what betatesters said, and argues with
    earlier decisions. Useful in the repo; not something to hand someone who
    downloaded a zip. Kept out of the build until the repo is actually public.
    """

    def _root(self):
        return Path(__file__).resolve().parent.parent

    def test_the_roadmap_exists_to_be_kept_out_of(self):
        self.assertTrue((self._root() / "ROADMAP.md").is_file())

    def test_the_build_does_not_copy_it(self):
        source = (self._root() / "build.ps1").read_text(encoding="utf-8")
        self.assertNotIn("ROADMAP", source)

    def test_the_documents_it_does_copy_are_the_public_ones(self):
        source = (self._root() / "build.ps1").read_text(encoding="utf-8")
        shipped = source.split('Say "Adding the documents"')[1][:400]
        for name in ("LICENSE", "SECURITY.md", "README.md"):
            self.assertIn(name, shipped)


class PartialFileReleasedOnRetryTests(unittest.TestCase):
    """A failed attempt must stop mpv reading the file it abandoned.

    Seen in a betatester log: mweb played 10% of iz1CAkDctL4.f137.mp4, then
    403'd mid-transfer, and the chain moved on with mpv still holding that
    half-written file. It survived only because web_safari happened to pick a
    different format. The output template is keyed on format id, so had the
    next strategy chosen 137 again, yt-dlp would have written into the file mpv
    had open - resuming from the middle, or on "fresh start" rewriting from
    byte zero underneath a running decoder.
    """

    def _player(self, token=1, partial=1):
        p = player_mod.Player.__new__(player_mod.Player)
        # What Player.__init__ sets for the status line; the stub base
        # class would answer anything missing with a truthy placeholder.
        p._headline, p._fetch_note, p._size_note = "idle", "", ""
        p._load_token, p._destroyed, p._partial_token = token, False, partial
        p.stream, p._blank, p.commands = "something", False, []
        p.mpv = type("M", (), {
            "command": lambda _s, *a: p.commands.append(a)})()
        p.frameReady = type("F", (), {"emit": lambda *a: None})()
        p.statusChanged = type("S", (), {"emit": lambda *a: None})()
        return p

    def test_mpv_is_told_to_let_go(self):
        p, done = self._player(), threading.Event()
        p._on_partial_lost(1, done)
        self.assertEqual(p.commands, [("stop",)])
        self.assertIsNone(p._partial_token)

    def test_the_slot_goes_black_rather_than_holding_a_dead_frame(self):
        p, done = self._player(), threading.Event()
        p._on_partial_lost(1, done)
        self.assertTrue(p._blank)
        self.assertIsNone(p.stream)

    def test_the_waiting_download_thread_is_always_released(self):
        # It blocks on this; a missed set() costs it RELEASE_TIMEOUT seconds.
        p, done = self._player(), threading.Event()
        p._on_partial_lost(1, done)
        self.assertTrue(done.is_set())

    def test_a_superseded_load_is_left_alone_but_still_released(self):
        p, done = self._player(token=9, partial=None), threading.Event()
        p._on_partial_lost(1, done)
        self.assertEqual(p.commands, [])
        self.assertEqual(p.stream, "something")
        self.assertTrue(done.is_set(), "early return skipped the set()")

    def test_nothing_playing_partially_is_a_no_op(self):
        p, done = self._player(partial=None), threading.Event()
        p._on_partial_lost(1, done)
        self.assertEqual(p.commands, [])
        self.assertTrue(done.is_set())

    def test_it_does_not_call_stop_which_would_orphan_the_load(self):
        # Player.stop() bumps _load_token, which would make the load still
        # trying to succeed look stale to its own callbacks.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _on_partial_lost")[1].split("def _on_cached")[0]
        self.assertNotIn("self.stop()", body)
        self.assertIn('self.mpv.command("stop")', body)

    def test_the_downloader_only_fires_it_after_a_failure(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        self.assertIn("if handed_over and proc.returncode != 0 and on_partial_lost:",
                      source)


class CookieJarTrimmingTests(unittest.TestCase):
    """The export itself must not build a jar YouTube will refuse."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="chuutube-jar-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.profile = self.dir / "abc.default"
        self.profile.mkdir()
        self.dest = self.dir / "out" / "cookies.txt"

    def _store(self, rows):
        db = sqlite3.connect(str(self.profile / "cookies.sqlite"))
        db.execute("CREATE TABLE moz_cookies (id INTEGER, host TEXT, path TEXT,"
                   " isSecure INTEGER, expiry INTEGER, name TEXT, value TEXT)")
        db.executemany("INSERT INTO moz_cookies VALUES (?,?,?,?,?,?,?)", [
            (i, ".youtube.com", "/", 1, 2000000000, name, value)
            for i, (name, value) in enumerate(rows, start=1)])
        db.commit()
        db.close()

    def _export(self):
        return cookies_mod.export(f"firefox:{self.profile}", self.dest)

    def _names(self, path):
        return {ln.split("\t")[5] for ln in path.read_text().splitlines()
                if ln and not ln.startswith("#")}

    def test_surface_tokens_are_dropped_even_from_a_small_jar(self):
        # Always, not only when over budget: they are never of any use here.
        self._store([("SID", "abc"), ("ST-xsrdfg", "junk"), ("ST-1x2y", "junk")])
        path, _ = self._export()
        self.assertEqual(self._names(path), {"SID"})

    def test_an_oversized_jar_is_cut_back_to_the_login(self):
        big = "x" * 2000
        self._store([("SID", "abc"), ("__Secure-3PSID", "def"),
                     ("YT_EXPERIMENT_A", big), ("YT_EXPERIMENT_B", big),
                     ("YT_EXPERIMENT_C", big), ("YT_EXPERIMENT_D", big)])
        path, note = self._export()
        self.assertEqual(self._names(path), {"SID", "__Secure-3PSID"})
        self.assertIn("413", note or "", "the note should name what it prevents")

    def test_a_jar_that_fits_is_left_completely_alone(self):
        # Trimming is a last resort - an unrecognised cookie may still matter,
        # and dropping it when there was room would be losing something for
        # nothing.
        self._store([("SID", "abc"), ("SOMETHING_ELSE", "small")])
        path, note = self._export()
        self.assertEqual(self._names(path), {"SID", "SOMETHING_ELSE"})
        self.assertIsNone(note)

    def test_the_file_is_still_usable_after_trimming(self):
        self._store([("SID", "abc"), ("JUNK", "y" * 9000)])
        path, _ = self._export()
        rows = [ln for ln in path.read_text().splitlines()
                if ln and not ln.startswith("#")]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(len(row.split("\t")), 7)

    def test_a_jar_of_nothing_but_ballast_still_says_so(self):
        self._store([("ST-a", "junk"), ("ST-b", "junk")])
        path, warning = self._export()
        self.assertIsNone(path)
        self.assertIn("signed in", warning)


class OversizedCookieJarTests(unittest.TestCase):
    """A jar too big to send is a dead app, not a slow one.

    A tester's log: `HTTP Error 413: Request Entity Too Large` on "Downloading
    webpage", four times, then again on the API page, and the load was over
    before a format had been chosen. 413 is YouTube refusing the request
    because the headers are too big, and the Cookie header is what makes them
    big - so it is not the client's fault and every client fails identically.

    Two halves: stop building a jar that big, and survive one anyway.
    """

    def test_the_ballast_cookies_are_never_exported(self):
        # ST-* are per-surface tokens YouTube sets by the dozen as you browse.
        # Nothing here needs them and they are what grows the jar.
        self.assertTrue("ST-".startswith(cookies_mod.BALLAST_PREFIXES[0][:3]))
        self.assertIn("ST-", cookies_mod.BALLAST_PREFIXES)

    def test_the_login_cookies_are_the_ones_kept(self):
        for name in ("SID", "__Secure-3PSID", "__Secure-1PAPISID",
                     "LOGIN_INFO", "VISITOR_INFO1_LIVE",
                     "__Secure-ROLLOUT_TOKEN"):
            with self.subTest(name=name):
                self.assertIn(name, cookies_mod.ESSENTIAL_COOKIES)

    def test_google_account_cookies_are_still_not_in_scope(self):
        # The keep-list must not become a way back in for the domain narrowing
        # that keeps Gmail and Drive out of the jar - that is done by domain,
        # and this list only ever narrows further.
        self.assertNotIn("google.com", cookies_mod.DEFAULT_DOMAINS)

    def test_the_budget_leaves_room_for_the_rest_of_the_headers(self):
        # The real limit is a total header budget, not a cookie one.
        self.assertLessEqual(cookies_mod.MAX_JAR_KB, 8.0)

    def test_413_is_recognised(self):
        for line in ("WARNING: [youtube] HTTP Error 413: Request Entity Too "
                     "Large. Retrying (1/3)...",
                     "ERROR: [youtube] x: Unable to download API page: "
                     "HTTP Error 413: Request Entity Too Large"):
            with self.subTest(line=line):
                self.assertTrue(downloader._TOO_LARGE.search(line))

    def test_an_ordinary_403_is_not_mistaken_for_it(self):
        self.assertIsNone(downloader._TOO_LARGE.search(
            "ERROR: unable to download video data: HTTP Error 403: Forbidden"))


class CookielessRetryTests(unittest.TestCase):
    """On 413 the chain drops the cookies rather than the load."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="chuutube-413-")
        source = Path(__file__).resolve().parent / "fake_413_ytdlp.py"
        cls.tool = str(Path(cls.dir) / "f413_ytdlp.py")
        shutil.copy(source, cls.tool)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="chuutube-cache-")
        self.addCleanup(shutil.rmtree, self.cache, True)
        self._real_cmd = downloader.ytdlp_command
        self._real_cookies = downloader.cookie_args
        downloader.ytdlp_command = lambda *a, **k: [sys.executable, self.tool]
        # A jar exists, so the first attempt is made with --cookies.
        jar = Path(self.cache) / "cookies.txt"
        jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        downloader.cookie_args = lambda: ["--cookies", str(jar)]

    def tearDown(self):
        downloader.ytdlp_command = self._real_cmd
        downloader.cookie_args = self._real_cookies

    def _invocations(self):
        return int((Path(self.cache) / "_invocations").read_text())

    def test_the_load_still_succeeds(self):
        clip = downloader.download("https://example/watch", 1080, self.cache)
        self.assertEqual(clip.height, 1080)

    def test_it_costs_one_retry_not_a_whole_chain(self):
        # 413 is the jar, not the client, so working through the remaining
        # clients with the same jar would just be four more identical failures.
        downloader.download("https://example/watch", 1080, self.cache)
        self.assertEqual(self._invocations(), 2)

    def test_the_retry_really_does_omit_the_cookies(self):
        # The fake only succeeds when --cookies is absent, so a passing load is
        # itself the proof - assert the mechanism too, so a fake that stopped
        # checking would not quietly make this vacuous.
        seen = []
        real = downloader._command
        def spy(*a, **k):
            seen.append(k.get("cookies", True))
            return real(*a, **k)
        downloader._command = spy
        try:
            downloader.download("https://example/watch", 1080, self.cache)
        finally:
            downloader._command = real
        self.assertEqual(seen, [True, False])

    def test_the_cookieless_attempts_cannot_retrigger_it(self):
        # The tail is replaced, not inserted into: leaving the cookie-bearing
        # copies queued would 413 again and splice again, without end.
        attempts = [downloader.Attempt("a", []), downloader.Attempt("b", [])]
        attempts[1:] = [a._replace(cookies=False) for a in attempts[0:]]
        self.assertEqual([a.cookies for a in attempts], [True, False, False])

    def test_an_attempt_carries_its_own_allowances(self):
        a = downloader.Attempt("default", [])
        self.assertFalse(a.allow_hls)
        self.assertTrue(a.cookies)


class ClientChainOrderTests(unittest.TestCase):
    """The order is measured, not guessed, and "default" is an entry in it.

    One logged load took 38.9s: mweb spent 12.5s reaching a 403 partway through
    the transfer, web_safari offered only HLS, tv_simply was skipped outright
    for not supporting cookies, tv came back UNPLAYABLE, and then yt-dlp's own
    choice got 1080p in 8.5s. It was tried last because it was hardcoded to be
    appended after everything else.

    The three failures in between were not free either: they are requests
    YouTube counts, and "HTTP Error 429: Too Many Requests" appears on every
    client after the first, which is the chain rate-limiting itself.
    """

    def test_default_is_tried_first(self):
        self.assertEqual(downloader._strategies()[0][0], "default")

    def test_the_first_attempt_passes_no_client_argument(self):
        # "default" means no --extractor-args player_client at all, not a client
        # literally named default.
        _, extra = downloader._strategies()[0]
        self.assertNotIn("player_client", " ".join(extra))

    def test_it_is_not_tried_twice(self):
        labels = [label for label, _ in downloader._strategies()]
        self.assertEqual(labels.count("default"), 1)

    def test_a_chain_without_it_still_gets_it_as_a_floor(self):
        # Never leave a user's edited chain with no fallback to yt-dlp's own
        # judgement - that is the one that works when the named clients have all
        # been broken by a YouTube change.
        with patched(downloader, "CLIENT_CHAIN", ["mweb", "tv"]):
            labels = [label for label, _ in downloader._strategies()]
        self.assertEqual(labels[:2], ["mweb", "tv"])
        self.assertIn("default", labels)

    def test_the_auto_spellings_all_mean_the_same_thing(self):
        for name in ("default", "auto", "none", "DEFAULT"):
            with self.subTest(name=name):
                with patched(downloader, "CLIENT_CHAIN", [name, "mweb"]):
                    first, extra = downloader._strategies()[0]
                self.assertEqual(first, "default")
                self.assertNotIn("player_client", " ".join(extra))

    def test_an_explicit_player_client_still_wins_outright(self):
        with patched(downloader, "PLAYER_CLIENT", "ios"):
            labels = [label for label, _ in downloader._strategies()]
        self.assertEqual(labels[0], "ios")
        self.assertNotIn("default", labels[:1])

    def test_tv_simply_is_gone(self):
        # It is skipped by yt-dlp whenever cookies are passed, and they always
        # are. Keeping it only spent time and one more request on the limit.
        self.assertNotIn("tv_simply", config.CLIENT_CHAIN)

    def test_the_chain_stays_short(self):
        # Each entry is a full page extraction and a PO token, ~4-12s, and one
        # more request against the rate limit.
        self.assertLessEqual(len(downloader._strategies()), 6)


class HlsFallbackChainTests(unittest.TestCase):
    """An HLS-only client is a poor offer, not a working one.

    When a client goes SABR-only, yt-dlp falls back to the m3u8 manifest. That
    costs three ways at once: the fragments come with a server-imposed throttle
    ("Sleeping 2.00 seconds as required by the site"), the muxed HLS itags cap
    lower than the progressive ones - one load came back as itag 94, 480p, with
    1080p sitting right there - and the closing ffmpeg fixup renames a file mpv
    would be holding, which rules out playing while it downloads.

    So the chain now walks away from it before a byte moves and keeps it in
    reserve. Nothing is thrown away: if no other client works, it comes back.
    """

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="chuutube-hls-")
        source = Path(__file__).resolve().parent / "fake_hls_ytdlp.py"
        cls.tools = {}
        for name in ("hlsonly_ytdlp", "hlsthenhttp_ytdlp"):
            path = Path(cls.dir) / f"{name}.py"
            shutil.copy(source, path)
            cls.tools[name] = str(path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="chuutube-cache-")
        self._real = downloader.ytdlp_command
        self.addCleanup(shutil.rmtree, self.cache, True)

    def tearDown(self):
        downloader.ytdlp_command = self._real

    def _use(self, name):
        downloader.ytdlp_command = lambda *a, **k: [sys.executable, self.tools[name]]

    def _invocations(self):
        return int((Path(self.cache) / "_invocations").read_text())

    def test_a_later_progressive_client_wins(self):
        self._use("hlsthenhttp_ytdlp")
        clip = downloader.download("https://example/watch", 1080, self.cache)
        self.assertEqual((clip.height, clip.width), (1080, 1920))
        self.assertTrue(clip.video_path.endswith("clip.f137.mp4"))

    def test_the_hls_file_is_never_written_when_something_better_exists(self):
        # Walking away has to happen before the transfer, or the throttle has
        # already been paid and there was no point.
        self._use("hlsthenhttp_ytdlp")
        downloader.download("https://example/watch", 1080, self.cache)
        self.assertFalse((Path(self.cache) / "clip.f94.mp4").exists())

    def test_it_costs_exactly_one_extra_client(self):
        self._use("hlsthenhttp_ytdlp")
        downloader.download("https://example/watch", 1080, self.cache)
        self.assertEqual(self._invocations(), 2)

    def test_hls_is_still_used_when_it_is_all_there_is(self):
        # The whole chain offers nothing else, so the one we walked away from
        # is now the best offer on the table. Failing here would be worse than
        # 480p.
        self._use("hlsonly_ytdlp")
        clip = downloader.download("https://example/watch", 1080, self.cache)
        self.assertEqual(clip.height, 480)
        self.assertTrue(clip.video_path.endswith("clip.f94.mp4"))

    def test_the_fallback_is_queued_once_and_only_once(self):
        # _defer_hls appends to the list being iterated. Re-queueing on the
        # retry's own outcome would loop forever, so hls_deferred is cleared
        # when it is spent - this is the test that would hang if it were not.
        self._use("hlsonly_ytdlp")
        downloader.download("https://example/watch", 1080, self.cache)
        self.assertEqual(self._invocations(), len(downloader._strategies()) + 1)

    def test_the_retry_is_the_one_allowed_to_accept_hls(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        self.assertIn(
            'attempts.append(Attempt(f"{label} (HLS fallback)", extra, allow_hls=True))',
            source)
        self.assertIn("if fragmented and destination is None and not allow_hls:",
                      source)


class WorthHandingOverTests(unittest.TestCase):
    """When playing the half-written file is a bad trade.

    Both cases come from one betatester log where a single load took 46 seconds
    and burned four client attempts.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "clip.f137.mp4"
        self.path.write_bytes(b"\0" * int(downloader.PARTIAL_START_MB * 1024 * 1024))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _long_ago(self):
        return time.monotonic() - (downloader.PARTIAL_AFTER_S + 5)

    def test_a_slow_transfer_is_handed_over(self):
        self.assertTrue(
            downloader._worth_handing_over(self.path, self._long_ago(), False))

    def test_a_fragmented_download_is_never_handed_over(self):
        # hlsnative ends by running the file through ffmpeg and renaming the
        # result over the original. Windows will not replace a file mpv has
        # open, so holding it turns a finished 74 MB download into
        # "PermissionError: [WinError 5] Access is denied" and discards it.
        self.assertFalse(
            downloader._worth_handing_over(self.path, self._long_ago(), True))

    def test_a_transfer_that_just_started_is_left_alone(self):
        # It would be done in about the time the hand-over costs, and reloading
        # two seconds in reads as the clip starting over.
        self.assertFalse(
            downloader._worth_handing_over(self.path, time.monotonic(), False))

    def test_a_transfer_that_has_not_started_is_left_alone(self):
        self.assertFalse(downloader._worth_handing_over(self.path, None, False))

    def test_a_file_that_is_still_too_small_waits(self):
        small = Path(self.tmp) / "small.mp4"
        small.write_bytes(b"\0" * 1024)
        self.assertFalse(
            downloader._worth_handing_over(small, self._long_ago(), False))

    def test_the_hls_lines_yt_dlp_actually_prints_are_recognised(self):
        for line in ("[hlsnative] Downloading m3u8 manifest",
                     "[hlsnative] Total fragments: 32",
                     '[debug] Invoking hlsnative downloader on "https://x/y"'):
            with self.subTest(line=line):
                self.assertTrue(downloader._FRAGMENTED.search(line))

    def test_a_plain_http_download_is_not_mistaken_for_fragments(self):
        for line in ('[debug] Invoking http downloader on "https://x/y"',
                     "[download] Destination: C:\\cache\\clip.f137.mp4",
                     "[download]  28.1% of   68.60MiB at   17.60MiB/s ETA 00:02"):
            with self.subTest(line=line):
                self.assertIsNone(downloader._FRAGMENTED.search(line))

    def test_the_gate_is_what_the_download_loop_calls(self):
        source = Path(downloader.__file__).read_text(encoding="utf-8")
        self.assertIn("if _worth_handing_over(destination, transfer_began, fragmented):",
                      source)
        self.assertIn("transfer_began = time.monotonic()", source)


class HandoverReloadTests(unittest.TestCase):
    """The finished download must be re-opened, at the playhead, every time.

    Two bugs, one place. First the reload started the clip over, because a
    threshold ("only carry the position across if past 0.5s") is triggered by
    exactly the case that matters: the handover happens a second or two into
    playback, so the position was always thrown away.

    Then skipping the reload - the finished file being byte-for-byte the file
    mpv already had open, thanks to --no-part - looked like the obvious fix and
    was worse. mpv caches the file size when it opens it and does not notice it
    growing:

        error ffmpeg/demuxer: mov,mp4: stream 0, offset 0x802800: partial file
        info cplayer: Video: no video

    It ran off the end of what it thought the file was about a third of the way
    in, and loop_file="inf" sent it back to the start. Re-opening the file is
    the only thing that tells mpv how long it actually is.
    """

    def _player(self, at=12.5, token=1, remembered=0.0):
        p = player_mod.Player.__new__(player_mod.Player)
        # What Player.__init__ sets for the status line; the stub base
        # class would answer anything missing with a truthy placeholder.
        p._headline, p._fetch_note, p._size_note = "idle", "", ""
        p._load_token, p._destroyed = token, False
        p._partial_token, p._partial_path = token, "/cache/clip.f137.mp4"
        p.stream, p.loads, p.starts = None, [], []
        # What playback last reported, as _on_time_pos records it. Without
        # this the fixture leans on the stub's catch-all attribute and the
        # handover cannot tell a real position from a missing one.
        p._last_time = remembered
        props = {"time-pos": at}
        p.mpv = type("M", (), {
            "__getitem__": lambda _s, k: props[k],
            "__setitem__": lambda _s, k, v: p.starts.append((k, v)),
            "loadfile": lambda _s, url, *a: p.loads.append(url),
        })()
        p.statusChanged = type("S", (), {"emit": lambda *a: None})()
        return p

    @staticmethod
    def _stream(path="/cache/clip.f137.mp4"):
        return resolver.Stream(video_url=path, direct=True, client="local")

    def test_the_status_says_cached_once(self):
        # Was "1080p cached - When I blow this whistle, a swimming pool
        # appears. (cached)": the label already says it.
        p = self._player()
        seen = []
        p.statusChanged = types.SimpleNamespace(emit=seen.append)
        stream = resolver.Stream(video_url="/cache/clip.f137.mp4", direct=True,
                                 client="local", height=1080,
                                 title="When I blow this whistle")
        with contextlib.redirect_stderr(io.StringIO()):
            p._on_cached(1, stream)
        self.assertEqual(seen[-1], "1080p cached - When I blow this whistle")
        self.assertEqual(seen[-1].count("cached"), 1)

    def test_the_same_file_is_still_re_opened(self):
        # The whole point: mpv's idea of how big the file is only updates here.
        p = self._player()
        p._on_cached(1, self._stream())
        self.assertEqual(p.loads, ["/cache/clip.f137.mp4"])

    def test_the_playhead_survives_the_reload(self):
        p = self._player(at=12.5)
        p._on_cached(1, self._stream())
        self.assertIn(("start", "12.500"), p.starts)

    def test_an_unavailable_playhead_falls_back_to_what_was_remembered(self):
        # THE BUG. A partial file sits at EOF, where mpv answers "unavailable"
        # for time-pos; _get() turns that into 0.0 and the handover restarted a
        # clip that had been playing for 45 seconds.
        p = self._player(at=0.0, remembered=45.2)
        p._on_cached(1, self._stream())
        self.assertIn(("start", "45.200"), p.starts)

    def test_a_live_playhead_still_wins(self):
        # The remembered value is a fallback, not a replacement: it lags by up
        # to one frame.
        p = self._player(at=12.5, remembered=3.0)
        p._on_cached(1, self._stream())
        self.assertIn(("start", "12.500"), p.starts)

    def test_nothing_played_means_nothing_to_resume(self):
        p = self._player(at=0.0, remembered=0.0)
        p._on_cached(1, self._stream())
        self.assertIn(("start", "none"), p.starts)

    def test_a_clip_one_second_in_is_not_started_over(self):
        # The original threshold was 0.5s, and the handover lands right about
        # here - which is why it read as "it starts from the beginning".
        p = self._player(at=0.9)
        p._on_cached(1, self._stream())
        self.assertIn(("start", "0.900"), p.starts)

    def test_a_clip_that_has_not_begun_starts_clean(self):
        p = self._player(at=0.0)
        p._on_cached(1, self._stream())
        self.assertIn(("start", "none"), p.starts)

    def test_the_partial_bookkeeping_is_cleared(self):
        p = self._player()
        stream = self._stream()
        p._on_cached(1, stream)
        self.assertIs(p.stream, stream)
        self.assertIsNone(p._partial_token)
        self.assertIsNone(p._partial_path)

    def test_a_superseded_load_is_ignored_entirely(self):
        p = self._player()
        p._load_token = 7
        p._on_cached(1, self._stream())
        self.assertEqual(p.loads, [])

    def test_there_is_no_shortcut_that_skips_the_reload(self):
        # Guards the regression directly: any "same path, leave it alone" test
        # would pass while mpv silently kept a stale view of the file.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _on_cached")[1].split("def _prepare_urls")[0]
        self.assertNotIn("_already_playing", body)
        self.assertIn("loadfile", body)


class SeekSliderDragTests(unittest.TestCase):
    """Clicking worked; dragging did nothing. They are the same gesture to Qt.

    mousePressEvent handled the click itself and returned without calling
    super(), so QSlider never entered its drag state: no sliderPressed, no
    grabbed handle, and therefore no sliderMoved or sliderReleased. The custom
    path made clicking look completely fine, which is why this survived a
    round of "the slider cannot be moved" that turned out to be about duration.
    """

    def setUp(self):
        self.w = MainWindow()
        self.slider = self.w.panels[0].slider
        self.slider.setRange(0, 212000)

    def _press(self, x):
        class Event:
            def button(self):
                return Qt.LeftButton

            def position(self):
                # .x() is a method on QPointF, not an attribute.
                return type("P", (), {"x": lambda _s: x})()
        self.slider.mousePressEvent(Event())

    def test_the_press_reaches_qslider(self):
        # The one line the original was missing. Without it Qt never grabs the
        # handle, and every drag signal it would emit simply never happens.
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source.split("class SeekSlider")[1].split("class UrlEdit")[0]
        press = body.split("def mousePressEvent")[1]
        self.assertIn("super().mousePressEvent(event)", press)
        self.assertNotIn("event.accept()", press,
                         "accepting and returning is what swallowed the drag")
        # And behaviourally: the stub QSlider counts what reaches it.
        self._press(self.slider.width() / 2)
        self.assertEqual(self.slider.presses_forwarded, 1)

    def test_clicking_still_jumps_to_where_you_clicked(self):
        seen = []
        self.slider.clickedAt.connect(seen.append)
        self._press(self.slider.width() / 2)
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 0)

    def test_the_handle_is_moved_not_just_the_value(self):
        # Qt hit-tests the handle to decide drag versus page-step, so the
        # handle has to be under the cursor before the press is forwarded.
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source.split("class SeekSlider")[1].split("class UrlEdit")[0]
        self.assertIn("self.setSliderPosition(value)", body)

    def test_the_mapping_accounts_for_the_handle_width(self):
        # x/width puts the handle slightly off the cursor near the ends, where
        # Qt then decides the press missed and page-steps - the same bug back
        # again, intermittently.
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("sliderValueFromPosition", source)
        self.assertIn("SC_SliderHandle", source)

    def test_a_right_click_is_left_to_qt(self):
        seen = []
        self.slider.clickedAt.connect(seen.append)

        class Right:
            def button(self):
                return Qt.RightButton

            def position(self):
                return type("P", (), {"x": lambda _s: 10})()
        self.slider.mousePressEvent(Right())
        self.assertEqual(seen, [])

    def test_an_empty_range_does_not_seek(self):
        self.slider.setRange(0, 0)
        seen = []
        self.slider.clickedAt.connect(seen.append)
        self._press(50)
        self.assertEqual(seen, [])


class RuntimeOnlySetupTests(unittest.TestCase):
    """-RuntimeOnly runs beside a frozen exe, where there is no .venv at all.

    The built app offers to install its own runtime on first run, and that
    offer is setup.ps1 -RuntimeOnly. Any line that reaches for
    .venv\\Scripts\\python.exe outside a guard aborts the install with a
    CommandNotFoundException - after the download has already succeeded, so
    the user sees a failure that looks like a network fault.
    """

    @staticmethod
    def _source() -> str:
        return (Path(__file__).resolve().parent.parent
                / "setup.ps1").read_text(encoding="utf-8")

    def _venv_py_lines(self):
        for number, line in enumerate(self._source().splitlines(), 1):
            if "$VenvPy" in line and not line.lstrip().startswith("#"):
                yield number, line

    def test_the_plugin_version_is_read_without_a_venv(self):
        # PyInstaller leaves bgutil_ytdlp_pot_provider-<ver>.dist-info beside
        # the frozen modules, so the bundle can answer this itself.
        source = self._source()
        self.assertIn("function Get-PluginVersion", source)
        self.assertIn("bgutil_ytdlp_pot_provider-*.dist-info", source)
        self.assertIn('Join-Path $Root "_internal"', source)

    def test_no_bare_importlib_probe_survives(self):
        # Both former call sites went through the venv. If one comes back,
        # -RuntimeOnly breaks again in exactly the same way.
        source = self._source()
        self.assertNotIn(
            "$VenvPy -c \"from importlib.metadata import version; "
            "print(version('bgutil-ytdlp-pot-provider'))\" 2>$null)\n"
            "if ($pluginVer)", source)
        probes = source.count("$VenvPy -c \"from importlib.metadata")
        self.assertEqual(probes, 1,
                         "the venv probe belongs only inside Get-PluginVersion")

    def test_pip_uninstall_is_skipped_for_a_frozen_bundle(self):
        # A bundle has no pip, and nothing ever installed the shim into it.
        source = self._source()
        block = source.split("yt-dlp-get-pot 2>&1")[0]
        guard = block.rsplit("if (-not $RuntimeOnly) {", 1)
        self.assertEqual(len(guard), 2, "the uninstall is not guarded")
        self.assertNotIn("\n}\n", guard[1],
                         "the guard closes before the uninstall")

    def test_every_venv_invocation_sits_behind_a_guard(self):
        # Belt and braces: walk setup.ps1 with a brace counter and confirm
        # every line that RUNS $VenvPy sits inside a block -RuntimeOnly never
        # enters. This is the check that would have caught the crash, which
        # struck after the download had already succeeded - so it read as a
        # network fault rather than a bug in the script.
        lines = self._source().splitlines()

        # Get-PluginVersion tests Test-Path $VenvPy before using it, so it is
        # safe wherever it is called from. Exempt its body.
        fn_start = next(i for i, l in enumerate(lines)
                        if l.startswith("function Get-PluginVersion"))
        fn_end = next(i for i in range(fn_start, len(lines))
                      if lines[i] == "}")

        stack = []               # one entry per open brace, True when guarded
        runtime_only_at = None   # depth of an "if ($RuntimeOnly) {" branch
        offenders = []

        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if "& $VenvPy" in line and not stripped.startswith("#") \
                    and not fn_start <= number - 1 <= fn_end \
                    and not any(stack):
                offenders.append(f"setup.ps1:{number}: {stripped}")

            # Character by character, or a one-line "try { } catch { }" pops
            # the enclosing guard before it pushes its own braces back.
            first = True
            for char in stripped:
                if char == "{":
                    if first and stripped.startswith("if ($RuntimeOnly) {"):
                        runtime_only_at = len(stack)
                    guarded = first and (
                        stripped.startswith("if (-not $RuntimeOnly) {")
                        or (stripped.startswith("} else {")
                            and runtime_only_at == len(stack)))
                    stack.append(guarded)
                    first = False
                elif char == "}":
                    if stack:
                        stack.pop()

        self.assertEqual(len(stack), 0,
                         "brace scan did not balance - the walk above is "
                         "no longer reading setup.ps1 correctly")
        self.assertEqual(offenders, [],
                         "these lines run python.exe from a virtual "
                         "environment that -RuntimeOnly never creates:\n"
                         + "\n".join(offenders))


class LongMessageWindowTests(unittest.TestCase):
    """A long error message used to make the window wider than the monitor.

    On a tester's laptop the window grew until it spilled onto the second
    screen. The mechanism is worth stating because it is not "the text is too
    long": a QLabel with no word wrap reports its full text as its MINIMUM
    width, a layout's minimum is the window's minimum, and Qt enforces a
    window minimum by growing the window. Nothing in the app asked for that
    size - the label did, and Qt obliged.

    Eliding alone makes it worse. Elide to the current width and the preferred
    width becomes that width; the layout grows a little; the next elide fits
    more text; repeat until the whole message is on screen. Which is exactly
    what a window creeping across two monitors looks like.
    """

    HUGE = ("SpoutGL is not available - Spout output is disabled. "
            + "(DLL load failed while importing SpoutGL: " + "x" * 600 + ")")

    def test_a_long_message_does_not_set_a_huge_minimum_width(self):
        label = ui_mod.ElidedLabel(self.HUGE)
        self.assertLessEqual(label.minimumWidth(), 200,
                             "this number becomes the window's minimum width")

    def test_the_text_is_shortened_not_just_tooltipped(self):
        label = ui_mod.ElidedLabel(self.HUGE)
        self.assertLess(len(label.text()), len(self.HUGE))
        self.assertEqual(label.toolTip(), self.HUGE)

    def test_setting_the_text_again_does_not_ratchet(self):
        # The old failure: each pass fits a little more, so the window opens a
        # little further, for ever. Re-setting the same text must be stable.
        label = ui_mod.ElidedLabel(self.HUGE)
        first = label.text()
        for _ in range(5):
            label.setText(label.full_text())
        self.assertEqual(label.text(), first)

    def test_the_full_text_survives_eliding(self):
        label = ui_mod.ElidedLabel(self.HUGE)
        label.setText(label.full_text())
        self.assertEqual(label.full_text(), self.HUGE)

    def test_the_spout_failure_notice_is_elided(self):
        # The specific label that caused it. Named in the source rather than
        # constructed here because building the status bar needs a window.
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        bar = source.split("def _build_status_bar")[1].split("\n    def ")[0]
        self.assertIn("ElidedLabel(self.spout.unavailable_reason", bar)
        self.assertNotIn("QLabel(self.spout.unavailable_reason", bar)

    def test_no_plain_label_is_given_a_message_that_can_be_long(self):
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        for forbidden in ("QLabel(self.spout.unavailable_reason",
                          "self.status_label = QLabel"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_the_window_is_checked_against_the_screen_on_startup(self):
        # Belt and braces behind the labels: if a widget ever demands more
        # width than the screen again, the window is pulled back rather than
        # left with its controls off the edge.
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _first_fit")[1].split("\n    def ")[0]
        self.assertIn("_clamp_to_screen()", body)

    def test_clamping_never_stops_the_window_opening(self):
        # It is cosmetic. A Qt build that disagrees about geometry types must
        # not be the reason the app will not start.
        window = MainWindow()
        window._clamp_to_screen()      # must not raise against the stubs


class IdleSenderTests(unittest.TestCase):
    """Both Spout senders exist from startup, not from the first video.

    A Spout sender is registered on its first frame, so until something played
    there was nothing in Resolume's source list to point at - you had to load a
    clip into each slot purely to make the names appear, then clear them. That
    is a bad thing to discover while patching a show.
    """

    def setUp(self):
        self.window = MainWindow()
        self.surface = self.window.surface
        self.sent = []
        self.surface.spout.publish = \
            lambda *a, **k: self.sent.append((a, k))

    def _boot(self):
        self.surface.initializeGL()
        self.surface.tick()

    def test_a_sender_is_published_before_anything_is_loaded(self):
        for player in self.surface.players:
            self.assertFalse(player.target.valid, "fixture assumption")
        self._boot()
        self.assertEqual(len(self.sent), 1,
                         "nothing was published, so Resolume lists no source")

    def test_what_is_published_is_the_idle_frame(self):
        self._boot()
        targets = self.sent[0][0][0]
        self.assertEqual(targets, [self.surface._idle, self.surface._idle])

    def test_both_slots_are_marked_as_changed_so_both_are_sent(self):
        self._boot()
        changed = self.sent[0][0][2]
        self.assertEqual(changed, [True, True])

    def test_the_idle_texture_exists_after_gl_is_ready(self):
        self.surface.initializeGL()
        self.assertTrue(self.surface._idle.valid)

    def test_it_is_not_re_sent_on_every_tick(self):
        # The render loop ticks every 4ms. Re-copying an unchanging black
        # texture 250 times a second would be pure cost.
        self._boot()
        for _ in range(20):
            self.surface.tick()
        self.assertEqual(len(self.sent), 1)

    def test_the_idle_beat_comes_round_again(self):
        self._boot()
        self.surface._last_idle = 0.0          # pretend the interval elapsed
        self.surface.tick()
        self.assertEqual(len(self.sent), 2)

    def test_a_real_frame_takes_over_from_the_idle_one(self):
        self._boot()
        target = self.surface.players[0].target
        target.fbo, target.w, target.h = 7, 1920, 1080
        self.surface.players[0].render = lambda: True
        self.surface.players[1].render = lambda: False
        self.surface._dirty = True
        self.surface.tick()
        targets = self.sent[-1][0][0]
        self.assertIs(targets[0], target,
                      "a loaded slot must publish its own picture")

    def test_the_texture_is_defined_not_merely_allocated(self):
        # glTexImage2D with None allocates without defining the contents, and
        # undefined GPU memory is whatever was there before - which, published
        # to Spout, is someone else's framebuffer on a projector.
        source = Path(surface_mod.__file__).read_text(encoding="utf-8")
        body = source.split("class IdleFrame")[1].split("\nclass ")[0]
        self.assertIn("empty_pixels(self.w, self.h)", body)
        self.assertEqual(len(surface_mod.empty_pixels(4, 3)), 4 * 3 * 4)

    def test_it_is_released_on_shutdown(self):
        source = Path(surface_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def shutdown")[1].split("\n    def ")[0]
        self.assertIn("_idle.destroy()", body)

    def test_it_can_be_switched_off(self):
        from chuutube2spout import config
        self.assertTrue(config.IDLE_SENDERS,
                        "on by default - it is what makes the app patchable")

    def test_a_gl_failure_is_not_fatal(self):
        # If the texture cannot be made, senders should appear on the first
        # clip as they always did - not take the app down.
        frame = surface_mod.IdleFrame(1920, 1080)
        frame.tex = 0
        with mock.patch.object(surface_mod.GL, "glGenTextures",
                               side_effect=RuntimeError("no context"),
                               create=True):
            self.assertFalse(frame.create())
        self.assertFalse(frame.valid)


class LogFileTests(unittest.TestCase):
    """Console output is also written to a file beside the app.

    "No logs for this" was the honest answer to most reports: run.bat leaves a
    console window open behind the app, the exe does not, and everything the
    app knows how to say it says on stderr.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="chuutube-log-"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _file(self, name="chuutube-20260920-120000-1.log"):
        handle = open(self.dir / name, "w", encoding="utf-8")
        self.addCleanup(handle.close)
        return handle

    def test_it_writes_to_both_the_console_and_the_file(self):
        console = io.StringIO()
        handle = self._file()
        tee = logfile.Tee(console, handle)
        tee.write("hello\n")
        handle.flush()
        self.assertEqual(console.getvalue(), "hello\n")
        self.assertIn("hello", (self.dir / "chuutube-20260920-120000-1.log")
                      .read_text(encoding="utf-8"))

    def test_a_missing_console_is_not_a_problem(self):
        # A frozen windowed build hands us None for stdout.
        handle = self._file()
        tee = logfile.Tee(None, handle)
        tee.write("still logged\n")
        handle.flush()
        self.assertIn("still logged",
                      (self.dir / "chuutube-20260920-120000-1.log")
                      .read_text(encoding="utf-8"))

    def test_the_file_is_redacted(self):
        handle = self._file()
        tee = logfile.Tee(None, handle)
        tee.write("https://x.googlevideo.com/videoplayback?id=abc&pot=SECRET\n")
        handle.flush()
        written = (self.dir / "chuutube-20260920-120000-1.log").read_text(
            encoding="utf-8")
        self.assertNotIn("SECRET", written)
        self.assertIn("pot=", written)

    def test_a_credential_split_across_two_writes_is_still_caught(self):
        # This is why the file side buffers by line. print() writes the text
        # and the newline separately, and some callers write in fragments -
        # scrub half a URL and the half with the token in it looks clean.
        handle = self._file()
        tee = logfile.Tee(None, handle)
        tee.write("https://x.googlevideo.com/videoplayback?pot=SEC")
        tee.write("RETVALUE&z=1\n")
        handle.flush()
        written = (self.dir / "chuutube-20260920-120000-1.log").read_text(
            encoding="utf-8")
        self.assertNotIn("SECRETVALUE", written)

    def test_an_unterminated_line_is_flushed_on_demand(self):
        handle = self._file()
        tee = logfile.Tee(None, handle)
        tee.write("no newline here")
        tee.flush()
        self.assertIn("no newline here",
                      (self.dir / "chuutube-20260920-120000-1.log")
                      .read_text(encoding="utf-8"))

    def test_an_error_line_is_flushed_at_once(self):
        # The next thing to happen may be the process disappearing.
        handle = self._file()
        tee = logfile.Tee(None, handle)
        tee.write("mpv fatal error: no such file\n")
        self.assertIn("fatal", (self.dir / "chuutube-20260920-120000-1.log")
                      .read_text(encoding="utf-8"))

    def test_ordinary_lines_do_not_each_cost_a_disk_sync(self):
        # A flush per line is GUI-thread work when the line came from the GUI
        # thread, and mpv and yt-dlp are both chatty at their defaults.
        handle = self._file()
        flushes = []
        real_flush = handle.flush

        def counting_flush():
            flushes.append(1)
            real_flush()

        handle.flush = counting_flush
        tee = logfile.Tee(None, handle)
        for i in range(50):
            tee.write(f"[download] {i}% of 100MiB\n")
        self.assertLessEqual(len(flushes), 2,
                             f"{len(flushes)} disk syncs for 50 quiet lines")

    def test_an_explicit_flush_is_always_honoured(self):
        handle = self._file()
        tee = logfile.Tee(None, handle)
        tee.write("quiet line\n")
        tee.flush()
        self.assertIn("quiet line",
                      (self.dir / "chuutube-20260920-120000-1.log")
                      .read_text(encoding="utf-8"))

    def test_a_clean_exit_pushes_the_last_lines_out(self):
        source = Path(logfile.__file__).read_text(encoding="utf-8")
        self.assertIn("atexit.register(_flush_all)", source)

    def test_a_broken_file_does_not_break_printing(self):
        console = io.StringIO()
        handle = self._file()
        handle.close()                       # disk full, removable drive gone
        tee = logfile.Tee(console, handle)
        tee.write("the app keeps running\n")
        self.assertEqual(console.getvalue(), "the app keeps running\n")

    def test_fileno_is_the_console_not_the_log(self):
        # Subprocesses inherit our handles. A child writing straight into the
        # log file would interleave with our own buffered lines.
        handle = self._file()
        tee = logfile.Tee(sys.__stdout__, handle)
        self.assertEqual(tee.fileno(), sys.__stdout__.fileno())

    def test_pruning_keeps_the_newest(self):
        for stamp in ("20260101-000000-1", "20260102-000000-1",
                      "20260103-000000-1", "20260104-000000-1"):
            (self.dir / f"chuutube-{stamp}.log").write_text("x")
        logfile.prune(self.dir, 2)
        left = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(left, ["chuutube-20260103-000000-1.log",
                                "chuutube-20260104-000000-1.log"])

    def test_pruning_leaves_other_files_alone(self):
        (self.dir / "settings.txt").write_text("x")
        (self.dir / "chuutube-20260101-000000-1.log").write_text("x")
        logfile.prune(self.dir, 1)
        self.assertTrue((self.dir / "settings.txt").exists())

    def test_the_current_run_is_one_of_the_ones_kept(self):
        # Pruning runs AFTER the new file is opened, so LOG_KEEP=1 must not
        # delete the log being written. Stated as an ordering check because
        # the bug it prevents only shows up at that one value.
        source = Path(logfile.__file__).read_text(encoding="utf-8")
        body = source.split("def start(")[1]
        self.assertLess(body.index("open(path"), body.index("prune(directory"),
                        "prune must run after the file is opened")

    def test_starting_twice_is_harmless(self):
        source = Path(logfile.__file__).read_text(encoding="utf-8")
        body = source.split("def start(")[1].split("\ndef ")[0]
        self.assertIn("if _started is not None:", body)

    def test_the_default_location_is_beside_the_app(self):
        directory = logfile.log_dir(Path("/somewhere/app"))
        self.assertEqual(directory, Path("/somewhere/app") / "logs")

    def test_the_yt_dlp_child_does_not_log(self):
        # The frozen build runs yt-dlp by re-launching itself. One log file per
        # download would age out the one anybody wanted.
        source = Path(app_mod.__file__).read_text(encoding="utf-8")
        head = source.split("def main(")[1]
        self.assertLess(head.index("YTDLP_FLAG"), head.index("start_logging"),
                        "logging must start after the yt-dlp re-entry check")

    def test_the_help_box_says_where_the_log_is(self):
        # Where someone looks when they want to tell you something went wrong.
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _show_help")[1].split("\n    def ")[0]
        self.assertIn("log_line()", body)


class SpoutCanvasTests(unittest.TestCase):
    """Every sender publishes at one fixed resolution.

    Before this each clip arrived in Resolume at its own size, so a 720p clip,
    a 4K one and a phone video were three differently sized sources and each
    had to be fitted to the canvas by hand, mid-set. The fix is not a resize
    pass: mpv renders into whatever target it is given, so giving it a constant
    one costs nothing and the scaling is mpv's own.
    """

    @contextlib.contextmanager
    def _settings(self, **settings):
        """Re-import config with these settings, and put it back afterwards.

        Yields the module, not functions pulled off it: importlib.reload
        rebinds the module's globals in place, so a function captured from the
        reloaded module reads the RESTORED values once the block ends - which
        silently turned every one of these into a test of the defaults.
        """
        keys = {f"CHUUTUBE2SPOUT_{k}": str(v) for k, v in settings.items()}
        try:
            with mock.patch.dict(os.environ, keys, clear=False):
                yield importlib.reload(config)
        finally:
            # OUTSIDE the patch, or the restoring reload reads the patched
            # environment and every later test sees these settings.
            importlib.reload(config)

    # -- the default (0.9.9.38: each clip at its own size, K) ---------------

    def test_the_default_is_each_clips_own_size(self):
        # K: "only ever send the actual video res" - a 4:3 clip with black side
        # bars on a 1920x1080 canvas showed black in Resolume.
        self.assertEqual((config.SENDER_WIDTH, config.SENDER_HEIGHT), (0, 0))
        self.assertIsNone(config.canvas_problem(), "the defaults must start")
        self.assertEqual(config.canvas(), (0, 0))
        self.assertEqual(config.canvas_for(1440, 1080), (1440, 1080))
        self.assertEqual(config.canvas_for(640, 480), (640, 480))

    def test_the_default_preview_is_16_9(self):
        self.assertAlmostEqual(config.PREVIEW_ASPECT, 16 / 9, places=4)

    # -- a fixed canvas, opted into -----------------------------------------

    def test_every_clip_lands_on_the_same_canvas(self):
        with self._settings(SENDER_WIDTH=1920, SENDER_HEIGHT=1080) as cfg:
            sizes = [(640, 480), (1280, 720), (3840, 2160), (1080, 1920)]
            published = {cfg.canvas_for(w, h) for w, h in sizes}
        self.assertEqual(published, {(1920, 1080)})

    def test_the_idle_frame_is_the_canvas_size(self):
        # Or the slot changes size the moment something plays, which is the
        # thing a canvas exists to stop.
        with self._settings(SENDER_WIDTH=1280, SENDER_HEIGHT=720) as cfg:
            self.assertEqual((cfg.IDLE_SENDER_WIDTH, cfg.IDLE_SENDER_HEIGHT),
                             (1280, 720))

    def test_the_preview_follows_the_canvas(self):
        with self._settings(SENDER_WIDTH=1280, SENDER_HEIGHT=960) as cfg:
            self.assertAlmostEqual(cfg.PREVIEW_ASPECT, 1280 / 960, places=4)

    # -- a size limit without a canvas (0.9.9.38) ----------------------------

    def test_a_larger_clip_is_scaled_down_keeping_its_shape(self):
        with self._settings(SENDER_MAX_WIDTH=1920, SENDER_MAX_HEIGHT=1080) as cfg:
            self.assertEqual(cfg.canvas_for(3840, 2160), (1920, 1080))
            self.assertEqual(cfg.canvas_for(2880, 2160), (1440, 1080), "4:3")
            self.assertEqual(cfg.canvas_for(1080, 1920), (608, 1080),
                             "vertical")

    def test_a_smaller_clip_is_never_scaled_up(self):
        with self._settings(SENDER_MAX_WIDTH=1920, SENDER_MAX_HEIGHT=1080) as cfg:
            self.assertEqual(cfg.canvas_for(960, 720), (960, 720))

    def test_one_limit_alone_works(self):
        with self._settings(SENDER_MAX_HEIGHT=720) as cfg:
            self.assertEqual(cfg.canvas_for(1920, 1080), (1280, 720))
            self.assertEqual(cfg.canvas_for(1440, 1080), (960, 720))
        with self._settings(SENDER_MAX_WIDTH=1280) as cfg:
            self.assertEqual(cfg.canvas_for(1920, 1080), (1280, 720))

    def test_a_fixed_canvas_wins_over_the_limit(self):
        with self._settings(SENDER_WIDTH=1920, SENDER_HEIGHT=1080,
                            SENDER_MAX_HEIGHT=720) as cfg:
            self.assertEqual(cfg.canvas_for(640, 480), (1920, 1080))

    def test_the_window_says_how_senders_are_sized(self):
        self.assertEqual(config.output_description(), "clip size")
        with self._settings(SENDER_MAX_WIDTH=1920, SENDER_MAX_HEIGHT=1080) as cfg:
            self.assertEqual(cfg.output_description(),
                             "clip size, max 1920x1080")
        with self._settings(SENDER_WIDTH=1280, SENDER_HEIGHT=720) as cfg:
            self.assertEqual(cfg.output_description(), "1280x720")

    # -- opting out ----------------------------------------------------------

    def test_zero_means_each_clip_at_its_own_size(self):
        with self._settings(SENDER_WIDTH=0, SENDER_HEIGHT=0) as cfg:
            self.assertEqual(cfg.canvas_for(1280, 720), (1280, 720))

    def test_the_idle_frame_still_has_a_size_with_no_canvas(self):
        with self._settings(SENDER_WIDTH=0, SENDER_HEIGHT=0) as cfg:
            self.assertEqual(
                (cfg.IDLE_SENDER_WIDTH, cfg.IDLE_SENDER_HEIGHT), (1920, 1080))

    def test_a_vertical_canvas_gives_vertical_preview_cells(self):
        with self._settings(SENDER_WIDTH=1080, SENDER_HEIGHT=1920) as cfg:
            self.assertLess(cfg.PREVIEW_ASPECT, 1.0,
                            "an LED wall taller than it is wide")

    def test_an_explicit_preview_aspect_still_wins(self):
        with self._settings(SENDER_WIDTH=1080, SENDER_HEIGHT=1920,
                            PREVIEW_ASPECT="1.7778") as cfg:
            self.assertAlmostEqual(cfg.PREVIEW_ASPECT, 1.7778, places=3)

    # -- refusing the impossible ---------------------------------------------

    def test_half_a_canvas_is_refused(self):
        # A width with no height does not describe anything, and inventing the
        # other number would publish at a size nobody asked for.
        with self._settings(SENDER_WIDTH=1920, SENDER_HEIGHT=0) as cfg:
            self.assertIsNotNone(cfg.canvas_problem())
            self.assertIn("together", cfg.canvas_problem())

    def test_half_a_canvas_falls_back_rather_than_guessing(self):
        with self._settings(SENDER_WIDTH=1920, SENDER_HEIGHT=0) as cfg:
            self.assertEqual(cfg.canvas_for(1280, 720), (1280, 720))

    def test_an_unknown_fit_mode_is_refused(self):
        with self._settings(SENDER_FIT="squish") as cfg:
            self.assertIsNotNone(cfg.canvas_problem())
            self.assertIn("squish", cfg.canvas_problem())

    def test_the_refusal_happens_before_the_window_opens(self):
        source = Path(app_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("canvas_problem()", source)
        body = source.split("def main(")[1]
        self.assertLess(body.index("canvas_problem()"),
                        body.index("from .ui import MainWindow"))

    def test_an_absurd_canvas_is_clamped_not_allocated(self):
        with self._settings(SENDER_WIDTH=100000, SENDER_HEIGHT=100000) as cfg:
            width, height = cfg.canvas_for(1280, 720)
            self.assertLessEqual(width, cfg.MAX_TEXTURE_DIM)
            self.assertLessEqual(height, cfg.MAX_TEXTURE_DIM)

    # -- how it is actually done ---------------------------------------------

    def test_the_render_target_is_the_canvas_not_the_video(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def render(")[1].split("\n    def ")[0]
        self.assertIn("canvas_for(self._vid_w, self._vid_h)", body)
        self.assertNotIn("self.target.ensure(self._vid_w", body)

    def test_it_waits_for_a_real_frame_before_allocating(self):
        # The target must stay unallocated until the clip has a size, or the
        # idle black frame has nothing to hand over from.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def render(")[1].split("\n    def ")[0]
        self.assertIn("if self._vid_w <= 0 or self._vid_h <= 0:", body)

    def test_the_fit_is_mpvs_own_scaling(self):
        # Not a second pass of ours. keepaspect and panscan are mpv defaults,
        # but they decide what a fixed target does with an odd-shaped clip, so
        # they are set rather than assumed.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("keepaspect=", source)
        self.assertIn("panscan=", source)

    def test_fill_crops_and_stretch_distorts(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        # Only with a fixed canvas: at the clip's own size there is nothing
        # to crop or stretch to.
        self.assertIn('panscan=(1.0 if SENDER_FIT == "fill" and canvas()[0] '
                      'else 0.0)', source)
        self.assertIn('keepaspect=(SENDER_FIT != "stretch") if canvas()[0]',
                      source)

    def test_the_status_line_names_both_sizes(self):
        # "1920x1080" on a clip that is actually 640x480 is the half-truth
        # that costs an hour when someone wonders why it looks soft.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _emit_size(")[1].split("\n    def ")[0]
        self.assertIn("in {out_w}x{out_h}", body)

    def test_the_status_bar_says_what_the_senders_publish_at(self):
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _update_senders_label(")[1].split(
            "\n    def ")[0]
        self.assertIn("output_description()", body)


class AspectRatioSafetyTests(unittest.TestCase):
    """A fixed canvas must never squash anything that is not 16:9.

    The worry is reasonable and the answer is specific: with SENDER_FIT=fit,
    mpv scales until the frame is INSIDE the canvas and fills the rest with the
    background. The scale factor is one number applied to both axes, so the
    aspect ratio cannot change - bars appear instead. Only "stretch" distorts,
    and it has to be asked for by name.

    These are stated as numbers rather than as a principle because "it should
    be fine" is what a squashed video on a projector is made of.
    """

    SHAPES = {
        "16:9 720p": (1280, 720),
        "16:9 4K": (3840, 2160),
        "4:3": (640, 480),
        "vertical phone": (1080, 1920),
        "square": (1080, 1080),
        "21:9 scope": (2560, 1080),
        "old DVD-ish": (1024, 576),
    }

    def _aspect(self, w, h):
        return round(w / h, 4)

    def test_fit_never_changes_the_shape_of_anything(self):
        for name, (w, h) in self.SHAPES.items():
            with self.subTest(name):
                out_w, out_h, kept = config.fit_report(w, h, 1920, 1080, "fit")
                self.assertAlmostEqual(self._aspect(out_w, out_h),
                                       self._aspect(w, h), places=2,
                                       msg=f"{name} came out a different shape")
                self.assertEqual(kept, 1.0, "fit must not crop")

    def test_fit_never_overflows_the_canvas(self):
        for name, (w, h) in self.SHAPES.items():
            with self.subTest(name):
                out_w, out_h, _ = config.fit_report(w, h, 1920, 1080, "fit")
                self.assertLessEqual(out_w, 1920)
                self.assertLessEqual(out_h, 1080)

    def test_fit_always_touches_one_pair_of_edges(self):
        # Otherwise it is not fitted, just small - bars on all four sides.
        for name, (w, h) in self.SHAPES.items():
            with self.subTest(name):
                out_w, out_h, _ = config.fit_report(w, h, 1920, 1080, "fit")
                self.assertTrue(out_w == 1920 or out_h == 1080,
                                f"{name} left a gap on every side")

    def test_sixteen_by_nine_gets_no_bars_at_any_size(self):
        for w, h in ((1280, 720), (3840, 2160), (640, 360)):
            with self.subTest(f"{w}x{h}"):
                self.assertEqual(config.fit_report(w, h, 1920, 1080, "fit"),
                                 (1920, 1080, 1.0))

    def test_the_bars_land_where_you_would_expect(self):
        # 4:3 pillarboxes, 21:9 letterboxes. Named because getting these two
        # the wrong way round is the classic sign the maths is inverted.
        self.assertEqual(config.fit_report(640, 480, 1920, 1080, "fit")[:2],
                         (1440, 1080))
        self.assertEqual(config.fit_report(2560, 1080, 1920, 1080, "fit")[:2],
                         (1920, 810))

    def test_only_stretch_fills_the_canvas_with_the_wrong_shape(self):
        out_w, out_h, _ = config.fit_report(640, 480, 1920, 1080, "stretch")
        self.assertEqual((out_w, out_h), (1920, 1080))
        self.assertNotAlmostEqual(self._aspect(out_w, out_h),
                                  self._aspect(640, 480), places=2)

    def test_the_default_is_the_one_that_cannot_distort(self):
        self.assertEqual(config.SENDER_FIT, "fit")

    def test_keepaspect_is_only_turned_off_for_stretch(self):
        # The single line that decides whether anything can ever be squashed.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        self.assertIn('keepaspect=(SENDER_FIT != "stretch") if canvas()[0] '
                      'else False', source)
        self.assertEqual(source.count("keepaspect"), 1,
                         "one place decides this, and it is that line")

    def test_a_vertical_canvas_is_just_as_safe(self):
        # Nothing about the maths assumes the canvas is landscape.
        for name, (w, h) in self.SHAPES.items():
            with self.subTest(name):
                out_w, out_h, _ = config.fit_report(w, h, 1080, 1920, "fit")
                self.assertAlmostEqual(self._aspect(out_w, out_h),
                                       self._aspect(w, h), places=2)

    def test_fill_is_the_one_that_costs_you_something(self):
        # Kept for contrast: this is what the default is protecting you from.
        self.assertAlmostEqual(
            config.fit_report(1080, 1920, 1920, 1080, "fill")[2], 0.3164,
            places=3)

    def test_a_heavy_crop_is_said_out_loud(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _warn_if_badly_cropped")[1].split(
            "\n    def ")[0]
        self.assertIn("SENDER_FIT=fit keeps all of it", body)

    def test_upscaling_is_not_a_new_loss(self):
        # A 480p clip on a 1080p canvas is soft - but it was going to be scaled
        # to the same canvas by Resolume either way. The canvas moves WHERE the
        # scaling happens, not whether it happens.
        out_w, out_h, kept = config.fit_report(640, 480, 1920, 1080, "fit")
        self.assertEqual(kept, 1.0)
        self.assertEqual((out_w, out_h), (1440, 1080))


class SenderBackgroundTests(unittest.TestCase):
    """What fills the bars. Black unless asked otherwise."""

    @contextlib.contextmanager
    def _settings(self, **settings):
        keys = {f"CHUUTUBE2SPOUT_{k}": str(v) for k, v in settings.items()}
        try:
            with mock.patch.dict(os.environ, keys, clear=False):
                yield importlib.reload(config)
        finally:
            importlib.reload(config)

    def test_black_is_the_default(self):
        self.assertEqual(config.SENDER_BACKGROUND, "black")
        self.assertEqual(config.background_rgba(), (0.0, 0.0, 0.0, 1.0))

    def test_transparent_is_alpha_zero(self):
        with self._settings(SENDER_BACKGROUND="transparent") as cfg:
            self.assertEqual(cfg.background_rgba()[3], 0.0)
            self.assertEqual(cfg.background_colour(), "")

    def test_a_colour_is_accepted(self):
        with self._settings(SENDER_BACKGROUND="#204060") as cfg:
            r, g, b, a = cfg.background_rgba()
            self.assertAlmostEqual(r, 0x20 / 255, places=4)
            self.assertAlmostEqual(g, 0x40 / 255, places=4)
            self.assertAlmostEqual(b, 0x60 / 255, places=4)
            self.assertEqual(a, 1.0)
            self.assertIsNone(cfg.canvas_problem())

    def test_nonsense_is_refused_at_startup(self):
        with self._settings(SENDER_BACKGROUND="puce") as cfg:
            self.assertIsNotNone(cfg.canvas_problem())
            self.assertIn("puce", cfg.canvas_problem())

    def test_a_short_hex_is_refused_rather_than_half_read(self):
        with self._settings(SENDER_BACKGROUND="#abc") as cfg:
            self.assertIsNotNone(cfg.canvas_problem())

    def test_our_own_clears_use_it_too(self):
        # mpv paints the bars, but the first frame after an allocation and the
        # blanking of an empty slot are both ours. A transparent canvas that
        # starts with one opaque frame is a flash on stage.
        # Since 0.9.9.27 the blanking uses empty_rgba() instead - an empty
        # slot is not bars - so the allocation is the one left.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        # The allocation, and since 0.9.9.38 the bars cleared after mpv draws.
        self.assertEqual(source.count("GL.glClearColor(*background_rgba())"), 2)
        self.assertEqual(source.count("GL.glClearColor(*empty_rgba())"), 1)
        self.assertNotIn("GL.glClearColor(0.0, 0.0, 0.0, 1.0)", source)

    def test_mpv_is_asked_in_more_than_one_spelling(self):
        # The option has been renamed across mpv versions; guessing one and
        # printing a failure for the others makes a good startup look broken.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _apply_background")[1].split("\n    def ")[0]
        self.assertIn('("background", "none")', body)
        self.assertIn('("alpha", "yes")', body)
        self.assertIn('"background-color"', body)

    def test_a_failure_leaves_black_rather_than_taking_the_app_down(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _try_set")[1].split("\n    def ")[0]
        self.assertIn("return False", body)
        self.assertNotIn("raise", body)


class RenderLoopCostTests(unittest.TestCase):
    """The loop runs on the GUI thread, so anything it does for no reason is
    felt as lag in the buttons and the preview."""

    def setUp(self):
        self.window = MainWindow()
        self.surface = self.window.surface
        self.surface.initializeGL()

    def _make_live(self, player, w=1920, h=1080):
        player.target.fbo, player.target.w, player.target.h = 9, w, h

    def test_the_idle_beat_stops_once_both_slots_have_content(self):
        # A slot that has played something keeps a valid target of its own -
        # clearing it renders black into that same FBO - so there is nothing
        # for the idle frame to do, and waking the loop twice a second for the
        # rest of the night is pure cost.
        for player in self.surface.players:
            self._make_live(player)
        self.surface._last_idle = 0.0
        self.assertFalse(self.surface._idle_due(time.monotonic()))

    def test_the_idle_beat_runs_while_a_slot_is_empty(self):
        self._make_live(self.surface.players[0])
        self.surface._last_idle = 0.0
        self.assertTrue(self.surface._idle_due(time.monotonic()),
                        "slot B still needs its name in Resolume")

    def test_an_idle_slot_does_not_beat_faster_than_the_interval(self):
        now = time.monotonic()
        self.surface._last_idle = now
        self.assertFalse(self.surface._idle_due(now))

    def test_a_quiet_tick_does_no_gl_work(self):
        for player in self.surface.players:
            self._make_live(player)
            player.render = lambda: False
        self.surface._last_idle = time.monotonic()
        self.surface._dirty = False
        self.surface._force_tick = False
        calls = []
        self.surface.makeCurrent = lambda: calls.append("makeCurrent")
        self.surface.tick()
        self.assertEqual(calls, [],
                         "the context was made current with nothing to draw")

    def test_timing_is_not_even_measured_when_it_is_off(self):
        # perf_counter twice per stage at 250 ticks a second is not free, and
        # a diagnostic that costs something when disabled is a bad diagnostic.
        self.assertFalse(self.surface._perf.on, "PERF_LOG_S defaults to off")
        source = Path(surface_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def tick(")[1].split("\n    def ")[0]
        self.assertIn("clock = time.perf_counter if self._perf.on else None",
                      body)

    def test_the_stages_are_timed_separately(self):
        # One fps number cannot say WHICH stage got slower, which is the whole
        # reason this exists.
        self.assertEqual(surface_mod.Perf.STAGES,
                         ("gl", "render", "spout", "paint"))

    def test_the_report_averages_over_frames_not_ticks(self):
        # A tick with nothing to do is a flag check. Averaging those in makes
        # every stage look fast while the app stutters.
        perf = surface_mod.Perf(1.0)
        perf._t0 -= 1.0                    # pretend the window has elapsed
        perf.ticks, perf.frames = 1000, 10
        perf.add("render", 1.0)
        printed = io.StringIO()
        with contextlib.redirect_stderr(printed):
            perf.report()
        self.assertIn("render 100.00ms", printed.getvalue())

    def test_reporting_resets_so_numbers_are_per_window(self):
        perf = surface_mod.Perf(1.0)
        perf._t0 -= 1.0
        perf.frames = 5
        perf.add("spout", 0.5)
        with contextlib.redirect_stderr(io.StringIO()):
            perf.report()
        self.assertEqual(perf.totals["spout"], 0.0)
        self.assertEqual(perf.frames, 0)

    def test_a_disabled_perf_never_prints(self):
        perf = surface_mod.Perf(0)
        perf._t0 -= 60.0
        printed = io.StringIO()
        with contextlib.redirect_stderr(printed):
            perf.report()
        self.assertEqual(printed.getvalue(), "")

    def test_the_report_says_what_size_it_was_working_at(self):
        # The cost of every stage is per pixel, so the sizes are the first
        # thing worth knowing when one of them is high.
        for player in self.surface.players:
            self._make_live(player, 1920, 1080)
        self.assertIn("1920x1080", self.surface._perf_note())


class RenderBlockingTests(unittest.TestCase):
    """mpv's render call must not sleep waiting for a frame's display time.

    libmpv defaults MPV_RENDER_PARAM_BLOCK_FOR_TARGET_TIME on, which is right
    for a video player - the render call is how a player paces itself to the
    monitor. This is not a player. It renders off a timer into an FBO and hands
    the texture to Spout, and Resolume has its own clock. Left on, the GUI
    thread sits inside mpv for most of every frame interval.

    Measured before it was changed: 37.5ms per frame in render, 97% of the GUI
    thread, against two clips that are a few milliseconds of real GPU work.
    37.5ms is not work - it is the 25fps clip's frame interval.
    """

    def test_the_render_call_is_told_not_to_block(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def render(")[1].split("\n    def ")[0]
        self.assertIn("block_for_target_time=MPV_BLOCK_FOR_TARGET_TIME", body)

    def test_the_default_is_not_to_block(self):
        self.assertFalse(config.MPV_BLOCK_FOR_TARGET_TIME,
                         "on by default is libmpv's choice, not ours")

    def test_python_mpv_actually_takes_that_parameter(self):
        # It is passed through **kwargs, so a typo would be silently accepted
        # by our code and rejected deep inside python-mpv at render time -
        # which is once per frame, on the GUI thread.
        try:
            import mpv as mpv_mod
        except Exception:
            self.skipTest("python-mpv not importable here")
        if not hasattr(mpv_mod, "MpvRenderParam"):
            self.skipTest("stubbed python-mpv")
        self.assertIn("block_for_target_time", mpv_mod.MpvRenderParam.TYPES)

    def test_it_is_still_reachable_for_a_comparison(self):
        # Kept as a setting so the old behaviour can be turned back on and
        # measured rather than remembered.
        source = Path(config.__file__).read_text(encoding="utf-8")
        self.assertIn('_flag("MPV_BLOCK_FOR_TARGET_TIME", False)', source)


class PerfPerSlotTests(unittest.TestCase):
    """"render is slow" and "slot B is slow" are different faults.

    Both slots are rendered in the same tick, so a single render total hides
    which one is expensive - and with blocking on, the slower clip set the pace
    for both.
    """

    def setUp(self):
        self.window = MainWindow()
        self.surface = self.window.surface
        self.surface.initializeGL()

    def test_each_slot_is_timed_on_its_own(self):
        perf = surface_mod.Perf(1.0)
        perf.add_slot(0, 0.010)
        perf.add_slot(1, 0.030)
        self.assertEqual(perf.slots, [0.010, 0.030])

    def test_the_slot_times_add_up_to_the_render_stage(self):
        perf = surface_mod.Perf(1.0)
        perf.add_slot(0, 0.010)
        perf.add_slot(1, 0.030)
        self.assertAlmostEqual(perf.totals["render"], 0.040, places=6)

    def test_the_note_names_the_slots(self):
        self.surface._perf.frames = 1
        self.surface._perf.add_slot(0, 0.005)
        self.surface._perf.add_slot(1, 0.015)
        note = self.surface._perf_note()
        self.assertIn("A ", note)
        self.assertIn("B ", note)
        self.assertIn("5.0ms", note)
        self.assertIn("15.0ms", note)

    def test_the_note_survives_an_empty_window(self):
        # report() can fire before any frame has been drawn.
        self.surface._perf.frames = 0
        self.surface._perf_note()     # must not divide by zero


class ReleaseGuardTests(unittest.TestCase):
    """Things the release process must catch rather than ask a human to."""

    @staticmethod
    def _build() -> str:
        return (Path(__file__).resolve().parent.parent
                / "build.ps1").read_text(encoding="utf-8")

    @staticmethod
    def _setup() -> str:
        return (Path(__file__).resolve().parent.parent
                / "setup.ps1").read_text(encoding="utf-8")

    def test_the_runtime_cannot_be_published_by_accident(self):
        # Not shipping libmpv, deno and the token generator is the whole basis
        # of THIRD-PARTY-NOTICES.md. A -Portable run or a first-run install
        # done from dist\ puts them there.
        build = self._build()
        for name in ("mpv-2.dll", "libmpv-2.dll", "deno.exe",
                     "bgutil-pot.exe", "bgutil-server"):
            with self.subTest(name):
                self.assertIn(f'Path = "{name}"', build)

    def test_personal_files_cannot_be_published_by_accident(self):
        build = self._build()
        for name in ("cache", "logs"):
            with self.subTest(name):
                self.assertIn(f'Path = "{name}"', build)
        # settings.txt is shipped since 0.9.9.30, so it is not on the forbidden
        # list - instead the shipped one has to hash-match the template.
        self.assertIn("settings.txt - not the template", build)

    def test_the_guard_stops_the_build_rather_than_warning(self):
        # A warning about the contents of a zip is read on the first build and
        # skimmed on the tenth, and the tenth is the one with something in it.
        build = self._build()
        body = build.split("$forbidden = @(")[1].split("# ---")[0]
        self.assertIn("Die ", body)

    def test_the_clean_machine_test_names_the_first_run_path(self):
        # The exe's first run differs from run.bat, which is how the
        # -RuntimeOnly crash reached a build in the first place.
        build = self._build()
        self.assertIn("FIRST RUN WITH NO RUNTIME", build)

    def test_a_hand_fetched_licence_is_not_reported_as_missing(self):
        # PyOpenGL and the bgutil plugin ship no licence text in their wheels,
        # so both were fetched by hand. An inventory that keeps calling them
        # missing contradicts the folder it describes, and the publish step
        # that says "fetch those by hand" can never be finished.
        setup = self._setup()
        body = setup.split("$missing += ")[0].rsplit("} else {", 1)[1]
        self.assertIn('"$($info.Name).txt"', body)
        self.assertIn("Test-Path (Join-Path $licDir $candidate)", body)

    def test_a_hand_fetched_licence_still_says_so(self):
        # "the wheel shipped this" and "a person fetched this" are different
        # claims, and a generated inventory that blurs them is worth less than
        # one that does not exist.
        self.assertIn("(fetched by hand)", self._setup())


class FaulthandlerSafetyTests(unittest.TestCase):
    """faulthandler must only ever read the stack of the thread that faulted.

    From a tester's log (0.9.9.11): libmpv's LuaJIT raised and caught its own
    errors as Windows SEH exceptions (0xE24C4A02). faulthandler reported each
    as "Windows fatal exception" and dumped every Python thread without the
    GIL while they kept running. One dump reached the mpv event thread in the
    middle of _on_time_pos, printed "File ???", and the process died of an
    access violation inside faulthandler itself.
    """

    def test_it_is_enabled_for_the_current_thread_only(self):
        seen = {}
        fake = types.SimpleNamespace(enable=lambda **kw: seen.update(kw))
        with mock.patch.dict(sys.modules, {"faulthandler": fake}):
            logfile._enable_faulthandler(io.StringIO())
        self.assertIs(seen.get("all_threads"), False,
                      "all_threads=True walks running threads' frames")

    def test_all_threads_true_is_nowhere_in_the_code(self):
        source = Path(logfile.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("all_threads=True", code)

    def test_a_missing_faulthandler_is_not_fatal(self):
        with mock.patch.dict(sys.modules, {"faulthandler": None}):
            logfile._enable_faulthandler(io.StringIO())    # must not raise


class LogFlushTimingTests(unittest.TestCase):
    """The tail of the log reaches the disk even when nothing follows it.

    0.9.9.9 flushed "when a line is written and 250ms have passed", which only
    ever runs when ANOTHER line arrives. Slot B's decoder lines were written,
    nothing followed, and the crash seconds later took them - so the log that
    could have said why B was black could not.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="chuutube-flush-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "chuutube-20260929-000000-1.log"
        self.handle = open(self.path, "w", encoding="utf-8")
        self.addCleanup(self.handle.close)

    def _on_disk(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def test_a_quiet_line_is_not_flushed_by_the_write_itself(self):
        # The premise: nothing in write() puts an ordinary line on disk.
        tee = logfile.Tee(None, self.handle)
        tee.write("[mpv:B] info vd: Using hardware decoding (nvdec).\n")
        self.assertNotIn("nvdec", self._on_disk())

    def test_one_flusher_pass_puts_it_on_disk_with_nothing_after_it(self):
        tee = logfile.Tee(None, self.handle)
        tee.write("[mpv:B] info cplayer: VO: [libmpv] 1920x1080\n")
        logfile._flush_once(self.handle)
        self.assertIn("VO: [libmpv] 1920x1080", self._on_disk())

    def test_the_flusher_runs_on_its_own_thread(self):
        source = Path(logfile.__file__).read_text(encoding="utf-8")
        body = source.split("def start(")[1].split("\ndef ")[0]
        self.assertIn("target=_flusher", body)
        self.assertIn("daemon=True", body)

    def test_a_fragment_is_left_for_its_newline(self):
        # The flusher pushes complete lines only; redaction must still always
        # see a whole line, not half a URL.
        tee = logfile.Tee(None, self.handle)
        tee.write("https://x.googlevideo.com/videoplayback?pot=SEC")
        logfile._flush_once(self.handle)
        self.assertNotIn("SEC", self._on_disk())

    def test_concurrent_writers_lose_nothing(self):
        # Both Tees write into one file from the GUI thread, both mpv event
        # threads and every yt-dlp reader at once. Each Tee also keeps its own
        # fragment buffer, which is where an unlocked read-modify-write loses
        # text: `self._buffer += text` and the rpartition that follows are two
        # steps, and a switch between them drops the other thread's append.
        #
        # Under the GIL that switch is rare at the default interval, and this
        # test passed with the lock removed until the interval was forced down
        # - a concurrency test that cannot fail is not a test.
        saved = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, saved)
        tee = logfile.Tee(None, self.handle)

        def spam(tag):
            for i in range(400):
                # Split each line across two writes, as print() does.
                tee.write(f"{tag}-{i}")
                tee.write("\n")

        threads = [threading.Thread(target=spam, args=(str(n),))
                   for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        tee.flush()
        lines = [l for l in self._on_disk().splitlines() if l]
        expected = {f"{n}-{i}" for n in range(8) for i in range(400)}
        self.assertEqual(set(lines), expected,
                         "lines were dropped or merged under contention")


class BuiltinLuaScriptTests(unittest.TestCase):
    """mpv's built-in Lua scripts are switched off - the ones this mpv has."""

    class FakeMpv:
        """Just the three raw functions the probe uses."""

        def __init__(self, known):
            self.known = set(known)
            self.created = 0
            self.destroyed = 0
            self.initialised = 0

        def _mpv_create(self):
            self.created += 1
            return object()

        def _mpv_set_option_string(self, handle, name, value):
            if name.decode() not in self.known:
                raise AttributeError(f"option not found: {name.decode()}")

        def _mpv_terminate_destroy(self, handle):
            self.destroyed += 1

        def _mpv_initialize(self, handle):
            self.initialised += 1

    def setUp(self):
        self.saved = (player_mod.mpv, player_mod._script_options)
        player_mod._script_options = None

    def tearDown(self):
        player_mod.mpv, player_mod._script_options = self.saved

    def _probe(self, known):
        fake = self.FakeMpv(known)
        player_mod.mpv = fake
        with contextlib.redirect_stderr(io.StringIO()):
            return fake, player_mod.builtin_scripts_off()

    def test_only_the_names_this_build_knows_are_used(self):
        # An unknown name raises out of python-mpv's constructor AFTER the core
        # is initialised. Passing all of them and hoping is not an option.
        _, found = self._probe({"load-stats-overlay", "load-osd-console"})
        self.assertEqual(found, {"load-stats-overlay": "no",
                                 "load-osd-console": "no"})

    def test_the_probe_handle_is_never_initialised(self):
        # Initialising it would load the very scripts being switched off.
        fake, _ = self._probe({"load-stats-overlay"})
        self.assertEqual(fake.initialised, 0)

    def test_the_probe_handle_is_always_destroyed(self):
        fake, _ = self._probe(set())
        self.assertEqual((fake.created, fake.destroyed), (1, 1))

    def test_it_is_probed_once_per_process(self):
        fake, _ = self._probe({"load-select"})
        with contextlib.redirect_stderr(io.StringIO()):
            player_mod.builtin_scripts_off()
        self.assertEqual(fake.created, 1)

    def test_no_raw_functions_means_nothing_is_switched_off(self):
        player_mod.mpv = types.SimpleNamespace()
        self.assertEqual(player_mod.builtin_scripts_off(), {})

    def test_every_script_this_app_does_not_use_is_on_the_list(self):
        for name in ("load-stats-overlay", "load-osd-console", "load-select",
                     "load-commands", "load-context-menu"):
            with self.subTest(name):
                self.assertIn(name, player_mod.BUILTIN_SCRIPT_OPTIONS)

    def test_they_reach_the_constructor(self):
        # Scripts load during mpv_initialize, which python-mpv calls inside
        # MPV(). Set afterwards they would change nothing.
        player_mod._script_options = {"load-stats-overlay": "no"}
        player = Player("T")
        self.assertEqual(player.mpv.options.get("load_stats_overlay"), "no")

    def test_osc_and_the_ytdl_hook_stay_off_by_name(self):
        player_mod._script_options = {}
        player = Player("T")
        self.assertIs(player.mpv.options.get("osc"), False)
        self.assertIs(player.mpv.options.get("ytdl"), False)


class FrozenChildEncodingTests(unittest.TestCase):
    """yt-dlp speaks UTF-8 on the pipe in the release build too.

    From a tester's 0.9.9.11 log: the frozen exe re-launched as yt-dlp ran in
    cp1252 ("pref cp1252, out cp1252") despite PYTHONUTF8 in its environment,
    and "\u30a2\u30b6\u30df\u30ca\uff1aRobocosan Original Song" came through as "Robocosan
    Original Song" - yt-dlp encodes with errors='ignore'.
    """

    def _cmd(self, builder):
        cache = Path(tempfile.gettempdir())
        if builder is downloader._command:
            return builder(["yt-dlp"], "https://example/watch", "b", cache,
                           cache / "p.txt", [], cookies=False)
        return builder(["yt-dlp"], cache / "i.json", "b", cache,
                       cache / "p.txt")

    def test_both_command_builders_force_utf8(self):
        for builder in (downloader._command, downloader._info_command):
            with self.subTest(builder.__name__):
                cmd = self._cmd(builder)
                i = cmd.index("--encoding")
                self.assertEqual(cmd[i + 1], "utf-8")

    def test_it_does_not_depend_on_the_environment(self):
        # The flag is on the command line, where PyInstaller cannot drop it.
        self.assertEqual(downloader.FORCE_UTF8, ("--encoding", "utf-8"))

    def test_the_frozen_child_reconfigures_its_own_streams_first(self):
        source = Path(app_mod.__file__).read_text(encoding="utf-8")
        body = source.split("if len(argv) > 1 and argv[1] == YTDLP_FLAG:")[1]
        body = body.split("return ytdlp_main")[0]
        self.assertIn('reconfigure(encoding="utf-8"', body)
        self.assertLess(body.index("reconfigure"),
                        body.index("from yt_dlp import main"))


class RedactionVersionPathTests(unittest.TestCase):
    """A four-part version after a URL-encoded space is not an IP address."""

    def test_the_testers_path_survives(self):
        line = ("file:///Y:/VJ/TOOLS/ChuuTube2Spout/ChuuTube2Spout%200.9.9.11"
                "/bgutil-server/x/server/src/main.ts:4:21")
        self.assertEqual(redact.scrub(line), line)

    def test_a_real_address_is_still_caught(self):
        self.assertNotIn("203.0.113.9",
                         redact.scrub("seen as 203.0.113.9 by YouTube"))

    def test_an_address_in_a_query_is_still_caught(self):
        self.assertNotIn("203.0.113.9",
                         redact.scrub("videoplayback?ip=203.0.113.9&x=1"))


class TokenServerDriveTests(unittest.TestCase):
    """The generator gets built somewhere it CAN be built.

    From a tester's log: the app lived on Y:, deno's node_modules is a farm of
    NTFS junctions, and the drive could not make one - "failed to create
    junction ... Incorrect function. (os error 1)". Script mode and the deno
    server both need that tree, so there was no token generator at all.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="chuutube-pot-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app = self.dir / "app"
        self.local = self.dir / "local" / "ChuuTube2Spout" / "bgutil-server"
        self.saved = {name: getattr(potprovider, name) for name in (
            "project_root", "local_server_root", "script_probe",
            "install_script_deps")}
        potprovider.project_root = lambda: self.app
        potprovider.local_server_root = lambda: self.local
        self.provider = potprovider.PotProvider()
        self.provider._start_server = lambda: False
        self.provider._after_ready = lambda: None

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(potprovider, name, value)

    def _tree(self, base: Path, release="bgutil-ytdlp-pot-provider-2.0.0",
              built=False, ready=False) -> Path:
        server = base / release / "server"
        (server / "src").mkdir(parents=True, exist_ok=True)
        (server / "src" / "main.ts").write_text("// generator")
        (server / "package.json").write_text("{}")
        if built or ready:
            (server / "node_modules").mkdir(exist_ok=True)
        if ready:
            (server / "node_modules" / potprovider.READY_MARKER).write_text("ok")
        return server

    # --- which tree -------------------------------------------------------
    def test_a_proven_tree_beats_an_unproven_one_wherever_it_is(self):
        self._tree(self.app / "bgutil-server", built=True)     # half-built
        local = self._tree(self.local, ready=True)
        self.assertEqual(potprovider.find_script_home(), str(local))

    def test_node_modules_alone_is_not_proof(self):
        # A deno install that dies half way leaves one behind.
        server = self._tree(self.app / "bgutil-server", built=True)
        self.assertFalse(potprovider.is_ready(server))

    def test_with_nothing_proven_the_app_folder_comes_first(self):
        app = self._tree(self.app / "bgutil-server")
        self._tree(self.local)
        self.assertEqual(potprovider.find_script_home(), str(app))

    def test_no_localappdata_is_not_an_error(self):
        potprovider.local_server_root = lambda: None
        app = self._tree(self.app / "bgutil-server")
        self.assertEqual(potprovider.find_script_home(), str(app))

    # --- relocation -------------------------------------------------------
    def test_relocation_copies_the_source_and_not_the_broken_modules(self):
        server = self._tree(self.app / "bgutil-server", built=True)
        (server / "node_modules" / "half-built").write_text("x")
        target = Path(potprovider.relocate_script_home(str(server)))
        self.assertTrue((target / "src" / "main.ts").is_file())
        self.assertTrue((target / "package.json").is_file())
        self.assertFalse((target / "node_modules").exists())

    def test_relocation_keeps_the_release_name(self):
        # So an app update with a new generator release gets a new folder
        # rather than being handed the old one.
        server = self._tree(self.app / "bgutil-server",
                            release="bgutil-ytdlp-pot-provider-2.1.0")
        target = potprovider.relocate_script_home(str(server))
        self.assertIn("bgutil-ytdlp-pot-provider-2.1.0", target)

    def test_a_tree_already_local_is_not_relocated_onto_itself(self):
        local = self._tree(self.local)
        self.assertIsNone(potprovider.relocate_script_home(str(local)))

    def test_no_localappdata_means_no_relocation(self):
        potprovider.local_server_root = lambda: None
        server = self._tree(self.app / "bgutil-server")
        self.assertIsNone(potprovider.relocate_script_home(str(server)))

    # --- the whole repair -------------------------------------------------
    def _run_prepare(self, install_results, probe_ok_at=None):
        installs = []
        results = iter(install_results)

        def install(home):
            installs.append(home)
            ok, detail = next(results)
            if ok:
                (Path(home) / "node_modules").mkdir(exist_ok=True)
            return ok, detail

        def probe(home):
            if probe_ok_at is not None and Path(home).is_relative_to(probe_ok_at):
                return True, "ok"
            return False, 'Could not resolve "express"'

        potprovider.install_script_deps = install
        potprovider.script_probe = probe
        out = io.StringIO()
        server = self._tree(self.app / "bgutil-server")
        with contextlib.redirect_stderr(out):
            self.provider._prepare_script(str(server))
        return server, installs, out.getvalue()

    def test_a_drive_without_junctions_is_worked_around(self):
        junction = ("error: failed to create junction 'Y:\\a' to 'Y:\\b': "
                    "Incorrect function. (os error 1)")
        server, installs, out = self._run_prepare(
            [(False, junction), (True, "")], probe_ok_at=self.local)
        self.assertEqual(len(installs), 2, "should retry once, elsewhere")
        self.assertTrue(Path(installs[1]).is_relative_to(self.local))
        self.assertTrue(self.provider._script_ok)
        self.assertTrue(Path(self.provider._script_home)
                        .is_relative_to(self.local))
        # What was observed, and where the tree went instead.
        self.assertIn("Incorrect function", out)
        self.assertIn(str(self.local), out)

    def test_the_repaired_tree_is_marked_so_the_next_launch_goes_straight_there(self):
        junction = "failed to create junction: Incorrect function. (os error 1)"
        self._run_prepare([(False, junction), (True, "")],
                          probe_ok_at=self.local)
        self.assertTrue(potprovider.is_ready(self.provider._script_home))
        self.assertEqual(potprovider.find_script_home(),
                         self.provider._script_home)

    def test_other_install_failures_are_not_relocated(self):
        # No network is no network on C: as well; do not spend another minute.
        _, installs, _ = self._run_prepare([(False, "error: no network")])
        self.assertEqual(len(installs), 1)

    def test_a_working_tree_is_marked_ready(self):
        potprovider.script_probe = lambda home: (True, "ok")
        server = self._tree(self.app / "bgutil-server", built=True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.provider._prepare_script(str(server))
        self.assertTrue(potprovider.is_ready(server))

    def test_success_goes_on_to_start_the_server(self):
        # Otherwise the whole first session after an unzip ran in per-request
        # script mode, because start() tried the server before it was built.
        called = []
        self.provider._after_ready = lambda: called.append(True)
        potprovider.script_probe = lambda home: (True, "ok")
        server = self._tree(self.app / "bgutil-server", built=True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.provider._prepare_script(str(server))
        self.assertEqual(called, [True])

    def test_the_real_after_ready_only_starts_a_server_when_asked_to(self):
        started = []
        self.provider._start_server = lambda: started.append(True)
        with mock.patch.object(potprovider, "TOKEN_SERVER", False):
            _REAL_AFTER_READY(self.provider)
        self.assertEqual(started, [])
        with mock.patch.object(potprovider, "TOKEN_SERVER", True):
            _REAL_AFTER_READY(self.provider)
        self.assertEqual(started, [True])

    def test_it_no_longer_claims_a_fallback_that_does_not_exist(self):
        source = Path(potprovider.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("falling back to the standalone token server", code)

    # --- the server never runs an unbuilt tree ----------------------------
    def test_an_unbuilt_tree_is_not_started_as_a_server(self):
        self._tree(self.app / "bgutil-server")
        provider = potprovider.PotProvider()
        # deno must LOOK present, or script_server_command returns None for
        # want of it and this passes without testing the readiness check.
        with mock.patch.object(potprovider.shutil, "which", lambda n: "deno"), \
             mock.patch.object(potprovider, "port_is_open", lambda *a, **k: False), \
             mock.patch.object(potprovider, "find_executable", lambda: None), \
             mock.patch.object(potprovider.subprocess, "Popen") as popen:
            started = provider._start_server()
        self.assertFalse(started)
        popen.assert_not_called()

    def test_first_run_is_quiet_about_a_tree_that_is_merely_unbuilt(self):
        # "no token generator found" on a machine whose generator is sitting
        # right there, about to be built, is the wrong thing to print.
        self._tree(self.app / "bgutil-server")
        provider = potprovider.PotProvider()
        out = io.StringIO()
        # deno must LOOK present, or script_server_command returns None for
        # want of it and this passes without testing the readiness check.
        with mock.patch.object(potprovider.shutil, "which", lambda n: "deno"), \
             mock.patch.object(potprovider, "port_is_open", lambda *a, **k: False), \
             mock.patch.object(potprovider, "find_executable", lambda: None), \
             contextlib.redirect_stderr(out):
            provider._start_server()
        self.assertEqual(out.getvalue(), "")
        self.assertIn("not prepared yet", provider.status)

    def test_a_tree_that_could_not_be_built_says_so_accurately(self):
        self._tree(self.app / "bgutil-server")
        provider = potprovider.PotProvider()
        provider._script_ok = False
        out = io.StringIO()
        with mock.patch.object(potprovider, "port_is_open", lambda *a, **k: False), \
             mock.patch.object(potprovider, "find_executable", lambda: None), \
             contextlib.redirect_stderr(out):
            provider._start_server()
        self.assertIn("could not be built", out.getvalue())
        self.assertNotIn("Re-run", out.getvalue(),
                         "re-running setup does not fix a drive")


class SetupDenoInstallTests(unittest.TestCase):
    """setup.ps1 must not report a half-built generator as installed."""

    @staticmethod
    def _block() -> str:
        source = (Path(__file__).resolve().parent.parent
                  / "setup.ps1").read_text(encoding="utf-8")
        return source.split("Installing the generator's dependencies")[1] \
                     .split("# ---")[0]

    def test_success_is_the_exit_code_not_the_folder(self):
        # deno creates node_modules before it fails half way through, so the
        # folder existing said "installed" on a drive where nothing resolved.
        block = self._block()
        self.assertIn("if ($denoExit -eq 0) {", block)
        self.assertNotIn('if (Test-Path (Join-Path $serverDir "node_modules")) {',
                         block)

    def test_a_drive_that_cannot_link_is_explained_not_retried(self):
        block = self._block()
        self.assertIn("-not $linkFailure", block)
        self.assertIn("Incorrect function", block)
        self.assertIn("LOCALAPPDATA", block)

    def test_setup_and_the_app_agree_on_what_a_link_failure_looks_like(self):
        # Two lists of the same words are how one of them drifts.
        for word in potprovider.LINK_FAILURE:
            with self.subTest(word):
                self.assertIn(word, self._block().lower())


class JoinedPrefetchPartialTests(unittest.TestCase):
    """Pressing Load during a prefetch still starts on the half-written file.

    Pasting a URL starts a prefetch 0.9s later, so pressing Load at ordinary
    human speed joins one - and a joined prefetch waited for its last byte,
    because only the load path had ever been given partial playback. It was
    reported as partial playback having stopped working: it had only ever
    worked for whoever clicked Load inside the debounce.
    """

    # --- the job ------------------------------------------------------------
    def test_an_unjoined_prefetch_declines(self):
        job = player_mod.PrefetchJob("u", 1080)
        self.assertIs(job.partial("C:/cache/half.mp4"), False)

    def test_once_joined_the_load_gets_the_file(self):
        job = player_mod.PrefetchJob("u", 1080)
        seen = []
        job.attach(seen.append, lambda: None)
        self.assertIs(job.partial("C:/cache/half.mp4"), True)
        self.assertEqual(seen, ["C:/cache/half.mp4"])

    def test_a_failed_attempt_reaches_the_joined_load(self):
        # Or mpv keeps the abandoned file open while the retry writes into it.
        job = player_mod.PrefetchJob("u", 1080)
        released = []
        job.attach(lambda p: None, lambda: released.append(True))
        job.partial_lost()
        self.assertEqual(released, [True])

    def test_losing_a_file_nobody_had_is_harmless(self):
        player_mod.PrefetchJob("u", 1080).partial_lost()      # must not raise

    def test_the_prefetch_offers_the_file_at_all(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def prefetch(self, url: str)")[1].split(
            "\n    def ")[0]
        self.assertIn("on_partial=job.partial if PARTIAL_PLAY else None", body)
        self.assertIn("on_partial_lost=job.partial_lost", body)

    def test_joining_is_said_in_the_log(self):
        # With how long the prefetch had been running - the one number that
        # says whether Load was pressed after the 0.9s debounce.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def load(self, url: str")[1].split(
            "\n    def ")[0]
        self.assertIn("Load joined the background download", body)
        self.assertIn("time.monotonic() - warm.started", body)

    def test_a_job_knows_when_it_started(self):
        before = time.monotonic()
        job = player_mod.PrefetchJob("u", 1080)
        self.assertGreaterEqual(job.started, before)

    def test_load_joins_with_its_own_callbacks_before_waiting(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def load(self, url: str")[1].split(
            "\n    def ")[0]
        self.assertIn("warm.attach(early if PARTIAL_PLAY else None", body)
        self.assertIn("gave_up if PARTIAL_PLAY else None, say)", body)
        self.assertLess(body.index("warm.attach("),
                        body.index("clip = warm.result()"))

    def test_progress_reaches_a_joined_load(self):
        job = player_mod.PrefetchJob("u", 1080)
        seen = []
        job.progress("downloading 10% of 1MiB")        # nobody yet: dropped
        job.attach(None, None, seen.append)
        job.progress("downloading 20% of 1MiB")
        self.assertEqual(seen, ["downloading 20% of 1MiB"])

    def test_the_prefetch_reports_progress_at_all(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def prefetch(self, url: str)")[1].split(
            "\n    def ")[0]
        self.assertIn("progress=job.progress", body)

    # --- the downloader asks again until someone takes it -------------------
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="chuutube-join-")
        self.addCleanup(shutil.rmtree, self.cache, True)
        fake = Path(__file__).resolve().parent / "fake_partial_ytdlp.py"
        saved = (downloader.ytdlp_command, downloader._worth_handing_over)
        downloader.ytdlp_command = lambda *a, **k: [sys.executable, str(fake)]
        # Every line after the Destination qualifies - the threshold has its
        # own tests; this is about what happens once it is met.
        downloader._worth_handing_over = lambda *a, **k: True

        def restore():
            downloader.ytdlp_command, downloader._worth_handing_over = saved
        self.addCleanup(restore)

    def _run(self, answers):
        offers = []
        answers = iter(answers)

        def hook(path):
            offers.append(path)
            return next(answers, True)

        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            downloader.download("https://example/watch", 1080, self.cache,
                                on_partial=hook)
        return offers, out.getvalue()

    def test_a_declined_file_is_offered_again_until_taken(self):
        # The Load joins some way into the prefetch; the next progress line
        # has to hand it over, not the one that already went by.
        offers, _ = self._run([False, False, True])
        self.assertEqual(len(offers), 3, "asked until taken, then not again")

    def test_playing_is_only_announced_when_something_is_playing(self):
        _, out = self._run([False, False, True])
        self.assertEqual(out.count("playing while it downloads"), 1)

    def test_an_unwatched_prefetch_never_claims_to_be_playing(self):
        offers, out = self._run([False] * 50)
        self.assertGreater(len(offers), 1)
        self.assertNotIn("playing while it downloads", out)

    def test_an_old_style_hook_is_taken_first_time(self):
        # A load's own hook returns None. Nothing about that path changes.
        offers, out = self._run([None])
        self.assertEqual(len(offers), 1)
        self.assertEqual(out.count("playing while it downloads"), 1)


class FfmpegLogAttributionTests(unittest.TestCase):
    """ffmpeg's lines cannot be pinned on a slot, so they are pinned on none.

    libmpv gives ffmpeg's process-wide log callback to the first mpv instance,
    so slot B's "Invalid NAL unit size ... partial file" arrived as [mpv:A],
    and - being an error - went into A's status line.
    """

    def _player(self, name):
        p = player_mod.Player.__new__(player_mod.Player)
        # What Player.__init__ sets for the status line; the stub base
        # class would answer anything missing with a truthy placeholder.
        p._headline, p._fetch_note, p._size_note = "idle", "", ""
        p.name = name
        p._recent_log = collections.deque(maxlen=4)
        p._repeats = 0
        p._recent_ffmpeg = collections.deque(maxlen=4)
        p._ffmpeg_repeats = 0
        p.statuses = []
        p.statusChanged = types.SimpleNamespace(emit=p.statuses.append)
        return p

    def _log(self, p, level, component, message):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            p._on_mpv_log(level, component, message)
        return out.getvalue()

    def test_an_ffmpeg_line_is_not_labelled_as_either_slot(self):
        out = self._log(self._player("A"), "error", "ffmpeg",
                        "NULL: Invalid NAL unit size (26037 > 16818).")
        self.assertTrue(out.startswith("[ffmpeg] "), out)
        self.assertNotIn("[mpv:A]", out)

    def test_an_ffmpeg_error_stays_out_of_the_status_line(self):
        p = self._player("A")
        self._log(p, "error", "ffmpeg/demuxer",
                  "mov,mp4,m4a,3gp,3g2,mj2: stream 0, offset 0x55c6d2a: "
                  "partial file")
        self.assertEqual(p.statuses, [])

    def test_mpvs_own_errors_still_reach_their_slot(self):
        p = self._player("B")
        out = self._log(p, "error", "cplayer", "Failed to open file.")
        self.assertTrue(out.startswith("[mpv:B] "), out)
        self.assertEqual(p.statuses, ["Failed to open file."])


class StatusLineTests(unittest.TestCase):
    """The status line says what the slot is doing, and keeps saying it.

    It used to be whatever wrote to it last. mpv reports the video size about
    100ms after every load, so "playing while it downloads" showed for a split
    second before becoming "1920x1080" - and so did every clip's title.
    """

    def _player(self, token=1):
        p = player_mod.Player.__new__(player_mod.Player)
        p._headline, p._fetch_note, p._size_note = "idle", "", ""
        p._load_token = token
        p._destroyed = False
        p._partial_token = None
        p._vid_w = p._vid_h = 0
        p.statuses = []
        p.statusChanged = types.SimpleNamespace(emit=p.statuses.append)
        p.sizeChanged = types.SimpleNamespace(emit=lambda *a: None)
        return p

    def _size(self, p, w, h):
        p._vid_w, p._vid_h = w, h
        with contextlib.redirect_stderr(io.StringIO()):
            p._emit_size()

    def test_the_size_does_not_replace_what_is_playing(self):
        # The screenshots: "playing while it downloads", then "1920x1080".
        p = self._player()
        p._set_status("playing while it downloads")
        self._size(p, 1920, 1080)
        self.assertEqual(p.statuses[-1], "playing while it downloads")

    def test_the_title_survives_the_size_arriving(self):
        p = self._player()
        p._set_status("1080p avc1 - GENER8ION - STORM")
        self._size(p, 1920, 1080)
        self.assertEqual(p.statuses[-1], "1080p avc1 - GENER8ION - STORM")

    def test_a_size_worth_knowing_is_appended(self):
        # 608x1080 letterboxed into the canvas is the case the size is for.
        p = self._player()
        p._set_status("1080p vp9 - WHO IS GRAHAM")
        with mock.patch.object(player_mod, "canvas_for",
                               lambda w, h: (1920, 1080)):
            self._size(p, 608, 1080)
        self.assertIn("WHO IS GRAHAM", p.statuses[-1])
        self.assertIn("608x1080 in 1920x1080", p.statuses[-1])

    def test_at_the_clips_own_size_nothing_is_appended(self):
        # The default since 0.9.9.38: the clip is sent as it is.
        p = self._player()
        p._set_status("1080p vp9 - WHO IS GRAHAM")
        self._size(p, 608, 1080)
        self.assertNotIn(" in ", p.statuses[-1].split("GRAHAM")[-1])

    def test_progress_is_a_note_while_the_half_file_plays(self):
        p = self._player(token=4)
        p._partial_token = 4
        p._set_status("playing while it downloads")
        p._on_progress(4, "downloading 45% of 93.50MiB at 12.00MiB/s")
        self.assertEqual(p.statuses[-1], "playing while it downloads "
                         "· 45% of 93.50MiB at 12.00MiB/s")

    def test_progress_is_the_whole_story_before_anything_plays(self):
        p = self._player(token=4)
        p._on_progress(4, "downloading 12% of 93.50MiB at 12.00MiB/s")
        self.assertEqual(p.statuses[-1],
                         "downloading 12% of 93.50MiB at 12.00MiB/s")

    def test_the_handover_does_not_keep_a_stale_percentage(self):
        p = self._player(token=4)
        p._partial_token = 4
        p._set_status("playing while it downloads")
        p._on_progress(4, "downloading 99% of 93.50MiB")
        p._set_status("1080p avc1 - STORM (cached)")
        self.assertEqual(p.statuses[-1], "1080p avc1 - STORM (cached)")

    def test_a_stale_load_says_nothing(self):
        p = self._player(token=9)
        p._on_progress(4, "downloading 50% of 1MiB")
        self.assertEqual(p.statuses, [])

    def test_everything_goes_through_the_headline(self):
        # One direct emit, in the method that composes the line. A second is
        # how the next "last writer wins" bug arrives.
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("self.statusChanged.emit("), 1)

    def test_a_new_load_forgets_the_old_clips_size(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def load(self, url: str")[1].split(
            "\n    def ")[0]
        self.assertLess(body.index('self._size_note = ""'),
                        body.index('self._set_status("resolving...")'))


class FfmpegRepeatCountTests(unittest.TestCase):
    """ffmpeg's repeats are counted as ffmpeg's, not as slot A's.

    From a log: B's endless "Late SEI" pair was summed up at exit as
    "[mpv:A] (previous 4 line(s) repeated 16 more time(s))".
    """

    def setUp(self):
        self.p = Player("A")

    def _log(self, level, component, message):
        self.p._on_mpv_log(level, component, message)

    def test_the_summary_carries_the_same_label_as_the_lines(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            for _ in range(16):
                self._log("warn", "ffmpeg/video", "h264: Late SEI")
                self._log("warn", "ffmpeg/video", "h264: If you want to help")
            self._log("warn", "ffmpeg/video", "something new")
        text = out.getvalue()
        self.assertIn("[ffmpeg] (previous", text)
        self.assertNotIn("[mpv:A] (previous", text)

    def test_mpvs_own_repeats_are_still_the_slots(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            for _ in range(5):
                self._log("warn", "vd", "decoder hiccup")
            self._log("info", "cplayer", "Video: no video")
        self.assertIn("[mpv:A] (previous", out.getvalue())

    def test_the_two_windows_do_not_reset_each_other(self):
        # An ffmpeg line arriving must not clear slot A's own repeat count.
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            for _ in range(3):
                self._log("warn", "vd", "decoder hiccup")
            self._log("warn", "ffmpeg/video", "unrelated")
            self._log("info", "cplayer", "Video: no video")
        text = out.getvalue()
        self.assertIn("[mpv:A] (previous 1 line(s) repeated 2 more time(s))",
                      text)
        # With one shared window the ffmpeg line reports A's repeats as its
        # own - and the line above still comes out right, by coincidence,
        # which is why this assertion is the one that matters.
        self.assertNotIn("[ffmpeg] (previous", text)



def _powershell():
    """The PowerShell that runs setup.ps1 for real, or pwsh where there is none.

    On Windows that is Windows PowerShell 5.1 - powershell.exe, which is what
    the first-run installer and setup-runtime.bat start - so the behavioural
    tests below run against the interpreter the user's install meets.
    """
    if sys.platform == "win32":
        return shutil.which("powershell") or shutil.which("pwsh")
    return shutil.which("pwsh")


class ArchiveContainmentTests(unittest.TestCase):
    """setup.ps1 refuses an archive that would write outside its folder.

    The check this replaced (Assert-NoEscape, to 0.9.9.16) listed the folder
    after extraction and compared every path against Resolve-Path's spelling
    of the folder. On a tester's machine it rejected every file of a genuine
    archive, and the first-run install stopped at libmpv with deno and the
    token generator never reached. The likely reason, unconfirmed: Resolve-Path
    keeps an 8.3 short name (C:\\Users\\LONGNA~1\\...) where Get-ChildItem
    returns the long one. It could also never have caught a real escape: a
    listing of a folder only returns what is inside it.

    The behavioural tests run the functions out of setup.ps1 itself, in a real
    PowerShell, and skip where there is none.
    """

    @staticmethod
    def _setup() -> str:
        return (Path(__file__).resolve().parent.parent
                / "setup.ps1").read_text(encoding="utf-8")

    def _functions(self) -> str:
        source = self._setup()
        return source[source.index("function Test-UnsafeEntry"):
                      source.index("$Root   = ")]

    @staticmethod
    def _q(path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    def _run(self, body: str) -> str:
        exe = _powershell()
        if not exe:
            self.skipTest("no PowerShell on this machine")
        script = Path(self.tmp) / "harness.ps1"
        script.write_text('$ErrorActionPreference = "Stop"\n'
                          + self._functions() + "\n" + body + "\n",
                          encoding="utf-8")
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(script)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"harness died:\n{proc.stdout}\n{proc.stderr}")
        return proc.stdout

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chuutube-archive-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _zip(self, name: str, entries) -> Path:
        import zipfile
        path = Path(self.tmp) / name
        with zipfile.ZipFile(path, "w") as z:
            for entry in entries:
                z.writestr(entry, "x")
        return path

    # --- structure -----------------------------------------------------------
    def test_the_folder_comparing_check_is_gone(self):
        self.assertNotIn("function Assert-NoEscape", self._setup())
        self.assertNotIn("Assert-NoEscape $", self._setup())

    def test_nothing_is_extracted_unlisted_or_unchecked_for_links(self):
        # Every extraction in setup.ps1 - tar, 7zr, Expand-Archive - has the
        # archive's names checked before it and the result checked for links
        # after it. A fourth extractor added later without them fails here.
        lines = self._setup().splitlines()
        extractors = ("& tar.exe -xf", "& $sevenZip x", "Expand-Archive ")
        found = 0
        for i, line in enumerate(lines):
            code = line.strip()
            if code.startswith("#") or not any(e in code for e in extractors):
                continue
            found += 1
            before = "\n".join(lines[max(0, i - 12):i])
            after = "\n".join(lines[i + 1:i + 25])
            with self.subTest(line=i + 1, code=code):
                self.assertIn("Assert-SafeEntries", before)
                self.assertIn("Assert-NoLinks", after)
        self.assertEqual(found, 4, "tar, 7zr, deno's zip and the generator's zip")

    # --- behaviour, in a real PowerShell -------------------------------------
    def test_entry_names_that_leave_the_folder_are_unsafe(self):
        cases = {
            "include/mpv/client.h": False, "libmpv-2.dll": False,
            "notes..txt": False, "a/b..c/d.txt": False, "./a": False,
            "..": True, "../evil.dll": True, "a/../../evil.dll": True,
            "a\\..\\evil.dll": True, "/evil.dll": True, "\\evil.dll": True,
            "\\\\server\\share\\evil.dll": True, "C:evil.dll": True,
            "c:\\Windows\\evil.dll": True,
        }
        body = "\n".join(
            f"'{{0}}|{{1}}' -f {self._q(name)}, (Test-UnsafeEntry {self._q(name)})"
            for name in cases)
        seen = dict(line.rsplit("|", 1)
                    for line in self._run(body).splitlines() if "|" in line)
        for name, unsafe in cases.items():
            with self.subTest(name):
                self.assertEqual(seen.get(name), str(unsafe))

    def test_a_hostile_zip_is_refused_and_a_clean_one_accepted(self):
        zips = {
            "slip.zip": (["ok/a.txt", "../evil.txt"], True),
            "backslash.zip": (["ok/a.txt", "ok\\..\\..\\evil.txt"], True),
            "rooted.zip": (["C:/Windows/evil.txt"], True),
            "clean.zip": (["notes..txt", "a/b..c/d.txt", "deno.exe"], False),
        }
        body = []
        for name, (entries, _) in zips.items():
            path = self._zip(name, entries)
            body.append(
                f"try {{ Assert-SafeEntries (Get-ZipEntryNames {self._q(path)})"
                f" {self._q(path)}; '{name}|accepted' }}"
                f" catch {{ '{name}|refused' }}")
        out = self._run("\n".join(body))
        for name, (_, hostile) in zips.items():
            with self.subTest(name):
                self.assertIn(f"{name}|{'refused' if hostile else 'accepted'}",
                              out)

    def test_an_archive_that_cannot_be_listed_is_not_extracted(self):
        out = self._run("try { Assert-SafeEntries @() 'x.7z'; 'accepted' }"
                        " catch { \"refused: $_\" }")
        self.assertIn("refused: could not list the contents of x.7z", out)

    def test_the_7z_listing_skips_the_archives_own_path(self):
        # Abbreviated from 7-Zip 26.03's real "l -slt" output for the pinned
        # libmpv archive. setup.ps1 passes the archive by full path, so the
        # header's Path is rooted - read as an entry, it would refuse every
        # archive, which is this bug again in a new place.
        listing = [
            "7-Zip (r) 26.03 (x64) : Copyright (c) 1999-2026 Igor Pavlov",
            "", "Listing archive: C:\\Users\\X\\AppData\\Local\\Temp\\m.7z",
            "", "--", "Path = C:\\Users\\X\\AppData\\Local\\Temp\\m.7z",
            "Type = 7z", "", "----------",
            "Path = include", "Size = 0", "Attributes = D", "",
            "Path = include\\mpv\\client.h", "Size = 12", "",
            "Path = libmpv-2.dll", "Size = 99",
        ]
        # Passed by name: & resolves a function before an executable, and
        # the parameter is a [string], as the path to 7zr.exe is.
        out = self._run(
            "function fake7z { " + "; ".join(self._q(l) for l in listing)
            + "; $global:LASTEXITCODE = 0 }\n"
            "function failing7z { 'error'; $global:LASTEXITCODE = 2 }\n"
            "$n = Get-7zEntryNames 'fake7z' 'C:\\m.7z'\n"
            "'names|' + ($n -join ';')\n"
            "try { Assert-SafeEntries $n 'C:\\m.7z'; 'verdict|accepted' }"
            " catch { \"verdict|refused $_\" }\n"
            "'failed|' + @(Get-7zEntryNames 'failing7z' 'C:\\m.7z').Count\n")
        self.assertIn("names|include;include\\mpv\\client.h;libmpv-2.dll", out)
        self.assertIn("verdict|accepted", out)
        self.assertIn("failed|0", out)

    def test_a_link_inside_the_extracted_tree_is_refused(self):
        clean = Path(self.tmp) / "clean" / "sub"
        clean.mkdir(parents=True)
        (clean / "a.txt").write_text("a", encoding="utf-8")
        linked = Path(self.tmp) / "linked"
        linked.mkdir()
        outside = Path(self.tmp) / "outside"
        outside.mkdir()
        # A junction needs no privilege on Windows; a symlink would.
        kind = "Junction" if sys.platform == "win32" else "SymbolicLink"
        out = self._run(
            f"New-Item -ItemType {kind} -Path {self._q(linked / 'out')}"
            f" -Target {self._q(outside)} | Out-Null\n"
            f"try {{ Assert-NoLinks {self._q(clean.parent)}; 'clean|accepted' }}"
            " catch { \"clean|refused $_\" }\n"
            f"try {{ Assert-NoLinks {self._q(linked)}; 'linked|accepted' }}"
            " catch { 'linked|refused' }\n")
        self.assertIn("clean|accepted", out)
        self.assertIn("linked|refused", out)

    def test_a_refused_libmpv_warns_and_the_install_carries_on(self):
        # The libmpv block out of setup.ps1, with the download and tar stood
        # in for. A refusal used to throw straight out of the script, so deno
        # and the token generator - in the same run - were never attempted.
        source = self._setup()
        block = source[source.index("# ----------------------------"
                                    "------------------------------------ libmpv"):
                       source.index("# ----------------------------"
                                    "-------------------------------------- deno")]
        (Path(self.tmp) / "block.ps1").write_text(block, encoding="utf-8")
        root = Path(self.tmp) / "app"
        root.mkdir()
        out = self._run(
            "function Say($m) { \"SAY $m\" }\n"
            "function Warn($m) { \"WARN $m\" }\n"
            f"$Root = {self._q(root)}\n"
            "function New-ScratchDir($t) { $p = Join-Path $Root \"s-$t\";"
            " New-Item -ItemType Directory -Force $p | Out-Null; $p }\n"
            "$PINNED = @{ Mpv = @{ Tag = 't'; Name = 'm.7z' };"
            " SevenZip = @{ Tag = 't'; Name = '7zr.exe' } }\n"
            "function Get-Verified($Url, $OutFile, $Name) {"
            " Set-Content -LiteralPath $OutFile -Value 'x' }\n"
            "function tar.exe { if ($args[0] -eq '-tf') { 'include';"
            " '..\\..\\evil.dll'; $global:LASTEXITCODE = 0 }"
            " else { 'EXTRACTED' } }\n"
            f". {self._q(Path(self.tmp) / 'block.ps1')}\n"
            "'BLOCK DONE'\n")
        self.assertIn("WARN Did not install libmpv: refusing to extract m.7z",
                      out)
        self.assertNotIn("EXTRACTED", out)
        self.assertIn("BLOCK DONE", out)

    @unittest.skipUnless(sys.platform == "win32", "8.3 names are a Windows thing")
    def test_a_folder_reached_by_its_short_name_is_accepted(self):
        # The tester's failure, directly: extract into a folder spelled with
        # its 8.3 name and run the checks on that spelling.
        import ctypes
        target = Path(self.tmp) / "chuutube-short-name-check" / "x"
        target.mkdir(parents=True)
        buf = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetShortPathNameW(str(target), buf, 1024)
        short = buf.value
        if not short or short.lower() == str(target).lower():
            self.skipTest("8.3 names are switched off on this volume")
        zipped = self._zip("clean.zip", ["include/mpv/client.h", "libmpv-2.dll"])
        out = self._run(
            f"Assert-SafeEntries (Get-ZipEntryNames {self._q(zipped)}) {self._q(zipped)}\n"
            f"Expand-Archive -Path {self._q(zipped)} -DestinationPath {self._q(short)} -Force\n"
            f"Assert-NoLinks {self._q(short)}\n"
            "'ACCEPTED'\n")
        self.assertIn("ACCEPTED", out)



class SetupDownloadTests(unittest.TestCase):
    """A dropped download is retried, and a failed install says so.

    0.9.9.17, first run of the exe: the libmpv download was cut off ("The
    connection was closed unexpectedly"), setup.ps1 warned, carried on, and
    finished with "Runtime installed" and exit code 0. The first-run window
    read that as success and closed itself with the warning in it, and the
    user got a dead-end "missing dependency" dialog. These run the code out of
    setup.ps1 in a real PowerShell and skip where there is none.
    """

    @staticmethod
    def _setup() -> str:
        return (Path(__file__).resolve().parent.parent
                / "setup.ps1").read_text(encoding="utf-8")

    @staticmethod
    def _q(path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chuutube-download-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run(self, body: str):
        exe = _powershell()
        if not exe:
            self.skipTest("no PowerShell on this machine")
        source = self._setup()
        say_warn_die = "\n".join(
            line for line in source.splitlines()
            if line.startswith(("function Say(", "function Warn(",
                                "function Die(")))
        downloads = source[source.index("function Assert-TrustedUrl"):
                           source.index("# ------------------------------------"
                                        "------------------- zip-slip")]
        script = self.tmp / "harness.ps1"
        script.write_text('$ErrorActionPreference = "Stop"\n'
                          + say_warn_die + "\n" + downloads + "\n" + body
                          + "\n", encoding="utf-8")
        return subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(script)],
            capture_output=True, text=True, timeout=120)

    def _flaky_download(self, failures: int) -> str:
        # Invoke-WebRequest and Start-Sleep stood in for: the first
        # `failures` calls drop the connection after writing half a file.
        (self.tmp / "checksums.txt").write_text(
            "edeaaff3f1774ad2888673770c6d64097e391bc362d7d6fb34982ddf0efd18cb"
            "  thing.bin\n", encoding="utf-8")       # sha256 of "abc\n"
        return (
            f"$Root = {self._q(self.tmp)}\n"
            "$Pin = $false\n"
            "$script:calls = 0\n"
            f"$script:failures = {failures}\n"
            "function Start-Sleep { }\n"
            "function Invoke-WebRequest { param($Uri, $OutFile,"
            " [switch]$UseBasicParsing)\n"
            "  $script:calls++\n"
            "  if ($script:calls -le $script:failures) {\n"
            "    [IO.File]::WriteAllText($OutFile, 'half')\n"
            "    throw 'The request was aborted: The connection was closed"
            " unexpectedly.' }\n"
            "  [IO.File]::WriteAllText($OutFile, \"abc`n\") }\n"
            f"$out = {self._q(self.tmp / 'thing.bin')}\n"
            "try { Get-Verified 'https://github.com/x/thing.bin' $out"
            " 'thing.bin'; 'RESULT ok' } catch { \"RESULT failed: $_\" }\n"
            "'CALLS ' + $script:calls\n"
            "'LEFT ' + (Test-Path $out)\n")

    def test_a_dropped_download_is_retried_and_verified(self):
        proc = self._run(self._flaky_download(failures=2))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RESULT ok", proc.stdout)
        self.assertIn("CALLS 3", proc.stdout)
        # Two retries, each one said out loud rather than silently.
        self.assertIn("(attempt 1 of 3)", proc.stdout)
        self.assertIn("(attempt 2 of 3)", proc.stdout)
        self.assertIn("verified thing.bin against checksums.txt", proc.stdout)

    def test_it_gives_up_after_three_and_leaves_no_partial_file(self):
        proc = self._run(self._flaky_download(failures=5))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RESULT failed: The request was aborted", proc.stdout)
        self.assertIn("CALLS 3", proc.stdout)
        self.assertIn("LEFT False", proc.stdout)

    def test_the_progress_bar_is_off_before_any_download(self):
        # Windows PowerShell 5.1 redraws it per chunk, which slows a large
        # download by a multiple. Set at script scope, before the first fetch.
        source = self._setup()
        at = source.find('$ProgressPreference = "SilentlyContinue"')
        self.assertGreater(at, 0)
        self.assertLess(at, source.index("Invoke-WebRequest -Uri"))
        self.assertLess(at, source.index("# ------------------------------"
                                         "---------------------------------- libmpv"))

    def _verdict(self, dll_present: bool):
        exe = _powershell()
        if not exe:
            self.skipTest("no PowerShell on this machine")
        source = self._setup()
        block = source[source.index("# ------------------------------------"
                                    "--------------------------- verdict"):
                       source.index("# ------------------------------------"
                                    "-------------------------- diagnose")]
        die = next(l for l in source.splitlines()
                   if l.startswith("function Die("))
        dll = self.tmp / "mpv-2.dll"
        if dll_present:
            dll.write_bytes(b"x")
        script = self.tmp / "verdict.ps1"
        script.write_text(die + f"\n$dllTarget = {self._q(dll)}\n" + block
                          + "\n'REACHED THE END'\n", encoding="utf-8")
        return subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(script)],
            capture_output=True, text=True, timeout=120)

    def test_no_mpv_dll_at_the_end_is_a_failure_not_success(self):
        proc = self._verdict(dll_present=False)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("mpv-2.dll is not installed", proc.stdout)
        self.assertNotIn("REACHED THE END", proc.stdout)

    def test_with_mpv_dll_the_install_goes_on_to_finish(self):
        proc = self._verdict(dll_present=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REACHED THE END", proc.stdout)

    def test_the_verdict_comes_before_the_success_message(self):
        source = self._setup()
        self.assertLess(source.index("if (-not (Test-Path $dllTarget)) {\n"
                                     "    Write-Host \"\"\n    Die "),
                        source.index('Say "Runtime installed.'))


class InstallerLogTests(unittest.TestCase):
    """What the first-run install says ends up in the app's log file.

    The window closes itself on success, so before 0.9.9.18 the only copy of
    a warning went with it - and a tester's log said nothing at all about why
    the engine was missing.
    """

    def test_every_line_reaches_stderr_and_the_window(self):
        seen = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = firstrun._Installer(
                [sys.executable, "-c",
                 "print('==> Fetching libmpv'); print('!!  Could not fetch')"]
            ).run(seen.append)
        self.assertEqual(code, 0)
        self.assertEqual(seen, ["==> Fetching libmpv", "!!  Could not fetch"])
        log = err.getvalue()
        self.assertIn("[setup] ==> Fetching libmpv", log)
        self.assertIn("[setup] !!  Could not fetch", log)
        self.assertIn("[setup] installer exited with code 0", log)

    def test_an_installer_that_cannot_start_is_logged_too(self):
        seen = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = firstrun._Installer(
                [str(Path(tempfile.gettempdir()) / "no-such-installer.exe")]
            ).run(seen.append)
        self.assertEqual(code, -1)
        self.assertIn("[setup] Could not start the installer", err.getvalue())
        self.assertTrue(seen and seen[0].startswith("Could not start"))


class GeneratorPinTests(unittest.TestCase):
    """The token plugin and the generator source it fetches are pinned together.

    requirements.txt named the plugin with no version, so a fresh venv took
    2.0.2; the exe bundled it, and setup.ps1 then fetched the 2.0.2 generator
    source - which checksums.txt had no hash for, so it went unverified.
    """

    def test_the_plugin_is_pinned_to_a_version_with_a_checksum(self):
        root = Path(__file__).resolve().parent.parent
        reqs = (root / "requirements.txt").read_text(encoding="utf-8")
        pins = [line.split("==", 1)[1].strip()
                for line in reqs.splitlines()
                if line.strip().startswith("bgutil-ytdlp-pot-provider")
                and "==" in line]
        self.assertEqual(len(pins), 1, "bgutil-ytdlp-pot-provider must be ==pinned")
        sums = (root / "checksums.txt").read_text(encoding="utf-8")
        self.assertRegex(
            sums, rf"(?m)^[0-9a-f]{{64}}\s+bgutil-ytdlp-pot-provider-"
                  rf"{re.escape(pins[0])}\.zip$")



class MpvFallbackTests(unittest.TestCase):
    """When the pinned libmpv has been deleted upstream, the newest one is used.

    shinchiro/mpv-winbuild-cmake keeps only its latest 30 builds. Observed
    2026-10-31: the pinned 20260903 returned 404 and every first-run install
    failed. The fallback takes the newest release's mpv-dev-x86_64 archive and
    verifies it against the sha256 digest GitHub publishes for it.

    The libmpv section of setup.ps1 runs here for real, with GitHub stood in
    for: Invoke-WebRequest, Invoke-RestMethod and tar are functions.
    """

    PINNED_HASH = "edeaaff3f1774ad2888673770c6d64097e391bc362d7d6fb34982ddf0efd18cb"   # "abc\n"

    @staticmethod
    def _setup() -> str:
        return (Path(__file__).resolve().parent.parent
                / "setup.ps1").read_text(encoding="utf-8")

    @staticmethod
    def _q(path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chuutube-mpvfb-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run(self, *, pinned: str, digest: str | None = "good"):
        """pinned: 'ok', 'gone' (download fails) or 'tampered' (wrong bytes)."""
        exe = _powershell()
        if not exe:
            self.skipTest("no PowerShell on this machine")
        import hashlib
        source = self._setup()
        lines = source.splitlines()
        helpers = "\n".join(l for l in lines if l.startswith(
            ("function Say(", "function Warn(", "function Die(")))
        functions = source[source.index("function Assert-TrustedUrl"):
                           source.index("$Root   = ")]
        block = source[source.index("# ------------------------------------"
                                    "---------------------------- libmpv"):
                       source.index("# ------------------------------------"
                                    "------------------------------ deno")]
        root = self.tmp / "app"
        root.mkdir()
        (root / "checksums.txt").write_text(
            f"{self.PINNED_HASH}  mpv-dev-x86_64-20261021-git-e471496c56.7z\n",
            encoding="utf-8")
        real = hashlib.sha256(b"latest").hexdigest()
        published = {"good": f"sha256:{real}", "wrong": "sha256:" + "0" * 64,
                     None: None}[digest]
        digest_ps = "$null" if published is None else f"'{published}'"
        pinned_body = {
            "ok": "[IO.File]::WriteAllText($OutFile, \"abc`n\")",
            "gone": "throw 'The remote server returned an error: (404) Not Found.'",
            "tampered": "[IO.File]::WriteAllText($OutFile, 'evil')",
        }[pinned]
        body = f"""
$Root = {self._q(root)}
$Pin = $false
$PINNED = @{{ Mpv = @{{ Tag = '20261021'; Name = 'mpv-dev-x86_64-20261021-git-e471496c56.7z' }};
              SevenZip = @{{ Tag = 't'; Name = '7zr.exe' }} }}
function New-ScratchDir($t) {{ $p = Join-Path $Root "s-$t"; New-Item -ItemType Directory -Force $p | Out-Null; $p }}
function Start-Sleep {{ }}
$script:fetched = @()
$script:asked = 0
function Invoke-WebRequest {{ param($Uri, $OutFile, [switch]$UseBasicParsing)
  $script:fetched += $Uri
  if ($Uri -like '*/20261021/*') {{ {pinned_body} }}
  else {{ [IO.File]::WriteAllText($OutFile, 'latest') }} }}
function Invoke-RestMethod {{ param($Uri, [switch]$UseBasicParsing, $Headers)
  $script:asked++
  $base = 'https://github.com/shinchiro/mpv-winbuild-cmake/releases/download/20261031'
  [pscustomobject]@{{ tag_name = '20261031'; assets = @(
    [pscustomobject]@{{ name = 'mpv-x86_64-20261031-git-abc1234.7z'; digest = 'sha256:' + ('1' * 64); browser_download_url = "$base/mpv-x86_64-20261031-git-abc1234.7z" }},
    [pscustomobject]@{{ name = 'mpv-dev-x86_64-v3-20261031-git-abc1234.7z'; digest = 'sha256:' + ('2' * 64); browser_download_url = "$base/mpv-dev-x86_64-v3-20261031-git-abc1234.7z" }},
    [pscustomobject]@{{ name = 'mpv-dev-aarch64-20261031-git-abc1234.7z'; digest = 'sha256:' + ('3' * 64); browser_download_url = "$base/mpv-dev-aarch64-20261031-git-abc1234.7z" }},
    [pscustomobject]@{{ name = 'mpv-dev-x86_64-20261031-git-abc1234.7z'; digest = {digest_ps}; browser_download_url = "$base/mpv-dev-x86_64-20261031-git-abc1234.7z" }}
  ) }} }}
function tar.exe {{
  if ($args[0] -eq '-tf') {{ 'include'; 'libmpv-2.dll'; $global:LASTEXITCODE = 0 }}
  else {{ Set-Content -LiteralPath (Join-Path $args[3] 'libmpv-2.dll') -Value 'dll'; $global:LASTEXITCODE = 0 }} }}
. {self._q(self.tmp / 'block.ps1')}
'FETCHED ' + ($script:fetched -join ' ')
'ASKED ' + $script:asked
'INSTALLED ' + (Test-Path (Join-Path $Root 'mpv-2.dll'))
"""
        (self.tmp / "block.ps1").write_text(block, encoding="utf-8")
        script = self.tmp / "harness.ps1"
        script.write_text('$ErrorActionPreference = "Stop"\n' + helpers + "\n"
                          + functions + "\n" + body, encoding="utf-8")
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-File", str(script)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def test_the_pinned_build_is_used_while_it_exists(self):
        out = self._run(pinned="ok")
        self.assertIn("verified mpv-dev-x86_64-20261021-git-e471496c56.7z "
                      "against checksums.txt", out)
        self.assertIn("ASKED 0", out)
        self.assertIn("INSTALLED True", out)

    def test_a_deleted_pin_falls_back_to_the_newest_build(self):
        out = self._run(pinned="gone")
        self.assertIn("ASKED 1", out)
        # The plain x86_64 dev archive - not v3, aarch64 or the player build.
        self.assertIn("/20261031/mpv-dev-x86_64-20261031-git-abc1234.7z", out)
        self.assertNotIn("-v3-", out.split("FETCHED", 1)[1])
        self.assertIn("against the SHA-256 GitHub publishes for it", out)
        self.assertIn("not the build this release was tested with", out)
        self.assertIn("INSTALLED True", out)

    def test_a_tampered_pin_does_not_fall_back(self):
        # The file is there and is not what was tested: that is not "gone".
        out = self._run(pinned="tampered")
        self.assertIn("checksum mismatch", out)
        self.assertIn("ASKED 0", out)
        self.assertIn("INSTALLED False", out)

    def test_no_published_digest_means_no_install(self):
        out = self._run(pinned="gone", digest=None)
        self.assertIn("refusing to install it unverified", out)
        self.assertNotIn("20261031-git-abc1234.7z ", out.split("FETCHED", 1)[1]
                         .split("\n")[0] + " ")
        self.assertIn("INSTALLED False", out)

    def test_a_digest_that_does_not_match_is_refused(self):
        out = self._run(pinned="gone", digest="wrong")
        self.assertIn("checksum mismatch for mpv-dev-x86_64-20261031", out)
        self.assertIn("INSTALLED False", out)

    def test_the_pin_and_its_checksum_still_agree(self):
        source = self._setup()
        name = re.search(r'Mpv\s*=\s*@\{\s*Tag = "(\d{8})"\s*\n\s*Name = "([^"]+)"',
                         source)
        self.assertIsNotNone(name)
        self.assertIn(name.group(1), name.group(2))
        sums = (Path(__file__).resolve().parent.parent / "checksums.txt"
                ).read_text(encoding="utf-8")
        self.assertRegex(sums, rf"(?m)^[0-9a-f]{{64}}\s+{re.escape(name.group(2))}$")



class PrefetchProgressTests(unittest.TestCase):
    """A clip caching in the background says so, and how far it has got.

    The percentage already reached PrefetchJob.progress, but went nowhere
    until a Load joined - so a clip fetching behind one that was playing gave
    no sign of it until it was ready.
    """

    def _player(self):
        p = player_mod.Player.__new__(player_mod.Player)
        p._job, p._job_lock = None, threading.Lock()
        p.sent = []
        p._prefetch_progress = type("S", (), {
            "emit": lambda _s, v: p.sent.append(v)})()
        return p

    def test_an_unjoined_job_still_reports_every_line(self):
        job = player_mod.PrefetchJob("u", 1080)
        seen = []
        job.background = seen.append
        job.progress("downloading 20% of 1MiB")
        self.assertEqual(seen, ["downloading 20% of 1MiB"])

    def test_a_joined_load_still_gets_its_lines_too(self):
        job = player_mod.PrefetchJob("u", 1080)
        bg, joined = [], []
        job.background = bg.append
        job.attach(None, None, joined.append)
        job.progress("downloading 30% of 1MiB")
        self.assertEqual(bg, joined)

    def test_the_current_job_sends_its_percentage(self):
        p = self._player()
        job = p._job = player_mod.PrefetchJob("u", 1080)
        p._report_prefetch_progress(job, downloader.parse_progress(
            "[download]  42.3% of 120.00MiB at  5.00MiB/s ETA 00:20") or "")
        self.assertEqual(p.sent, [42])

    def test_lines_without_a_percentage_send_nothing(self):
        p = self._player()
        job = p._job = player_mod.PrefetchJob("u", 1080)
        p._report_prefetch_progress(job, "fetching (default)...")
        p._report_prefetch_progress(job, "cached")
        self.assertEqual(p.sent, [])

    def test_a_superseded_job_is_not_the_news(self):
        # Its yt-dlp keeps printing until it notices the cancel; those numbers
        # belong to a URL that is no longer in the box.
        p = self._player()
        old = player_mod.PrefetchJob("old", 1080)
        p._job = player_mod.PrefetchJob("new", 1080)
        p._report_prefetch_progress(old, "downloading 80% of 1MiB")
        self.assertEqual(p.sent, [])

    def test_a_foreground_load_reports_its_percentage(self):
        p = player_mod.Player.__new__(player_mod.Player)
        sent = []
        p.loadProgress = type("S", (), {"emit": lambda _s, v: sent.append(v)})()
        p._stale = lambda token: False
        p._partial_token = None
        p._set_status = lambda message: None
        p._on_progress(1, "downloading 37% of 120MiB at 5MiB/s")
        p._on_progress(1, "fetching (default)...")
        self.assertEqual(sent, [37])

    def test_prefetch_wires_the_job_up(self):
        source = Path(player_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def prefetch(self, url: str)")[1].split(
            "\n    def ")[0]
        self.assertIn("job.background = ", body)
        self.assertIn("self._report_prefetch_progress(", body)


class FetchIndicatorTests(unittest.TestCase):
    """Background caching shows in the URL field; a foreground load on the button.

    0.9.9.19 put both on the Load button ("Load 42%"), which read as though
    the clip were already loading when nobody had pressed anything.
    """

    def setUp(self):
        self.w = MainWindow()
        self.panel = self.w.panels[0]

    def strip(self):
        return self.panel.url_edit._strip

    def test_background_caching_is_a_strip_not_a_button_label(self):
        self.panel._set_prefetch_state("preparing")
        self.panel._on_prefetch_progress(42)
        self.assertAlmostEqual(self.strip(), 0.42)
        self.assertEqual(self.panel.load_btn.text(), "Load")

    def test_the_strip_moves_from_the_first_moment(self):
        # Page, token and format lookup take seconds before yt-dlp prints a
        # percentage. With nothing on screen, Paste got pressed again and again.
        self.panel._set_prefetch_state("preparing")
        self.assertEqual(self.strip(), ui_mod.STRIP_BUSY)
        self.assertTrue(self.panel.url_edit._animating)

    def test_the_first_percentage_stops_the_sweep(self):
        self.panel._set_prefetch_state("preparing")
        self.panel._on_prefetch_progress(3)
        self.assertFalse(self.panel.url_edit._animating)
        self.assertAlmostEqual(self.strip(), 0.03)

    def test_nothing_keeps_animating_once_it_is_over(self):
        for end in ("ready", ""):
            with self.subTest(end):
                self.panel._set_prefetch_state("preparing")
                self.panel._set_prefetch_state(end)
                self.assertFalse(self.panel.url_edit._animating)
        self.panel._set_prefetch_state("preparing")
        self.panel._set_loading(True)
        self.assertFalse(self.panel.url_edit._animating)

    def test_the_sweep_travels_through_the_track(self):
        track, _ = ui_mod.strip_rects(200, 30, 0.0)
        x, y, w, h = track
        # Enters from nothing at the left edge ...
        self.assertEqual(ui_mod.busy_rect(200, 30, 0.0)[2], 0)
        # ... is a full segment in the middle, inside the track ...
        mid = ui_mod.busy_rect(200, 30, 0.5)
        self.assertEqual(mid[2], int(w * ui_mod.BUSY_SEGMENT))
        self.assertGreaterEqual(mid[0], x)
        self.assertLessEqual(mid[0] + mid[2], x + w)
        self.assertEqual((mid[1], mid[3]), (y, h))
        # ... and has left on the right just before the phase wraps.
        late = ui_mod.busy_rect(200, 30, 0.999)
        self.assertLessEqual(late[0] + late[2], x + w)
        self.assertLess(late[2], mid[2])

    def test_one_sweep_moves_steadily(self):
        edge = ui_mod.UrlEdit()
        edge.set_strip(ui_mod.STRIP_BUSY)
        steps = round(ui_mod.BUSY_SWEEP_MS / ui_mod.BUSY_STEP_MS)
        for _ in range(steps // 2):
            edge._advance()
        self.assertAlmostEqual(edge._phase, 0.5, delta=0.05)

    def test_ready_fills_the_button_and_drops_the_strip(self):
        self.panel._set_prefetch_state("preparing")
        self.panel._on_prefetch_progress(99)
        self.panel._set_prefetch_state("ready")
        self.assertIsNone(self.strip())
        self.assertTrue(self.panel.load_btn.property("ready"))
        self.assertEqual(self.panel.load_btn.text(), "Load")

    def test_a_cleared_box_clears_the_strip(self):
        self.panel._set_prefetch_state("preparing")
        self.panel._set_prefetch_state("")
        self.assertIsNone(self.strip())

    def test_the_strip_shows_while_a_clip_is_playing(self):
        self.panel.player.stream = object()
        self.panel.status_label.setText("1080p (cached)")
        self.panel._set_prefetch_state("preparing")
        self.panel._on_prefetch_progress(10)
        self.assertAlmostEqual(self.strip(), 0.10)
        self.assertEqual(self.panel.status_label.text(), "1080p (cached)")

    def test_pressing_load_is_foreground_on_the_button(self):
        self.panel._set_loading(True)
        self.assertEqual(self.panel.load_btn.text(), "Loading")
        self.panel._on_load_progress(30)
        self.assertEqual(self.panel.load_btn.text(), "Loading 30%")

    def test_a_load_that_joins_a_background_fetch_takes_it_over(self):
        self.panel._set_prefetch_state("preparing")
        self.panel._on_prefetch_progress(60)
        self.panel._set_loading(True)
        self.assertIsNone(self.strip())
        self.assertEqual(self.panel.load_btn.text(), "Loading")

    def test_a_finished_load_goes_back_to_plain_load(self):
        self.panel._set_loading(True)
        self.panel._on_load_progress(80)
        self.panel._set_loading(False)
        self.assertEqual(self.panel.load_btn.text(), "Load")
        self.panel._set_loading(True)
        self.assertEqual(self.panel.load_btn.text(), "Loading",
                         "a new load starts from no percentage")

    def test_load_progress_outside_a_load_is_ignored(self):
        self.panel._on_load_progress(50)
        self.assertEqual(self.panel.load_btn.text(), "Load")

    def test_the_strip_sits_inside_the_bottom_border(self):
        track, filled = ui_mod.strip_rects(200, 30, 0.5)
        x, y, w, h = track
        self.assertEqual((x, w, h), (2, 196, ui_mod.STRIP_HEIGHT))
        self.assertEqual(y + h, 28)
        self.assertEqual(filled[2], 98)
        self.assertEqual(ui_mod.strip_rects(200, 30, 7.0)[1][2], 196)
        self.assertEqual(ui_mod.strip_rects(200, 30, -1.0)[1][2], 0)

    def test_the_button_has_room_for_its_longest_label(self):
        block = theme.STYLESHEET.split('QPushButton[role="load"] {')[1].split("}")[0]
        self.assertRegex(block, r"min-width:\s*\d+px")

    def test_the_start_glyph_renders_as_text(self):
        self.assertTrue(ui_mod.START_GLYPH.endswith("︎"))


class JumpToStartTests(unittest.TestCase):

    def setUp(self):
        self.w = MainWindow()
        self.calls = []
        for panel in self.w.panels:
            panel.player.seek = (lambda s, exact=True, i=panel.index:
                                 self.calls.append((i, s, exact)))

    def test_the_button_goes_to_the_first_frame(self):
        self.w.panels[1].jump_to_start()
        self.assertEqual(self.calls, [(1, 0, True)])

    def test_home_acts_on_the_selected_slot(self):
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _install_shortcuts")[1].split("\n    def ")[0]
        self.assertIn('add("Home", lambda: self.panels[self.selected_index]'
                      '.jump_to_start())', body)

    def test_the_button_sits_before_play(self):
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        self.assertLess(source.index("row3.addWidget(self.start_btn)"),
                        source.index("row3.addWidget(self.play_btn)"))


class ShortcutDocsTests(unittest.TestCase):
    """Every shortcut the window installs is in the README and the ? dialog."""

    NAMES = {"Left": "←", "Right": "→"}

    def _shortcuts(self):
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _install_shortcuts")[1].split("\n    def ")[0]
        found = re.findall(r'add\("([^"]+)"', body)
        self.assertGreater(len(found), 5)
        return found

    def test_the_readme_lists_them(self):
        readme = (Path(__file__).resolve().parent.parent / "README.md"
                  ).read_text(encoding="utf-8")
        section = readme.split("### Keyboard")[1].split("\n#")[0]
        for seq in self._shortcuts():
            for key in seq.split("+"):
                with self.subTest(seq):
                    self.assertIn(f"`{self.NAMES.get(key, key)}`", section)

    def test_the_help_dialog_lists_them(self):
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _show_help")[1].split("\n    def ")[0]
        for seq in self._shortcuts():
            for key in seq.split("+"):
                with self.subTest(seq):
                    self.assertIn(self.NAMES.get(key, key), body)



class PreviewLatePaintTests(unittest.TestCase):
    """A frame the preview ceiling turns away is shown a moment later, not never.

    K, 0.9.9.22: A then B loaded, A cleared, then B cleared - B's preview kept
    its last frame for seconds before going black. The black was drawn within
    1/PREVIEW_FPS of B's previous frame, so its repaint was skipped, and with
    both slots now empty no later frame came to repaint it.
    """

    def setUp(self):
        self.w = MainWindow()
        self.s = self.w.surface
        self.s._gl_ready = True
        self.painted = []
        self.s.update = lambda: self.painted.append(1)
        self.s.spout.publish = lambda *a, **k: None

    def _tick(self, drew=True):
        for player in self.s.players:
            player.render = lambda d=drew: d
        self.s._dirty = True
        self.s.tick()

    def test_a_frame_inside_the_ceiling_is_painted_later(self):
        self.s._last_paint = time.monotonic()        # just painted
        self._tick()
        self.assertEqual(self.painted, [])
        self.assertTrue(self.s._late_paint.isActive())
        ms = self.s._late_paint.started_with[0]
        self.assertGreaterEqual(ms, 1)
        self.assertLessEqual(ms, int(surface_mod.PREVIEW_INTERVAL * 1000) + 1)
        self.s._paint_late()                          # the timer fires
        self.assertEqual(self.painted, [1])

    def test_a_frame_outside_it_is_painted_at_once(self):
        self.s._last_paint = 0.0
        self._tick()
        self.assertEqual(self.painted, [1])
        self.assertFalse(self.s._late_paint.isActive())

    def test_one_late_paint_covers_a_burst(self):
        self.s._last_paint = time.monotonic()
        self._tick()
        first = self.s._late_paint.started_with
        self._tick()
        self.assertIs(self.s._late_paint.started_with, first,
                      "a second frame must not push the paint further back")

    def test_painting_cancels_a_pending_late_paint(self):
        self.s._last_paint = time.monotonic()
        self._tick()
        self.s._last_paint = 0.0
        self._tick()
        self.assertFalse(self.s._late_paint.isActive())

    def test_no_late_paint_while_the_window_is_dragged(self):
        self.s._last_paint = time.monotonic()
        self._tick()
        self.s._interacting = True
        self.s._paint_late()
        self.assertEqual(self.painted, [])



class ResizePumpTests(unittest.TestCase):
    """Window move/resize events drive the render loop when the timer starves.

    0.9.9.18 on K's machine, PERF_LOG_S: 53-121 ticks per 5s while resizing,
    ~240 otherwise, with the app's own GUI-thread share falling to 4-7%. Not
    slow work - Windows' size loop delivering the timer rarely.
    """

    def setUp(self):
        self.w = MainWindow()
        self.s = self.w.surface
        self.calls = []
        self.s.tick = lambda: self.calls.append(1)

    def test_an_overdue_tick_runs_from_the_event(self):
        self.s._last_tick_call = time.monotonic() - 1.0
        self.s.pump()
        self.assertEqual(self.calls, [1])

    def test_a_recent_tick_is_not_repeated(self):
        self.s._last_tick_call = time.monotonic()
        self.s.pump()
        self.assertEqual(self.calls, [])

    def test_resizing_and_moving_both_pump(self):
        for event in ("resizeEvent", "moveEvent"):
            with self.subTest(event):
                self.calls.clear()
                self.s._last_tick_call = 0.0
                getattr(self.w, event)(object())
                self.assertEqual(self.calls, [1])

    def test_every_tick_call_is_timed_even_with_nothing_to_do(self):
        surface = MainWindow().surface
        surface._gl_ready = True
        surface._last_tick_call = 0.0
        surface.tick()                          # nothing dirty: returns early
        self.assertGreater(surface._last_tick_call, 0.0)


class PerfGapTests(unittest.TestCase):
    """The perf line reports the longest wait between ticks."""

    def test_the_longest_gap_is_kept(self):
        perf = surface_mod.Perf(1.0)
        for t in (10.0, 10.004, 10.008, 10.200, 10.204):
            perf.tick_called(t)
        self.assertAlmostEqual(perf.max_gap, 0.192, places=6)

    def test_it_is_printed_and_carries_across_reports(self):
        perf = surface_mod.Perf(0.001)
        perf.tick_called(5.0)
        perf.tick_called(5.050)
        perf._t0 = time.monotonic() - 1.0
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            perf.report()
        self.assertIn("max gap 50ms", err.getvalue())
        self.assertEqual(perf.max_gap, 0.0)
        # A gap spanning the report still counts in the next one.
        perf.tick_called(5.150)
        self.assertAlmostEqual(perf.max_gap, 0.100, places=6)



class PluginPinHeldTests(unittest.TestCase):
    """The pinned token plugin is the one that gets built in.

    The 0.9.9.24 zip bundled bgutil-ytdlp-pot-provider 2.0.2 against a 2.0.0
    pin: setup.ps1 installed requirements.txt and then ran
    `pip install --upgrade bgutil-ytdlp-pot-provider`.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def test_setup_installs_the_plugin_only_through_requirements(self):
        setup = (self.ROOT / "setup.ps1").read_text(encoding="utf-8")
        for line in setup.splitlines():
            code = line.split("#", 1)[0]
            if "pip" in code and "install" in code:
                with self.subTest(line=line.strip()):
                    self.assertNotIn("bgutil", code)

    def test_build_checks_the_venv_against_the_pin_before_building(self):
        build = (self.ROOT / "build.ps1").read_text(encoding="utf-8")
        guard = build.index("Get-RequirementPin (Join-Path $Root \"requirements.txt\")")
        self.assertLess(guard, build.index("-m PyInstaller"))
        self.assertIn("if ($installed -ne $wanted) {", build)

    def _pin(self, requirements: str) -> str:
        exe = _powershell()
        if not exe:
            self.skipTest("no PowerShell on this machine")
        tmp = Path(tempfile.mkdtemp(prefix="chuutube-pin-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        build = (self.ROOT / "build.ps1").read_text(encoding="utf-8")
        start = build.index("function Get-RequirementPin")
        end = build.index("\n}\n", start) + 3
        (tmp / "requirements.txt").write_text(requirements, encoding="utf-8")
        script = tmp / "pin.ps1"
        script.write_text(build[start:end] + "\n$r = Get-RequirementPin '"
                          + str(tmp / "requirements.txt").replace("'", "''")
                          + "' 'bgutil-ytdlp-pot-provider'\n"
                          "if ($null -eq $r) { 'PIN <none>' } else { 'PIN ' + $r }\n",
                          encoding="utf-8")
        proc = subprocess.run([exe, "-NoProfile", "-NonInteractive",
                               "-ExecutionPolicy", "Bypass", "-File", str(script)],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.strip().splitlines()[-1]

    def test_the_real_requirements_pin_is_read(self):
        reqs = (self.ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertEqual(self._pin(reqs), "PIN 2.0.0")

    def test_an_unpinned_plugin_is_no_pin(self):
        self.assertEqual(self._pin("yt-dlp\nbgutil-ytdlp-pot-provider\n"),
                         "PIN <none>")
        self.assertEqual(self._pin("bgutil-ytdlp-pot-provider>=2.0.0\n"),
                         "PIN <none>")

    def test_comments_and_neighbours_do_not_confuse_it(self):
        self.assertEqual(self._pin(
            "# bgutil-ytdlp-pot-provider==9.9.9 was tried\n"
            "bgutil-ytdlp-pot-provider-extra==1.0\n"
            "bgutil-ytdlp-pot-provider == 2.0.0  # tested\n"), "PIN 2.0.0")



class RemoteComponentsOffTests(unittest.TestCase):
    """`REMOTE_COMPONENTS=` turns yt-dlp's GitHub download off, as documented.

    SECURITY.md has said so all along; config read the empty value as "use
    the default" until 0.9.9.26, so it stayed on.
    """

    def _value(self, raw):
        key = config.ENV_PREFIX + "REMOTE_COMPONENTS"
        saved = os.environ.pop(key, None)
        self.addCleanup(lambda: os.environ.pop(key, None) if saved is None
                        else os.environ.__setitem__(key, saved))
        if raw is not None:
            os.environ[key] = raw
        return config._text_or_empty("REMOTE_COMPONENTS", "ejs:github")

    def test_unset_is_the_default(self):
        self.assertEqual(self._value(None), "ejs:github")

    def test_empty_is_off(self):
        self.assertEqual(self._value(""), "")
        self.assertEqual(self._value("   "), "")

    def test_a_value_is_kept(self):
        self.assertEqual(self._value(" ejs:npm "), "ejs:npm")

    def test_off_means_no_flag_for_yt_dlp(self):
        saved = downloader.REMOTE_COMPONENTS
        self.addCleanup(setattr, downloader, "REMOTE_COMPONENTS", saved)
        for value, expected in (("", False), ("ejs:github", True)):
            downloader.REMOTE_COMPONENTS = value
            with self.subTest(value=value):
                cmd = downloader._command("yt-dlp", "https://www.youtube.com/watch?v=x",
                                          "best", Path("c"), Path("p.txt"), [])
                self.assertEqual("--remote-components" in cmd, expected)

    def test_the_setting_uses_it(self):
        source = Path(config.__file__).read_text(encoding="utf-8")
        self.assertIn('REMOTE_COMPONENTS = _text_or_empty("REMOTE_COMPONENTS"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)


def _sixteen_by_nine_cells(surface):
    """Size a stub surface so each preview cell's picture area is 640x360."""
    b, g = surface_mod.BORDER, surface_mod.GAP
    surface.width = lambda: 2 * (640 + 2 * b) + g
    surface.height = lambda: 360 + 2 * b
    surface.devicePixelRatioF = lambda: 1.0


class EmptySlotTransparencyTests(unittest.TestCase):
    """An empty slot sends transparency, and the preview shows it as a grid.

    K, 0.9.9.27: a slot with no video should be see-through in Resolume, the
    preview should show the usual checkerboard (dark, coarse), and both can be
    switched off - PREVIEW_GRID=0 for a black preview, EMPTY_TRANSPARENT=0 to
    send black as before.
    """

    @contextlib.contextmanager
    def _settings(self, **settings):
        keys = {f"CHUUTUBE2SPOUT_{k}": str(v) for k, v in settings.items()}
        try:
            with mock.patch.dict(os.environ, keys, clear=False):
                yield importlib.reload(config)
        finally:
            importlib.reload(config)

    # -- settings -----------------------------------------------------------

    def test_transparent_with_a_grid_by_default(self):
        self.assertTrue(config.EMPTY_TRANSPARENT)
        self.assertTrue(config.PREVIEW_GRID)
        self.assertEqual(config.empty_rgba(), (0.0, 0.0, 0.0, 0.0))
        self.assertTrue(config.show_grid())

    def test_grid_off_keeps_the_transparency(self):
        with self._settings(PREVIEW_GRID=0) as cfg:
            self.assertFalse(cfg.show_grid())
            self.assertEqual(cfg.empty_rgba()[3], 0.0)

    def test_transparency_off_sends_black_and_drops_the_grid(self):
        with self._settings(EMPTY_TRANSPARENT=0) as cfg:
            self.assertEqual(cfg.empty_rgba(), (0.0, 0.0, 0.0, 1.0))
            self.assertFalse(cfg.show_grid(),
                             "nothing is transparent, so no grid")

    def test_bars_are_a_separate_setting(self):
        self.assertEqual(config.background_rgba(), (0.0, 0.0, 0.0, 1.0),
                         "SENDER_BACKGROUND stays black by default")

    # -- what is sent -------------------------------------------------------

    def test_the_idle_frame_is_transparent(self):
        pixels = surface_mod.empty_pixels(3, 2)
        self.assertEqual(pixels, bytes(3 * 2 * 4))

    def test_the_idle_frame_is_black_when_transparency_is_off(self):
        with mock.patch.object(surface_mod, "empty_rgba",
                               lambda: (0.0, 0.0, 0.0, 1.0)):
            pixels = surface_mod.empty_pixels(3, 2)
        self.assertEqual(pixels, bytes((0, 0, 0, 255)) * 6)

    def _cleared_colour(self, colour):
        player = player_mod.Player("A")
        player._render_ctx = object()
        player.target.fbo, player.target.w, player.target.h = 5, 8, 8
        player.empty = False
        player._blank = True
        seen = []
        gl = mock.MagicMock()
        gl.glClearColor.side_effect = lambda *c: seen.append(c)
        with mock.patch.object(player_mod, "GL", gl), \
                mock.patch.object(player_mod, "empty_rgba", lambda: colour):
            self.assertTrue(player.render())
        return player, seen

    def test_clear_uses_the_empty_colour(self):
        player, seen = self._cleared_colour((0.0, 0.0, 0.0, 0.0))
        self.assertEqual(seen, [(0.0, 0.0, 0.0, 0.0)])
        self.assertTrue(player.empty)

    def test_clear_sends_black_when_asked(self):
        _, seen = self._cleared_colour((0.0, 0.0, 0.0, 1.0))
        self.assertEqual(seen, [(0.0, 0.0, 0.0, 1.0)])

    def test_a_video_frame_ends_empty(self):
        player = player_mod.Player("A")
        player._render_ctx = mock.MagicMock()
        player._render_ctx.update.return_value = True
        player._vid_w, player._vid_h = 1920, 1080
        player.target.ensure = lambda w, h: True
        player.target.fbo, player.target.w, player.target.h = 5, 1920, 1080
        self.assertTrue(player.empty)
        self.assertTrue(player.render())
        self.assertFalse(player.empty)

    # -- the grid -----------------------------------------------------------

    def test_checker_pattern(self):
        sq = 2
        px = surface_mod.checker_pixels(8, 6, sq, dark=(1, 1, 1),
                                        light=(9, 9, 9))
        self.assertEqual(len(px), 8 * 6 * 4)

        def at(x, y):
            i = (y * 8 + x) * 4
            return px[i]
        self.assertEqual(at(0, 0), 1)
        self.assertEqual(at(1, 1), 1)
        self.assertEqual(at(2, 0), 9)
        self.assertEqual(at(0, 2), 9)
        self.assertEqual(at(2, 2), 1)
        self.assertTrue(all(px[i + 3] == 255 for i in range(0, len(px), 4)),
                        "the grid itself is opaque")

    def test_grid_is_dark_and_coarse(self):
        self.assertGreaterEqual(surface_mod.GRID_SQUARE, 12)
        self.assertLess(max(surface_mod.GRID_LIGHT), 0x50,
                        "no flashbang on a dark stage")

    def test_checker_grows_in_steps(self):
        gl = mock.MagicMock()
        gl.glCheckFramebufferStatus.return_value = gl.GL_FRAMEBUFFER_COMPLETE
        uploads = []
        gl.glTexImage2D.side_effect = lambda *a: uploads.append(a[3:5])
        c = surface_mod.Checker()
        with mock.patch.object(surface_mod, "GL", gl):
            self.assertTrue(c.ensure(300, 200, 16))
            self.assertEqual((c.w, c.h), (512, 256))
            self.assertTrue(c.ensure(400, 250, 16))
            self.assertEqual(uploads, [(512, 256)], "fits: no rebuild")
            self.assertTrue(c.ensure(600, 250, 16))
            self.assertEqual(uploads[-1], (768, 256))
            self.assertTrue(c.ensure(100, 100, 32))
            self.assertEqual(uploads[-1], (256, 256),
                             "a new square size starts again")

    def test_a_failed_grid_is_not_retried_every_paint(self):
        gl = mock.MagicMock()
        gl.glCheckFramebufferStatus.return_value = 0
        c = surface_mod.Checker()
        with mock.patch.object(surface_mod, "GL", gl), \
                mock.patch("sys.stderr", io.StringIO()):
            self.assertFalse(c.ensure(300, 200, 16))
            self.assertFalse(c.ensure(300, 200, 16))
        self.assertEqual(gl.glGenTextures.call_count, 1)

    def _paint(self, empty, grid=True):
        w = MainWindow()
        s = w.surface
        _sixteen_by_nine_cells(s)
        s._checker = surface_mod.Checker() if grid else None
        for p in s.players:
            p.target.fbo, p.target.w, p.target.h = 5, 1920, 1080
            p.empty = empty
        grids, blits = [], []
        s._draw_grid = lambda *a: grids.append(a)
        gl = mock.MagicMock()
        gl.glBlitFramebuffer.side_effect = lambda *a: blits.append(a)
        with mock.patch.object(surface_mod, "GL", gl):
            s._paint()
        return grids, blits

    def test_an_empty_slot_shows_the_grid(self):
        grids, blits = self._paint(empty=True)
        self.assertEqual(len(grids), 2)
        self.assertEqual(blits, [], "the transparent FBO is not drawn over it")

    def test_a_playing_slot_shows_its_frame(self):
        grids, blits = self._paint(empty=False)
        self.assertEqual(grids, [])
        self.assertEqual(len(blits), 2)

    def test_without_the_grid_an_empty_slot_is_black_as_before(self):
        grids, blits = self._paint(empty=True, grid=False)
        self.assertEqual(grids, [])
        self.assertEqual(len(blits), 2)

    def test_the_grid_is_released_on_shutdown(self):
        source = Path(surface_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def shutdown")[1].split("\n    def ")[0]
        self.assertIn("_checker.destroy()", body)


class WindowFloorTests(unittest.TestCase):
    """The window cannot start, or be dragged, smaller than its controls.

    Tester, 0.9.9.25: the app opened at 689x399 (a restored size; the default
    is 1320 wide) and Paste, Clear and the URL field were squeezed below their
    text, while Load - the one button with a hard minimum - kept its width.
    """

    class _Size:
        def __init__(self, w, h):
            self._w, self._h = w, h

        def width(self):
            return self._w

        def height(self):
            return self._h

    def setUp(self):
        self.w = MainWindow()
        self.minimums = []
        self.w.setMinimumSize = lambda w, h: self.minimums.append((w, h))
        self._scale = ui_mod.WINDOW_SCALE
        self._fill = ui_mod.WINDOW_FILL_SCREEN
        self._width = ui_mod.WINDOW_WIDTH
        ui_mod.WINDOW_SCALE, ui_mod.WINDOW_FILL_SCREEN = 1.0, False
        ui_mod.WINDOW_WIDTH = 0

    def tearDown(self):
        ui_mod.WINDOW_SCALE, ui_mod.WINDOW_FILL_SCREEN = self._scale, self._fill
        ui_mod.WINDOW_WIDTH = self._width

    def _fit(self, floor):
        self.w.minimumSizeHint = lambda: self._Size(*floor)
        self.w.resizes.clear()
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.w._first_fit()
        return err.getvalue()

    def test_a_restored_size_below_the_floor_starts_at_the_default(self):
        self._fit((1100, 380))          # the stub window is 1000 wide
        self.assertEqual(self.w.resizes[-1][0], ui_mod.DEFAULT_WIDTH)

    def test_a_restored_size_that_fits_is_kept(self):
        self._fit((900, 380))
        self.assertTrue(all(w == self.w.width() for w, _ in self.w.resizes),
                        "a size the controls fit into is the user's choice")

    def test_the_floor_becomes_a_real_minimum(self):
        self._fit((900, 380))
        self.assertEqual(self.minimums, [(900, 380)])

    def test_the_minimum_is_applied_after_the_fit(self):
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        body = source.split("def _first_fit")[1].split("\n    def ")[0]
        self.assertLess(body.index("self.fit_preview(width)"),
                        body.index("self.setMinimumSize(*floor)"))

    def test_the_log_names_the_minimum(self):
        line = self._fit((900, 380))
        self.assertIn("minimum 900x380", line)

    def test_no_floor_from_a_stub_is_not_fatal(self):
        self.w.minimumSizeHint = lambda: object()
        with mock.patch("sys.stderr", io.StringIO()):
            self.w._first_fit()
        self.assertEqual(self.minimums, [])

    def test_the_url_row_has_hard_minimums(self):
        from chuutube2spout import theme
        sheet = theme.STYLESHEET
        for role in ("paste", "load", "clear"):
            with self.subTest(role=role):
                block = sheet.split(f'QPushButton[role="{role}"] {{')[1]
                block = block.split("}")[0]
                self.assertIn("min-width:", block)
        panel = self.w.panels[0]
        self.assertGreaterEqual(panel.url_edit.minimumWidth(),
                                ui_mod.URL_MIN_WIDTH)


class WindowWidthTests(unittest.TestCase):
    """WINDOW_WIDTH opens the window at a fixed width (K, 0.9.9.29).

    WINDOW_SCALE is relative to the default; this is absolute, and
    WINDOW_WIDTH=1320 is the way back to the default size every start.
    """

    class _Size:
        def __init__(self, w, h):
            self._w, self._h = w, h

        def width(self):
            return self._w

        def height(self):
            return self._h

    def setUp(self):
        self.w = MainWindow()
        self.w.setMinimumSize = lambda w, h: None
        self.w.minimumSizeHint = lambda: self._Size(877, 404)
        self.w._available_size = lambda: (2560, 1400)
        self._saved = (ui_mod.WINDOW_SCALE, ui_mod.WINDOW_FILL_SCREEN,
                       ui_mod.WINDOW_WIDTH)

    def tearDown(self):
        (ui_mod.WINDOW_SCALE, ui_mod.WINDOW_FILL_SCREEN,
         ui_mod.WINDOW_WIDTH) = self._saved

    def _fit(self, width=0, scale=1.0, fill=False):
        ui_mod.WINDOW_WIDTH, ui_mod.WINDOW_SCALE = width, scale
        ui_mod.WINDOW_FILL_SCREEN = fill
        self.w.resizes.clear()
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.w._first_fit()
        last = self.w.resizes[-1] if self.w.resizes else None
        return last, err.getvalue()

    def test_off_by_default(self):
        self.assertEqual(config.WINDOW_WIDTH, 0)

    def test_a_fixed_width_is_used(self):
        (width, _), _ = self._fit(width=1600)
        self.assertEqual(width, 1600)

    def test_it_wins_over_scale_and_max(self):
        (width, _), _ = self._fit(width=1600, scale=2.0)
        self.assertEqual(width, 1600)
        (width, _), _ = self._fit(width=1600, fill=True)
        self.assertEqual(width, 1600)

    def test_never_below_the_controls(self):
        (width, _), _ = self._fit(width=400)
        self.assertEqual(width, 877)

    def test_never_wider_than_the_screen(self):
        (width, _), _ = self._fit(width=9000)
        self.assertLessEqual(width, 2560)

    def test_the_height_follows(self):
        (width, height), _ = self._fit(width=1600)
        chrome_w = self.w.width() - self.w.surface.width()
        chrome_h = self.w.height() - self.w.surface.height()
        self.assertEqual(height - chrome_h,
                         self.w.surface.height_for_width(width - chrome_w))

    def test_the_log_says_which_setting_decided(self):
        _, log = self._fit(width=1600)
        self.assertIn("window: width 1600,", log)
        _, log = self._fit()
        self.assertIn("window: scale 1,", log)

    def test_parsing(self):
        for raw, want in (("1600", 1600), ("1600.0", 1600), ("", 0),
                          ("wide", 0), ("-5", 0), ("99999", 16384)):
            with self.subTest(raw=raw), \
                    _env(CHUUTUBE2SPOUT_WINDOW_WIDTH=raw):
                self.assertEqual(config._window_width(), want)


class SignInNoticeTests(unittest.TestCase):
    """Without cookies, a bot check stops the chain and says how to sign in.

    K, 0.9.9.30: with no COOKIES_FROM_BROWSER every client got "Sign in to
    confirm you're not a bot" and HTTP 429, one after the other.
    """

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="chuutube-bot-")
        source = Path(__file__).resolve().parent / "fake_botcheck_ytdlp.py"
        cls.tool = str(Path(cls.dir) / "bot_ytdlp.py")
        shutil.copy(source, cls.tool)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="chuutube-cache-")
        self.addCleanup(shutil.rmtree, self.cache, True)
        patches = [
            mock.patch.object(downloader, "ytdlp_command",
                              lambda *a, **k: [sys.executable, self.tool]),
            mock.patch.object(downloader, "CACHE_MANIFEST", False),
            mock.patch.object(downloader, "fresh_info", lambda *a: None),
            mock.patch("sys.stderr", io.StringIO()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _invocations(self):
        return int((Path(self.cache) / "_invocations").read_text())

    def _download(self, browser, jar):
        with mock.patch.object(downloader, "COOKIES_FROM_BROWSER", browser), \
                mock.patch.object(downloader, "cookie_args", lambda: jar):
            return downloader.download("https://example/watch", 1080,
                                       self.cache)

    def test_without_cookies_one_attempt_and_a_hint(self):
        with self.assertRaises(downloader.SignInRequired) as caught:
            self._download("", [])
        self.assertEqual(self._invocations(), 1,
                         "the rest of the chain only adds 429s")
        self.assertIn("COOKIES_FROM_BROWSER", str(caught.exception))

    def test_with_cookies_the_chain_still_runs(self):
        jar = Path(self.cache) / "cookies.txt"
        jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        with self.assertRaises(downloader.DownloadError) as caught:
            self._download("firefox", ["--cookies", str(jar)])
        self.assertNotIsInstance(caught.exception, downloader.SignInRequired)
        self.assertGreater(self._invocations(), 1)

    def test_it_is_a_download_error_so_the_slot_shows_it(self):
        # player.py reports DownloadError on the slot's status line.
        self.assertTrue(issubclass(downloader.SignInRequired,
                                   downloader.DownloadError))

    def test_both_apostrophes_match(self):
        for text in ("Sign in to confirm you\u2019re not a bot",
                     "Sign in to confirm you're not a bot"):
            with self.subTest(text=text):
                self.assertTrue(downloader._BOT_CHECK.search(text))


class ModalLoopTickTests(unittest.TestCase):
    """Rendering continues while the title bar is held (K, 0.9.9.31).

    Pressing on the title bar starts Windows' move loop before the mouse moves,
    Qt's timers starve there, and Spout froze with the preview. A timer set on
    the window itself still fires in that loop.
    """

    class _User32:
        def __init__(self):
            self.calls = []

        def SetTimer(self, hwnd, ident, ms, proc):        # noqa: N802
            self.calls.append(("set", hwnd, ident, ms))
            return ident

        def KillTimer(self, hwnd, ident):                  # noqa: N802
            self.calls.append(("kill", hwnd, ident))
            return 1

    def setUp(self):
        from chuutube2spout import modalloop
        self.m = modalloop
        self.user32 = self._User32()
        self.ticks = []
        self.t = modalloop.ModalTicker(lambda: self.ticks.append(1),
                                       user32=self.user32)
        self._keep = []

    def _send(self, kind, wparam=0, hwnd=0x1234, event=b"windows_generic_MSG"):
        msg = self.m.MSG(hwnd, kind, wparam, 0)
        self._keep.append(msg)
        return self.t.handle(event, ctypes.addressof(msg))

    def test_the_loop_starts_a_timer_on_the_window(self):
        self.assertFalse(self._send(self.m.WM_ENTERSIZEMOVE))
        self.assertEqual(self.user32.calls,
                         [("set", 0x1234, self.m.TIMER_ID, self.m.TIMER_MS)])
        self.assertTrue(self.t.active)

    def test_our_timer_ticks_and_is_handled(self):
        self._send(self.m.WM_ENTERSIZEMOVE)
        self.assertTrue(self._send(self.m.WM_TIMER, self.m.TIMER_ID))
        self.assertTrue(self._send(self.m.WM_TIMER, self.m.TIMER_ID))
        self.assertEqual(len(self.ticks), 2)

    def test_other_timers_are_left_to_qt(self):
        self._send(self.m.WM_ENTERSIZEMOVE)
        self.assertFalse(self._send(self.m.WM_TIMER, 7))
        self.assertEqual(self.ticks, [])

    def test_leaving_the_loop_stops_the_timer(self):
        self._send(self.m.WM_ENTERSIZEMOVE)
        self._send(self.m.WM_EXITSIZEMOVE)
        self.assertEqual(self.user32.calls[-1],
                         ("kill", 0x1234, self.m.TIMER_ID))
        self.assertFalse(self.t.active)

    def test_a_second_enter_does_not_leak_a_timer(self):
        self._send(self.m.WM_ENTERSIZEMOVE)
        self._send(self.m.WM_ENTERSIZEMOVE)
        kinds = [c[0] for c in self.user32.calls]
        self.assertEqual(kinds, ["set", "kill", "set"])

    def test_other_event_types_are_ignored(self):
        self.assertFalse(self._send(self.m.WM_ENTERSIZEMOVE,
                                    event=b"xcb_generic_event_t"))
        self.assertEqual(self.user32.calls, [])

    def test_a_qbytearray_event_type_is_understood(self):
        class _QBA:
            def data(self):
                return b"windows_generic_MSG"
        msg = self.m.MSG(0x1234, self.m.WM_ENTERSIZEMOVE, 0, 0)
        self.t.handle(_QBA(), ctypes.addressof(msg))
        self.assertTrue(self.t.active)

    def test_off_windows_it_does_nothing(self):
        t = self.m.ModalTicker(lambda: self.ticks.append(1))
        if sys.platform != "win32":
            msg = self.m.MSG(0x1234, self.m.WM_ENTERSIZEMOVE, 0, 0)
            self.assertFalse(t.handle(b"windows_generic_MSG",
                                      ctypes.addressof(msg)))
            self.assertFalse(t.active)

    def test_a_failing_tick_never_escapes_into_qt(self):
        t = self.m.ModalTicker(lambda: 1 / 0, user32=self.user32)
        msg = self.m.MSG(0x1234, self.m.WM_TIMER, self.m.TIMER_ID, 0)
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertTrue(t.handle(b"windows_generic_MSG",
                                     ctypes.addressof(msg)))
            t.handle(b"windows_generic_MSG", ctypes.addressof(msg))
        self.assertEqual(err.getvalue().count("could not keep rendering"), 1)

    def test_a_bad_message_pointer_never_escapes(self):
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertFalse(self.t.handle(b"windows_generic_MSG", "nonsense"))

    def test_the_window_routes_native_events_through_it(self):
        w = MainWindow()
        self.assertIsInstance(w._modal, self.m.ModalTicker)
        w._modal = self.t
        msg = self.m.MSG(0x1234, self.m.WM_TIMER, self.m.TIMER_ID, 0)
        self.assertEqual(w.nativeEvent(b"windows_generic_MSG",
                                       ctypes.addressof(msg)), (True, 0))
        self.assertEqual(self.ticks, [1])

    def test_the_window_ticks_through_pump(self):
        # pump() only ticks when the render timer is overdue, so the Windows
        # timer and a late Qt timer cannot double up.
        source = Path(ui_mod.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "ModalTicker(self.surface.held_tick, self.surface.set_held)",
            source)

    # -- 0.9.9.33: K saw the preview stay frozen while held ----------------

    def test_hold_is_reported_to_the_preview(self):
        held = []
        t = self.m.ModalTicker(lambda: None, held.append, user32=self.user32)
        for kind in (self.m.WM_ENTERSIZEMOVE, self.m.WM_EXITSIZEMOVE):
            msg = self.m.MSG(0x1234, kind, 0, 0)
            with mock.patch("sys.stderr", io.StringIO()):
                t.handle(b"windows_generic_MSG", ctypes.addressof(msg))
        self.assertEqual(held, [True, False])

    def test_each_hold_logs_how_often_the_timer_fired(self):
        # The one number that tells "Windows did not deliver the timer" from
        # "it did, and something else is frozen".
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self._send(self.m.WM_ENTERSIZEMOVE)
            for _ in range(3):
                self._send(self.m.WM_TIMER, self.m.TIMER_ID)
            self._send(self.m.WM_EXITSIZEMOVE)
        self.assertRegex(err.getvalue(),
                         r"window held \d+\.\ds: 3 render ticks")

    def test_a_refused_timer_is_logged(self):
        self.user32.SetTimer = lambda *a: 0
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self._send(self.m.WM_ENTERSIZEMOVE)
        self.assertIn("refused", err.getvalue())
        self.assertFalse(self.t.active)

    def _surface(self):
        w = MainWindow()
        s = w.surface
        calls = []
        s.update = lambda: calls.append("update")
        s.repaint = lambda: calls.append("repaint")
        return s, calls

    def test_while_held_the_preview_paints_at_once(self):
        # update() only queues the paint, and the move loop starves the timer
        # that would deliver it.
        s, calls = self._surface()
        s.set_held(True)
        s._paint_now(time.monotonic())
        self.assertEqual(calls, ["repaint"])

    def test_otherwise_it_queues_as_before(self):
        s, calls = self._surface()
        s._paint_now(time.monotonic())
        self.assertEqual(calls, ["update"])

    def test_letting_go_brings_the_preview_up_to_date(self):
        s, calls = self._surface()
        s.set_held(True)
        s._force_tick = False
        s.set_held(False)
        self.assertTrue(s._force_tick)
        self.assertEqual(calls, ["update"])


class HeldTickTests(unittest.TestCase):
    """While held, a tick renders without waiting for frameReady (0.9.9.34).

    K's 0.9.9.33 log: the Windows timer fired ~60 times a second while the
    window was held, and the picture still froze. frameReady is a queued,
    cross-thread signal - a posted event the move loop does not deliver - so
    every tick found nothing marked dirty and returned.
    """

    def setUp(self):
        self.w = MainWindow()
        self.s = self.w.surface
        self.s._gl_ready = True
        self.s._last_tick_call = 0.0
        self.s.spout.publish = lambda *a, **k: None
        self.s.update = lambda: None
        self.s.repaint = lambda: None
        self.rendered = []
        for p in self.s.players:
            p.render = lambda: self.rendered.append(1) or True

    def test_a_held_tick_renders_with_nothing_marked_dirty(self):
        self.s._dirty = False
        self.s._force_tick = False
        self.s.held_tick()
        self.assertEqual(len(self.rendered), 2)

    def test_a_plain_tick_still_waits_for_a_frame(self):
        # Outside the move loop nothing changes: no frame, no work.
        self.s._dirty = False
        self.s._force_tick = False
        self.s.tick()
        self.assertEqual(self.rendered, [])

    def test_the_hold_reports_frames_drawn(self):
        self.s.set_held(True)
        self.s._last_tick_call = 0.0
        self.s.held_tick()
        self.assertEqual(self.s.set_held(False), 1)
        self.assertEqual(self.s.set_held(False), 0, "reset after reporting")

    def test_the_log_line_carries_the_frame_count(self):
        from chuutube2spout import modalloop

        class _U:
            SetTimer = staticmethod(lambda *a: 1)
            KillTimer = staticmethod(lambda *a: 1)

        t = modalloop.ModalTicker(lambda: None, lambda held: 7, user32=_U())
        keep = []
        with mock.patch("sys.stderr", io.StringIO()) as err:
            for kind in (modalloop.WM_ENTERSIZEMOVE, modalloop.WM_EXITSIZEMOVE):
                msg = modalloop.MSG(0x1234, kind, 0, 0)
                keep.append(msg)
                t.handle(b"windows_generic_MSG", ctypes.addressof(msg))
        self.assertIn("7 frames drawn", err.getvalue())


class TitlePressTests(unittest.TestCase):
    """The timer starts on the title-bar press, not only at the move loop.

    0.9.9.34: frames were drawn while held and K still saw a freeze. If
    Windows waits between the press and WM_ENTERSIZEMOVE, that wait was not
    covered. 0.9.9.35 starts on the press and logs the gap.
    """

    class _User32:
        def __init__(self):
            self.calls = []
            self.button_down = True

        def SetTimer(self, hwnd, ident, ms, proc):        # noqa: N802
            self.calls.append("set")
            return ident

        def KillTimer(self, hwnd, ident):                  # noqa: N802
            self.calls.append("kill")
            return 1

        def GetAsyncKeyState(self, key):                   # noqa: N802
            return -32768 if self.button_down else 0

    def setUp(self):
        from chuutube2spout import modalloop
        self.m = modalloop
        self.u = self._User32()
        self.ticks = []
        self.t = modalloop.ModalTicker(lambda: self.ticks.append(1),
                                       user32=self.u)
        self.keep = []

    def _send(self, kind, wparam=0):
        msg = self.m.MSG(0x1234, kind, wparam, 0)
        self.keep.append(msg)
        return self.t.handle(b"windows_generic_MSG", ctypes.addressof(msg))

    def test_a_title_press_starts_the_timer(self):
        self._send(self.m.WM_NCLBUTTONDOWN, self.m.HTCAPTION)
        self.assertTrue(self.t.active)
        self._send(self.m.WM_TIMER, self.m.TIMER_ID)
        self.assertEqual(self.ticks, [1])

    def test_a_press_elsewhere_does_not(self):
        self._send(self.m.WM_NCLBUTTONDOWN, 20)      # HTCLOSE
        self.assertFalse(self.t.active)

    def test_the_loop_continues_the_same_timer_and_logs_the_gap(self):
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self._send(self.m.WM_NCLBUTTONDOWN, self.m.HTCAPTION)
            self._send(self.m.WM_ENTERSIZEMOVE)
            self._send(self.m.WM_EXITSIZEMOVE)
        self.assertEqual(self.u.calls, ["set", "kill"], "one timer, not two")
        self.assertRegex(err.getvalue(), r"move loop after \d+ms")

    def test_a_click_without_a_loop_stops_when_released(self):
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self._send(self.m.WM_NCLBUTTONDOWN, self.m.HTCAPTION)
            self._send(self.m.WM_TIMER, self.m.TIMER_ID)
            self.assertTrue(self.t.active, "still pressed")
            self.u.button_down = False
            self._send(self.m.WM_TIMER, self.m.TIMER_ID)
        self.assertFalse(self.t.active)
        self.assertIn("no move loop", err.getvalue())

    def test_inside_the_loop_the_button_state_does_not_stop_it(self):
        with mock.patch("sys.stderr", io.StringIO()):
            self._send(self.m.WM_NCLBUTTONDOWN, self.m.HTCAPTION)
            self._send(self.m.WM_ENTERSIZEMOVE)
            self.u.button_down = False
            self._send(self.m.WM_TIMER, self.m.TIMER_ID)
        self.assertTrue(self.t.active, "only WM_EXITSIZEMOVE ends a loop")


class QtMessageTests(unittest.TestCase):
    """Qt's harmless window-size warning reads as a note (K, 0.9.9.37)."""

    TESTER = (
        "QWindowsWindow::setGeometry: Unable to set geometry 620x378-1600+587 "
        "(frame: 636x417-1608+556) on QWidgetWindow/\"QDialogClassWindow\" on "
        "\"DELL S2721DGF\". Resulting geometry: 620x429-1600+587 (frame: "
        "636x468-1608+556) margins: 8, 31, 8, 8 minimum size: 620x378 "
        "MINMAXINFO(maxSize=POINT(x=0, y=0), maxpos=POINT(x=0, y=0), "
        "maxtrack=POINT(x=0, y=0), mintrack=POINT(x=636, y=417)))")

    def setUp(self):
        from chuutube2spout import qtmessages
        self.q = qtmessages

    def test_the_testers_line_becomes_a_labelled_note(self):
        line = self.q.rewrite(self.TESTER)
        self.assertTrue(line.startswith("[qt] note, harmless:"))
        self.assertIn("620x378 asked, 620x429 given", line)
        self.assertNotIn("Unable", line)

    def test_other_messages_pass_unchanged(self):
        for msg in ("QOpenGLWidget: Failed to create context",
                    "qt.qpa.window: SetProcessDpiAwarenessContext() failed",
                    ""):
            with self.subTest(msg=msg):
                self.assertEqual(self.q.rewrite(msg), msg)

    def test_the_handler_writes_to_the_log(self):
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.q._handler(None, None, self.TESTER)
        self.assertIn("[qt] note, harmless", err.getvalue())

    def test_installed_before_the_qapplication(self):
        source = Path(app_mod.__file__).read_text(encoding="utf-8") \
            if "app_mod" in globals() else \
            (ROOT / "chuutube2spout" / "app.py").read_text(encoding="utf-8")
        self.assertLess(source.index("install_qt_messages()"),
                        source.index("app = QApplication(argv)"))


class QtNoteInSetupWindowTests(unittest.TestCase):
    """The note also reaches the setup window, not only the log (K)."""

    def setUp(self):
        from chuutube2spout import qtmessages
        self.q = qtmessages
        self.seen = []
        self.q.add_listener(self.seen.append)
        self.addCleanup(self.q.remove_listener, self.seen.append)

    def test_a_note_is_passed_to_listeners(self):
        with mock.patch("sys.stderr", io.StringIO()):
            self.q._handler(None, None, QtMessageTests.TESTER)
        self.assertEqual(len(self.seen), 1)
        self.assertTrue(self.seen[0].startswith("[qt] note, harmless:"))

    def test_other_qt_messages_stay_in_the_log_only(self):
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.q._handler(None, None, "QOpenGLWidget: something")
        self.assertEqual(self.seen, [])
        self.assertIn("QOpenGLWidget: something", err.getvalue())

    def test_a_failing_listener_does_not_break_logging(self):
        self.q.add_listener(lambda text: 1 / 0)
        self.addCleanup(self.q._listeners.clear)
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.q._handler(None, None, QtMessageTests.TESTER)
        self.assertIn("[qt] note", err.getvalue())
        self.assertEqual(len(self.seen), 1)

    def test_the_setup_window_listens_only_while_open(self):
        source = (ROOT / "chuutube2spout" / "firstrun.py").read_text(
            encoding="utf-8")
        body = source.split("qtmessages.add_listener(forward)")[1]
        self.assertIn("dialog.exec()", body.split("finally:")[0])
        self.assertIn("qtmessages.remove_listener(forward)",
                      body.split("finally:")[1])
        self.assertIn("notes.text.connect(append, Qt.QueuedConnection)",
                      source)


class StaleResumeTests(unittest.TestCase):
    """A finished clip in the cache no longer walks the chain into 416s.

    Tester, 0.9.9.35: the same video under another link (`&pp=...`) missed the
    cache, yt-dlp resumed the finished file past its end and got HTTP 416 from
    every client - 19 s, and once a 240p file from a different client.
    """

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="chuutube-416-")
        source = Path(__file__).resolve().parent / "fake_resume416_ytdlp.py"
        cls.tool = str(Path(cls.dir) / "resume_ytdlp.py")
        shutil.copy(source, cls.tool)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="chuutube-cache-")
        self.addCleanup(shutil.rmtree, self.cache, True)
        for p in (mock.patch.object(downloader, "ytdlp_command",
                                    lambda *a, **k: [sys.executable, self.tool]),
                  mock.patch.object(downloader, "CACHE_MANIFEST", False),
                  mock.patch.object(downloader, "fresh_info", lambda *a: None),
                  mock.patch.object(downloader, "cookie_args", lambda: []),
                  mock.patch("sys.stderr", io.StringIO())):
            p.start()
            self.addCleanup(p.stop)

    def test_restarts_the_same_client_from_zero_at_once(self):
        result = downloader.download("https://example/watch", 1080, self.cache)
        self.assertTrue(result.video_path.endswith("clip.f137.mp4"))
        invocations = int((Path(self.cache) / "_invocations").read_text())
        self.assertEqual(invocations, 2, "one 416, then one fresh run")
        args = (Path(self.cache) / "_args").read_text(encoding="utf-8")
        self.assertIn("--no-continue", args)
        self.assertNotIn("player_client=mweb", args,
                         "same client as the first try, not the next one")


class CacheKeyTests(unittest.TestCase):
    """The same YouTube video is one cache entry, whatever the link says."""

    SAME = ("https://www.youtube.com/watch?v=Xl4fZSY8CFI",
            "https://www.youtube.com/watch?v=Xl4fZSY8CFI&pp=ygUKaGFtdGFybyBvcA%3D%3D",
            "https://youtube.com/watch?feature=share&v=Xl4fZSY8CFI",
            "https://m.youtube.com/watch?v=Xl4fZSY8CFI&t=42",
            "https://youtu.be/Xl4fZSY8CFI?si=abcdef",
            "https://www.youtube.com/shorts/Xl4fZSY8CFI",
            "https://www.youtube.com/embed/Xl4fZSY8CFI",
            "https://music.youtube.com/watch?v=Xl4fZSY8CFI&list=RDabc")

    def test_every_spelling_is_one_key(self):
        keys = {downloader.cache_key(u) for u in self.SAME}
        self.assertEqual(keys, {"youtube:Xl4fZSY8CFI"})

    def test_manifest_and_info_follow_it(self):
        cache = Path("/tmp/x")
        first, second = self.SAME[0], self.SAME[1]
        self.assertEqual(downloader.manifest_path(cache, first, 1080),
                         downloader.manifest_path(cache, second, 1080))
        self.assertEqual(downloader.info_path(cache, first),
                         downloader.info_path(cache, second))
        self.assertNotEqual(downloader.manifest_path(cache, first, 1080),
                            downloader.manifest_path(cache, first, 720),
                            "quality still matters")

    def test_other_links_key_on_themselves(self):
        for url in ("https://example.com/clip.mp4",
                    "https://notyoutube.com/watch?v=Xl4fZSY8CFI",
                    "https://www.youtube.com/watch?v=short",
                    "D:\\clips\\loop.mp4", ""):
            with self.subTest(url=url):
                self.assertEqual(downloader.cache_key(url), url)

    def test_different_videos_stay_apart(self):
        self.assertNotEqual(
            downloader.cache_key("https://youtu.be/Xl4fZSY8CFI"),
            downloader.cache_key("https://youtu.be/1CBzd8KrKLo"))


class LetterboxGridTests(unittest.TestCase):
    """Around a clip of another shape the preview shows the grid (0.9.9.38).

    At the clip's own size, the part of a 16:9 cell a 4:3 clip does not cover
    is not sent at all, so the preview shows it as transparent, not black.
    """

    def _paint(self, size):
        w = MainWindow()
        s = w.surface
        _sixteen_by_nine_cells(s)
        s._checker = surface_mod.Checker()
        for p in s.players:
            p.target.fbo, (p.target.w, p.target.h) = 5, size
            p.empty = False
        grids, blits = [], []
        s._draw_grid = lambda *a: grids.append(a)
        gl = mock.MagicMock()
        gl.glBlitFramebuffer.side_effect = lambda *a: blits.append(a)
        with mock.patch.object(surface_mod, "GL", gl):
            s._paint()
        return grids, blits

    def test_a_4_3_clip_gets_the_grid_behind_it(self):
        grids, blits = self._paint((1440, 1080))
        self.assertEqual(len(grids), 2)
        self.assertEqual(len(blits), 2, "and the clip on top")

    def test_a_clip_that_fills_the_cell_does_not(self):
        grids, _ = self._paint((1920, 1080))
        self.assertEqual(grids, [])

    def test_rounding_is_not_a_bar(self):
        self.assertFalse(surface_mod._leaves_room((0, 0, 639, 360),
                                                  (0, 0, 640, 360)))
        self.assertTrue(surface_mod._leaves_room((80, 0, 560, 360),
                                                 (0, 0, 640, 360)))


class TransparentBarsTests(unittest.TestCase):
    """Fixed canvas + SENDER_BACKGROUND=transparent really is transparent.

    K, 0.9.9.38: with SENDER_WIDTH=1920, SENDER_HEIGHT=1080 and
    SENDER_BACKGROUND=transparent a 4:3 clip still reached Resolume with black
    bars, and the preview showed them black. The bars are now cleared by us
    after mpv draws, to the colour asked for.
    """

    def _player(self, osd=None):
        p = player_mod.Player("A")
        p._vid_w, p._vid_h = 960, 720
        p.target.fbo, p.target.w, p.target.h = 5, 1920, 1080
        p._osd = osd
        return p

    def test_the_picture_rect_comes_from_mpv(self):
        p = self._player(osd=(1920, 1080, 240, 240, 0, 0))
        self.assertEqual(p.picture_rect(), (240, 0, 1680, 1080))

    def test_without_mpvs_numbers_it_works_them_out(self):
        p = self._player()
        self.assertEqual(p.picture_rect(), (240, 0, 1680, 1080))

    def test_mpv_numbers_for_another_size_are_not_used(self):
        p = self._player(osd=(1280, 720, 160, 160, 0, 0))
        self.assertEqual(p.picture_rect(), (240, 0, 1680, 1080))

    def test_no_bars_no_rect(self):
        p = self._player()
        p._vid_w, p._vid_h = 1920, 1080
        self.assertIsNone(p.picture_rect())

    def test_letterbox_top_and_bottom(self):
        p = self._player(osd=(1920, 1080, 0, 0, 60, 60))
        self.assertEqual(p.picture_rect(), (0, 60, 1920, 1020))

    def _render(self, background, canvas_size=(1920, 1080)):
        p = self._player(osd=(1920, 1080, 240, 240, 0, 0))
        p._render_ctx = mock.MagicMock()
        p._render_ctx.update.return_value = True
        p.target.ensure = lambda w, h: True
        gl = mock.MagicMock()
        cleared = []
        gl.glScissor.side_effect = lambda *a: cleared.append(a)
        colours = []
        gl.glClearColor.side_effect = lambda *c: colours.append(c)
        with mock.patch.object(player_mod, "GL", gl), \
                mock.patch.object(player_mod, "SENDER_BACKGROUND", background), \
                mock.patch.object(player_mod, "canvas", lambda: canvas_size), \
                mock.patch.object(player_mod, "canvas_for",
                                  lambda w, h: canvas_size), \
                mock.patch.object(player_mod, "background_rgba",
                                  lambda: (0.0, 0.0, 0.0, 0.0)):
            self.assertTrue(p.render())
        return cleared, colours

    def test_transparent_bars_are_cleared_after_mpv_draws(self):
        cleared, colours = self._render("transparent")
        self.assertIn((0, 0, 240, 1080), cleared, "left bar")
        self.assertIn((1680, 0, 240, 1080), cleared, "right bar")
        self.assertEqual(colours, [(0.0, 0.0, 0.0, 0.0)])

    def test_black_bars_are_left_to_mpv(self):
        cleared, _ = self._render("black")
        self.assertEqual(cleared, [])

    def test_at_the_clips_own_size_there_is_nothing_to_clear(self):
        cleared, _ = self._render("transparent", canvas_size=(0, 0))
        self.assertEqual(cleared, [])

    def test_the_preview_shows_the_grid_in_transparent_bars(self):
        w = MainWindow()
        s = w.surface
        _sixteen_by_nine_cells(s)
        s._checker = surface_mod.Checker()
        for p in s.players:
            p.target.fbo, p.target.w, p.target.h = 5, 1920, 1080
            p._vid_w, p._vid_h = 960, 720
            p._osd = (1920, 1080, 240, 240, 0, 0)
            p.empty = False
        grids, blits = [], []
        s._draw_grid = lambda *a: grids.append(a)
        gl = mock.MagicMock()
        gl.glBlitFramebuffer.side_effect = lambda *a: blits.append(a)
        with mock.patch.object(surface_mod, "GL", gl), \
                mock.patch.object(surface_mod, "SENDER_BACKGROUND",
                                  "transparent"), \
                mock.patch.object(surface_mod, "canvas", lambda: (1920, 1080)):
            s._paint()
        self.assertEqual(len(grids), 2)
        self.assertEqual(blits[0][:4], (240, 0, 1680, 1080),
                         "only the picture is blitted, not the bars")

    def test_the_rect_maps_into_the_cell(self):
        self.assertEqual(surface_mod._map_rect((240, 0, 1680, 1080), 1920, 1080,
                                               (0, 0, 640, 360)),
                         (80, 0, 560, 360))


class DeveloperSettingsIsolationTests(unittest.TestCase):
    """The tests never read the developer's own settings.txt (0.9.9.40).

    K's build stopped on eight failures: his settings.txt in the project
    folder set SENDER_WIDTH=1920, SENDER_HEIGHT=1080 and
    SENDER_BACKGROUND=transparent, and the tests read it as the defaults.
    """

    def test_no_settings_file_is_read_by_the_tests(self):
        self.assertEqual(os.environ.get("CHUUTUBE2SPOUT_SETTINGS_FILE"), "none")
        self.assertIsNone(config._settings_path())
        self.assertIsNone(config.SETTINGS_FILE)

    def test_a_settings_file_in_the_project_folder_is_ignored(self):
        planted = ROOT / "settings.txt"
        if planted.exists():
            self.skipTest("a real settings.txt is there; the check above covers it")
        planted.write_text("SENDER_WIDTH=1920\nSENDER_HEIGHT=1080\n",
                           encoding="utf-8")
        try:
            cfg = importlib.reload(config)
            self.assertEqual((cfg.SENDER_WIDTH, cfg.SENDER_HEIGHT), (0, 0))
        finally:
            planted.unlink()
            importlib.reload(config)

    def test_the_override_can_name_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "other.txt"
            path.write_text("LOOP=0\n", encoding="utf-8")
            with mock.patch.dict(os.environ,
                                 {"CHUUTUBE2SPOUT_SETTINGS_FILE": str(path)}):
                self.assertEqual(config._settings_path(), path)
            with mock.patch.dict(os.environ,
                                 {"CHUUTUBE2SPOUT_SETTINGS_FILE": str(path) + "x"}):
                self.assertIsNone(config._settings_path())
