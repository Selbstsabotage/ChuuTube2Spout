# SPDX-License-Identifier: GPL-3.0-or-later
"""Runs a proof-of-origin token server so YouTube serves whole files.

yt-dlp's verbose output says the quiet part out loud:

    [youtube] [pot] PO Token Providers: none
    [youtube] Detected experiment to bind GVS PO Token to video ID
    [youtube] YouTube is forcing SABR streaming for this client

Without a PO token, YouTube hands out an opening chunk of a video and then
answers 403 to everything after it. That is not a bug in the player, the
headers, the ranges or the IP - all of which were investigated at length - it
is a deliberate gate, and the token is the key to it.

The generator is the plugin's own TypeScript, from the same release as the
plugin, compiled once into `build/` and run by Node.js. We run its server as a
child process for the lifetime of the app and point yt-dlp at it; if someone
already has one running on the port, we use theirs instead. Script mode - the
plugin starting Node once per token - is the fallback, and the separate Rust
executable (bgutil-pot.exe) a spare behind that.

Node replaced deno in 1.1. deno ran the TypeScript as it was; Node needs it
built first (`npm ci`, then `tsc`), which is what install_script_deps does.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .bootstrap import find_node, node_major, npm_cli, project_root
from .config import POT_VERBOSE, TOKEN_SERVER
from .redact import scrub

DEFAULT_PORT = 4416
START_TIMEOUT = 45.0
# How long a download may wait for the one-off build of the generator (npm ci,
# then tsc) to finish. The alternative is fetching that clip without a token,
# i.e. at 360p.
SCRIPT_PREP_TIMEOUT = 900.0
EXE_NAMES = ("bgutil-pot.exe", "bgutil-pot",
             "bgutil-pot-windows-x86_64.exe")

# Bind explicitly to IPv4 loopback. The generator defaults to "::", and Windows
# sockets are IPv6-only unless told otherwise - so the server comes up perfectly
# well and is still unreachable at 127.0.0.1, which is exactly where the yt-dlp
# plugin knocks.
BIND_HOST = "127.0.0.1"
PROBE_HOSTS = ("127.0.0.1", "::1")

# The server colours its logs; a Windows console shows the escapes raw.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def server_version(exe: str) -> str | None:
    """Version the generator reports, so the plugin can be matched to it."""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True,
                             text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        match = re.search(r"(\d+\.\d+\.\d+)", (out.stdout or "") + (out.stderr or ""))
        return match.group(1) if match else None
    except Exception:
        return None


def plugin_version() -> str | None:
    """Version of the yt-dlp plugin that talks to the generator."""
    try:
        from importlib.metadata import version
        return version("bgutil-ytdlp-pot-provider")
    except Exception:
        return None


def versions_agree(plugin: str | None, server: str | None) -> bool:
    """The plugin refuses to work across a major-version gap - so check it.

    This is not a nicety: a mismatch makes the plugin decline to request a
    token at all, and the only symptom downstream is another 403.
    """
    if not plugin or not server:
        return True          # nothing to compare; do not cry wolf
    return plugin.split(".")[0] == server.split(".")[0]


# The generator's entry point, newest naming first.
#
# 2.0.0 merged the one-shot generator and the server into a single `main.ts`;
# up to 1.3.1 the one-shot was its own `generate_once.ts`. Both spellings are
# accepted because an installed tree is whatever setup.ps1 last fetched, and a
# tester running last week's build must not be broken by this week's rename.
GENERATOR_NAMES = ("main.ts", "generate_once.ts")


def generator_script(server: Path | str) -> Path | None:
    """The generator SOURCE inside a `server` directory, whichever release wrote it.

    Says that a tree is there. Whether it can run is built_server/built_script.
    """
    src = Path(server) / "src"
    for name in GENERATOR_NAMES:
        candidate = src / name
        if candidate.is_file():
            return candidate
    return None


# What Node runs: the TypeScript compiled by tsc into build/ (tsconfig.json's
# outDir). The names are not ours to choose. The plugin's Node provider runs
# exactly build/generate_once.js (BgUtilScriptNodePTP._SCRIPT_BASENAME), and
# the server is main.ts compiled, as in the generator's own Dockerfile.
BUILD_DIR = "build"
BUILT_SERVER = "main.js"
BUILT_SCRIPT = "generate_once.js"


def built_server(server: Path | str) -> Path | None:
    candidate = Path(server) / BUILD_DIR / BUILT_SERVER
    return candidate if candidate.is_file() else None


def built_script(server: Path | str) -> Path | None:
    candidate = Path(server) / BUILD_DIR / BUILT_SCRIPT
    return candidate if candidate.is_file() else None


# Written into node_modules after a probe has actually run the generator. The
# folder existing is not evidence: an install that dies half way leaves a
# node_modules behind that cannot resolve.
READY_MARKER = ".chuutube-ready"

# What an install says when the drive cannot make the links node_modules is
# built from. Seen with deno on a tester's Y: drive: "failed to create junction
# '...' to '...': Incorrect function. (os error 1)" - ERROR_INVALID_FUNCTION:
# the driver behind the drive letter does not implement the request.
#
# npm, since 1.1, lays node_modules out as plain folders and files, so this
# should no longer happen. Kept so that if any drive does refuse, the build
# still has somewhere else to go.
#
# The tester's drive was exFAT, which has no reparse points at all. Keyed on
# the error rather than on the filesystem anyway: FAT32, most network shares
# and some sync or encryption drives refuse the same request, and asking the
# filesystem its name is a second guess at a question the error already
# answered.
LINK_FAILURE = ("junction", "symlink", "reparse", "incorrect function")


def local_server_root() -> Path | None:
    """Where the generator goes when the app's own folder cannot hold it.

    %LOCALAPPDATA% is on the system drive, where Windows itself depends on
    junctions working. The app's own folder can be anywhere a VJ keeps their
    tools, and some of those places refuse them - see LINK_FAILURE.
    """
    base = os.getenv("LOCALAPPDATA")
    if not base:
        return None
    return Path(base) / "ChuuTubePoi" / "bgutil-server"


def is_ready(server: Path | str) -> bool:
    """Probed OK, and built for Node.

    The build half matters on the first start after 1.1: a tree deno had
    probed still carries the marker, but has no build/ for Node to run. Taken
    as ready, it would win over every other tree and never start.
    """
    return ((Path(server) / "node_modules" / READY_MARKER).is_file()
            and built_server(server) is not None
            and built_script(server) is not None)


def mark_ready(server: Path | str) -> None:
    node_modules = Path(server) / "node_modules"
    if not node_modules.is_dir():
        return
    try:
        (node_modules / READY_MARKER).write_text(
            "probed OK by ChuuTubePoi\n", encoding="utf-8")
    except OSError:
        pass                  # read-only, or gone - it will be probed again


def _trees_under(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    found = []
    for candidate in [base] + sorted(base.glob("bgutil-ytdlp-pot-provider*")):
        server = candidate / "server"
        if generator_script(server) is not None:
            found.append(server)
    return found


def script_trees() -> list[Path]:
    """Every generator tree we could use: beside the app first, then local."""
    root_dir = project_root()
    trees: list[Path] = []
    for base in (root_dir / "bgutil-server", root_dir / "bin" / "bgutil-server"):
        trees += _trees_under(base)
    local = local_server_root()
    if local is not None:
        trees += _trees_under(local)
    return trees


def find_script_home() -> str | None:
    """Source tree for the plugin's own script-mode generator, if present.

    This is the version-matched option: the plugin and the generator come from
    the same release, so they cannot disagree about protocol. Node runs it
    from build/, compiled on this machine.

    A tree that has been proven to work wins over one that has not, wherever
    it is. Otherwise a copy relocated to %LOCALAPPDATA% because the app's drive
    refused the links would lose, on every launch, to the unbuildable original
    beside the app - and each launch would spend a minute failing to build it
    again before finding the copy that works.
    """
    trees = script_trees()
    for tree in trees:
        if is_ready(tree):
            return str(tree)
    return str(trees[0]) if trees else None


def relocate_script_home(script_home: str) -> str | None:
    """Copy a generator tree - source only - to local_server_root().

    Returns the new server directory, or None if there is nowhere to put it or
    the tree is already there. node_modules is left behind: it is the part that
    could not be built, and a half-built one is worse than none.
    """
    root = local_server_root()
    if root is None:
        return None
    source = Path(script_home)
    target = root / source.parent.name / "server"
    try:
        if target.resolve() == source.resolve():
            return None
    except OSError:
        pass
    try:
        shutil.copytree(source, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("node_modules"))
    except (OSError, shutil.Error) as exc:
        print(f"[pot] could not copy the generator to {target}: {exc}",
              file=sys.stderr)
        return None
    return str(target)


def script_server_command(script_home: str) -> list[str] | None:
    """Node running the generator's own server, or None if this tree has none.

    2.0.0 merged the one-shot generator and the server into a single main.ts,
    which is what makes this possible: the server is now the same code, from
    the same release, as the plugin that talks to it. Version-matched by
    construction - unlike bgutil-pot.exe, which is a separate Rust port on its
    own numbering (0.8.x against the plugin's 2.x) and is refused by the very
    compatibility check that exists to stop mismatches.

    Only for 2.x trees: 1.3.1's generate_once.ts answers one request and exits.
    And only once built: Node cannot run the TypeScript source.
    """
    script = generator_script(script_home)
    if script is None or script.name != "main.ts":
        return None
    server = built_server(script_home)
    if server is None:
        return None
    node = find_node()
    if not node:
        return None
    return [node, *server_args(script_home, node_major(node)), str(server)]


def find_executable() -> str | None:
    """The token generator, preferring a copy next to the app (portability)."""
    root = project_root()
    for folder in (root, root / "bin"):
        for name in EXE_NAMES:
            candidate = folder / name
            if candidate.is_file():
                return str(candidate)
    for name in EXE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def port_is_open(port: int, host: str | None = None, timeout: float = 0.4) -> bool:
    """Is anything listening? Checks both loopback families unless told one."""
    for candidate in ((host,) if host else PROBE_HOSTS):
        try:
            with socket.create_connection((candidate, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


class PotProvider:
    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self._process: Optional[subprocess.Popen] = None
        self._external = False
        # Which server is answering: "node" (the generator's own, version-matched
        # by construction), "exe" (the separate Rust port) or "external"
        # (someone else's, already on the port). compatible() cannot ask a
        # version question of the first one, because there is no gap to check.
        self._server_kind: str | None = None
        self._script_home: str | None = None
        self._script_ok: bool | None = None
        self._preparing = False
        self._prepared = threading.Event()
        # _start_server can now be reached from the prepare thread as well as
        # the GUI thread, and the two used to race over self._process.
        self._server_lock = threading.Lock()
        self._log: list[str] = []
        self.status = "not started"

    @property
    def server_kind(self) -> str | None:
        """Which server is answering: "node", "exe", "external", or None.

        Public because the diagnostics have to report what the app will
        actually use, and guessing wrong is how a working setup got a red FAIL.
        """
        return self._server_kind

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        """Is there a *server process* answering on the port?"""
        return self._external or (self._process is not None
                                  and self._process.poll() is None)

    @property
    def active(self) -> bool:
        """Will a PO token actually be issued?

        Not the same question as `running`, and conflating the two put a red
        "no PO token server" warning across the bottom of a window that was
        happily fetching 1080p. In script mode there is no server and never will
        be: yt-dlp runs the generator itself, per request, via Node. Anything
        asking "are we gated?" wants this, not a process check.
        """
        return (self._script_home is not None and self._script_ok is not False
                ) or self.running

    def compatible(self) -> bool:
        """Will the installed plugin actually talk to this server?

        Pointing yt-dlp at a server the plugin refuses is worse than pointing
        it at nothing: you get a wall of warnings and the same failure.
        """
        if self._server_kind == "node":
            # Same release as the plugin, out of the same directory. There is
            # no version pair to compare.
            return True
        exe = find_executable()
        if not exe:
            return False
        return versions_agree(plugin_version(), server_version(exe))

    def extractor_args(self) -> list[str]:
        """yt-dlp flags pointing the plugin at whichever generator we have.

        Only a *verified* script home is advertised. Handing the plugin a
        server_home whose dependencies do not resolve is worse than handing it
        nothing: the plugin reports itself available, yt-dlp asks it for a
        token, the Node run fails, and the only visible consequence is a 360p
        download - which is indistinguishable from having no provider at all.
        """
        # Whichever we actually have, server first for the same reason start()
        # tries it first. Asking the plugin to use a server that is not running
        # is what produced a wall of warnings and a 360p download.
        if self.running and self.compatible():
            return ["--extractor-args",
                    f"youtubepot-bgutilhttp:base_url={self.base_url}"]
        if self._script_home:
            if self._script_ok is None:
                print("[pot] waiting for the token generator to finish "
                      "preparing", file=sys.stderr)
                self._prepared.wait(SCRIPT_PREP_TIMEOUT)
            if self._script_ok:
                return ["--extractor-args",
                        f"youtubepot-bgutilscript:server_home={self._script_home}"]
        if not self.running or not self.compatible():
            return []
        return ["--extractor-args",
                f"youtubepot-bgutilhttp:base_url={self.base_url}"]

    def start(self) -> bool:
        if self.running:
            return True

        # The server first, when we have a usable one. It is the same token
        # either way; the difference is that script mode pays for a Node
        # process on every single request and the server has already paid.
        # _start_server refuses a version-mismatched executable and returns
        # False, so this degrades to script mode rather than to no token.
        if TOKEN_SERVER and self._start_server():
            return True

        script_home = find_script_home()
        if script_home:
            if self._preparing or self._script_ok is not None:
                # Already checked, or a check is running. start() is called
                # from every window that opens; re-probing each time meant a
                # fresh Node run - and, on a cold tree, a fresh build.
                return True
            self._preparing = True
            self._script_home = script_home
            self._script_ok = None
            self._prepared.clear()
            self.status = f"checking script mode: {script_home}"
            print(f"[pot] {self.status}", file=sys.stderr)
            threading.Thread(target=self._prepare_script, args=(script_home,),
                             daemon=True, name="pot-prepare").start()
            return True

        return self._start_server()

    def wait_settled(self, timeout: float = SCRIPT_PREP_TIMEOUT) -> None:
        """Wait until script mode is checked and the server has had its chance.

        start() returns at once: script mode is checked in the background, and
        the server is started only after that check passes. Anything that
        reports what the app uses (the diagnostics) has to wait for both, or it
        reports the first second - "script mode", on a setup where the server
        comes up a moment later. Idempotent: if the server is already
        running, _start_server returns at once.
        """
        if self._script_home is None and not self._preparing:
            return
        self._prepared.wait(timeout)
        if self._script_ok and TOKEN_SERVER:
            self._start_server()

    def _prepare_script(self, script_home: str) -> None:
        """Verify script mode really works, repairing it once if it does not.

        Nothing of the generator is in a release: setup fetches its source,
        and its `node_modules` and `build/` are made on the machine that will
        use them - by setup, or here if setup could not finish. A one-off few
        minutes, and the same npm ci and tsc that setup.ps1 runs.
        """
        ok, detail = script_probe(script_home)
        if not ok:
            self.status = "preparing the token generator (a few minutes)"
            print(f"[pot] {self.status}", file=sys.stderr)
            installed, install_detail = install_script_deps(script_home)
            if not installed:
                print(f"[pot] building the token generator failed: "
                      f"{install_detail}", file=sys.stderr)
                if any(word in install_detail.lower() for word in LINK_FAILURE):
                    moved = self._relocate(script_home)
                    if moved:
                        script_home = moved
            ok, detail = script_probe(script_home)

        if ok:
            mark_ready(script_home)
            self._script_home = script_home
            self._script_ok = True
            self._preparing = False
            self.status = f"script mode ready: {script_home}"
            print(f"[pot] {self.status}", file=sys.stderr)
            self._prepared.set()
            self._after_ready()
            return

        # Do not keep advertising it. _start_server says for itself what it
        # finds: this used to announce "falling back to the standalone token
        # server" on machines that had no standalone server, and then start
        # the Node one that had just failed, which failed again the same way.
        self._script_ok = False
        self._script_home = None
        self._preparing = False
        self._prepared.set()
        print(f"[pot] script mode is not usable: {detail}", file=sys.stderr)
        self._start_server()

    def _relocate(self, script_home: str) -> str | None:
        """Build the generator on the system drive instead. None if that fails."""
        target = relocate_script_home(script_home)
        if not target:
            return None
        drive = Path(script_home).drive or "the app's drive"
        print(f"[pot] {drive} refused to create the links node_modules needs "
              "(\"Incorrect function\") - exFAT and FAT32 drives cannot make "
              f"them. Building it in {target} instead.", file=sys.stderr)
        installed, detail = install_script_deps(target)
        if not installed:
            print(f"[pot] building the token generator failed there too: "
                  f"{detail}", file=sys.stderr)
            return None
        return target

    def _after_ready(self) -> None:
        """Script mode works - so the server built from the same tree will too.

        start() tries the server before anything else, but on a fresh unzip the
        tree has not been built yet, so that first attempt cannot work - and
        until now nothing tried again, so the whole first session ran in the
        slower per-request script mode. Try now that it can.
        """
        if TOKEN_SERVER:
            self._start_server()

    def _start_server(self) -> bool:
        with self._server_lock:
            return self._start_server_locked()

    def _start_server_locked(self) -> bool:
        if self.running:
            return True

        # Someone else's server on the port is just as good as ours.
        if port_is_open(self.port):
            self._external = True
            self._server_kind = "external"
            self.status = f"using the server already on port {self.port}"
            print(f"[pot] {self.status}", file=sys.stderr)
            return True

        # The generator's own server first: same release as the plugin, so no
        # version gap can open up. The separate executable is the fallback.
        script_home = find_script_home()
        kind = "exe"
        command = None
        # Only a tree a probe has run. An unbuilt one cannot start - "Cannot
        # find module 'express'" - and trying anyway (with deno) put an error at
        # the top of every first run's log, which a tester reasonably read as
        # the runtime being broken. _prepare_script builds it and then calls
        # back in here.
        if script_home and is_ready(script_home):
            command = script_server_command(script_home)
            if command is not None:
                kind = "node"
                command += ["--host", BIND_HOST, "--port", str(self.port)]

        if command is None:
            exe_check = find_executable()
            if exe_check and not versions_agree(plugin_version(),
                                                server_version(exe_check)):
                # Starting it anyway just produces a warning per request and no
                # token, because the plugin refuses to talk to it.
                self.status = ("token generator is a mismatched version - not "
                               "started. Script mode will be used instead.")
                print(f"[pot] {self.status}", file=sys.stderr)
                return False

            exe = exe_check
            if not exe:
                if script_home and self._script_ok is None:
                    # Not missing - not built yet. _prepare_script is about to
                    # (or already is), and starts the server when it has.
                    self.status = "token generator not prepared yet"
                    return False
                if script_home and self._script_ok:
                    # Script mode works; only the server did not start (its
                    # own exit is logged above). Tokens still come, slower.
                    self.status = ("script mode issues the tokens - the "
                                   "token server could not be started")
                elif script_home:
                    self.status = ("no token generator is running: its "
                                   "dependencies could not be built (see "
                                   "above). Most clips still download; some "
                                   "may be throttled or refused.")
                else:
                    self.status = ("no token generator found - YouTube will "
                                   "refuse anything past the first few MB. "
                                   "Re-run setup-runtime.bat (setup.ps1 "
                                   "from source).")
                print(f"[pot] {self.status}", file=sys.stderr)
                return False
            command = [exe, "server", "--host", BIND_HOST,
                       "--port", str(self.port)]
            if POT_VERBOSE:
                command.append("--verbose")

        exe = command[0]
        print(f"[pot] starting the {kind} token server on "
              f"{BIND_HOST}:{self.port}", file=sys.stderr)
        try:
            self._process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=dict(os.environ, NO_COLOR="1"),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:
            self.status = f"could not start {Path(exe).name}: {exc}"
            print(f"[pot] {self.status}", file=sys.stderr)
            return False

        # Forward whatever it says. Discarding this output is how "did not come
        # up" stayed a mystery instead of being a message.
        self._server_kind = kind
        self._log = []
        threading.Thread(target=self._drain, daemon=True, name="pot-log").start()

        # Hold the handle locally: self._process is cleared on failure, and
        # reading it again after that raised AttributeError on None.
        proc = self._process
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                detail = "; ".join(self._log[-3:]) or "no output"
                self.status = (f"{Path(exe).name} exited "
                               f"(code {proc.returncode}): {detail}")
                print(f"[pot] {self.status}", file=sys.stderr)
                if kind == "node" and any(ACCESS_DENIED in line
                                          for line in self._log):
                    print(f"[pot] {SANDBOX_REFUSED}", file=sys.stderr)
                self._process = None
                self._server_kind = None
                return False
            if port_is_open(self.port):
                self.status = (f"{self._server_kind} token server ready on "
                               f"{BIND_HOST}:{self.port}")
                print(f"[pot] {self.status}", file=sys.stderr)
                return True
            time.sleep(0.25)

        detail = "; ".join(self._log[-3:]) or "it printed nothing"
        self.status = (f"no answer on {BIND_HOST}:{self.port} after "
                       f"{START_TIMEOUT:.0f}s ({detail})")
        print(f"[pot] {self.status}", file=sys.stderr)
        self.stop()
        return False

    def _drain(self) -> None:
        """Forward the server's output line by line, and keep the last of it."""
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                line = scrub(_ANSI.sub("", line).rstrip())
                if not line:
                    continue
                print(f"[pot] {line}", file=sys.stderr)
                self._log.append(line)
                del self._log[:-20]
        except Exception:
            pass

    def stop(self) -> None:
        self._external = False
        self._server_kind = None
        self._script_home = None
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def script_cache_dir(script_home: str) -> str:
    """Where the generator caches, mirroring the plugin's own rule.

    Copied deliberately from BgUtilScriptPTPBase._script_cache_dir. If our probe
    grants a different directory from the one yt-dlp grants, the probe answers a
    question nobody asked.
    """
    xdg = os.getenv("XDG_CACHE_HOME")
    if xdg is not None:
        return os.path.abspath(os.path.join(xdg, "bgutil-ytdlp-pot-provider"))
    home = os.getenv("HOME") or os.getenv("USERPROFILE")
    if home:
        return os.path.abspath(
            os.path.join(home, ".cache", "bgutil-ytdlp-pot-provider"))
    return script_home


# Node's permission model refused the server something. The code it prints.
ACCESS_DENIED = "ERR_ACCESS_DENIED"
SANDBOX_REFUSED = ("Node's permission model refused the token server "
                   "something it needs (ERR_ACCESS_DENIED above). Script mode "
                   "is used instead, which works but runs without that limit. "
                   "Please report this line.")


# From this Node.js version on, --permission blocks the network too, and
# --allow-net (a plain switch) grants it. Older versions do not know the flag
# and refuse to start with it. Seen on K's first 1.1 run with Node 26: "Access
# to this API has been restricted. Use --allow-net to manage permissions."
NODE_NET_PERMISSION_MAJOR = 25


def server_args(script_home: str, major: int | None = None) -> list[str]:
    """Node's arguments for the server we start: the permission model, scoped.

    This runs third-party JavaScript. deno ran it with file access limited to
    node_modules and the cache; Node has no such limit unless asked. So the
    server may read its own tree (build, node_modules) and read and write the
    cache, nothing else: no other files, no child processes, no native addons.
    Network is allowed - minting a token is a network job - with --allow-net
    on a Node that has it (`major`, 25 or newer; unknown counts as new, since
    the Node setup installs is).

    Script mode cannot have this. The plugin starts Node itself, with no
    arguments of ours, so it runs the same code unlimited. That is the
    fallback, used when the server is not running.

    Paths as separate flags, one each: Node 23 stopped splitting the value on
    commas, so `--allow-fs-read=a,b` would mean one path called "a,b".
    """
    cache = script_cache_dir(script_home)
    home = os.path.abspath(script_home)
    args = ["--permission",
            f"--allow-fs-read={home}",
            f"--allow-fs-read={cache}",
            f"--allow-fs-write={cache}"]
    if major is None or major >= NODE_NET_PERMISSION_MAJOR:
        # Node 26 calls the flag experimental and says so on every start
        # ("ExperimentalWarning: The flag --allow-net is under experimental
        # phase", K's log). Harmless, but it reads like a fault in a log a
        # tester sends in. Only that category is silenced; Node 22+ knows
        # --disable-warning.
        args += ["--allow-net", "--disable-warning=ExperimentalWarning"]
    return args


def script_env() -> dict:
    env = dict(os.environ)
    env["FORCE_COLOR"] = "false"
    env["NO_COLOR"] = "1"
    return env


def script_probe(script_home: str) -> tuple[bool, str]:
    """Run the generator for real. The only honest test that script mode works.

    The previous version of this ran the same command, threw the exit code
    away, and printed "warmed up" either way - so a release whose node_modules
    could not resolve reported itself ready and quietly served 360p. Checking
    the return code is the entire point.

    Runs exactly what the plugin's own check runs before it will use Node:
    `node build/generate_once.js --version`. A probe stricter than the real
    thing reports working setups as broken; a looser one reports broken ones
    as working.
    """
    node = find_node()
    if not node:
        return False, "Node.js was not found (re-run setup)"
    if generator_script(script_home) is None:
        return False, (f"no generator in {Path(script_home) / 'src'} - "
                       f"expected one of {', '.join(GENERATOR_NAMES)}")
    script = built_script(script_home)
    if script is None:
        return False, (f"not built yet: {BUILD_DIR}/{BUILT_SCRIPT} is missing")
    try:
        os.makedirs(script_cache_dir(script_home), exist_ok=True)
        done = subprocess.run([node, str(script), "--version"],
                              capture_output=True, text=True, env=script_env(),
                              encoding="utf-8", errors="replace", timeout=180,
                              creationflags=getattr(subprocess,
                                                    "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        return False, f"could not run Node.js: {exc}"
    if done.returncode == 0:
        return True, (done.stdout or "").strip()
    return False, _last_lines(done)


def _last_lines(done) -> str:
    detail = _ANSI.sub("", (done.stderr or done.stdout or "")).strip()
    last = [line for line in detail.splitlines() if line.strip()][-2:]
    return scrub("; ".join(last) or f"exit status {done.returncode}")


# npm's own chatter switched off: no update check against the registry, no
# funding or audit requests, no colour codes in the log.
NPM_ENV = {"npm_config_update_notifier": "false",
           "npm_config_fund": "false",
           "npm_config_audit": "false",
           "npm_config_color": "false"}


def npm_ci_command(node: str, npm: Path | str) -> list[str]:
    """`npm ci`, exactly as setup.ps1 runs it.

    `ci`, not `install`: it installs what package-lock.json lists, every
    package checked against the integrity hash recorded there, and stops if
    the lock and package.json disagree. Nothing is re-resolved on this machine.

    --ignore-scripts: no package's install script runs. That is the same choice
    deno made (no --allow-scripts). The two packages that ask for scripts are
    canvas, whose install script fetches a native part, and @swc/core, a
    development tool. The generator ran without either script under deno for
    months, so it does not need them.
    """
    return [node, str(npm), "ci", "--ignore-scripts", "--no-audit", "--no-fund"]


def tsc_command(node: str, script_home: str) -> list[str]:
    """TypeScript's compiler, from the generator's own node_modules."""
    tsc = Path(script_home) / "node_modules" / "typescript" / "bin" / "tsc"
    return [node, str(tsc)]


def install_script_deps(script_home: str) -> tuple[bool, str]:
    """Build the generator in place: `npm ci`, then `tsc` into build/.

    The result is judged by what Node will run, not by exit codes alone: tsc
    writes its output even when it also reports a type error, and the two
    built files are what the server and the plugin need.
    """
    node = find_node()
    if not node:
        return False, "Node.js was not found (re-run setup)"
    npm = npm_cli(node)
    if npm is None:
        return False, f"npm was not found beside {node} (re-run setup)"
    env = dict(script_env(), **NPM_ENV)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(npm_ci_command(node, npm), cwd=script_home,
                              capture_output=True, text=True, env=env,
                              encoding="utf-8", errors="replace", timeout=900,
                              creationflags=flags)
    except Exception as exc:
        return False, f"could not run npm: {exc}"
    if done.returncode != 0:
        return False, "npm ci: " + _last_lines(done)
    try:
        done = subprocess.run(tsc_command(node, script_home), cwd=script_home,
                              capture_output=True, text=True, env=env,
                              encoding="utf-8", errors="replace", timeout=300,
                              creationflags=flags)
    except Exception as exc:
        return False, f"could not run tsc: {exc}"
    if built_server(script_home) and built_script(script_home):
        return True, ""
    return False, "tsc: " + _last_lines(done)


_provider = PotProvider()


def get_provider() -> PotProvider:
    return _provider
