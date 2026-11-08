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

The token generator is a single Rust executable with no Node.js or other
runtime behind it, which matters because this app is meant to be portable. We
run it as a child process for the lifetime of the app and point yt-dlp at it;
if someone already has one running on the port, we use theirs instead.
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

from .bootstrap import project_root
from .config import POT_VERBOSE, TOKEN_SERVER
from .redact import scrub

DEFAULT_PORT = 4416
START_TIMEOUT = 45.0
# How long a download may wait for the one-off `deno install` to finish. The
# alternative is fetching that clip without a token, i.e. at 360p.
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
    """The generator inside a `server` directory, whichever release wrote it."""
    src = Path(server) / "src"
    for name in GENERATOR_NAMES:
        candidate = src / name
        if candidate.is_file():
            return candidate
    return None


# Written into node_modules after a probe has actually run the generator. The
# folder existing is not evidence: a deno install that dies half way leaves a
# node_modules behind that cannot resolve.
READY_MARKER = ".chuutube-ready"

# What deno says when the drive cannot make the links node_modules is built
# from. Seen on a tester's Y: drive: "failed to create junction '...' to '...':
# Incorrect function. (os error 1)" - ERROR_INVALID_FUNCTION: the driver behind
# the drive letter does not implement the request.
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
    return Path(base) / "ChuuTube2Spout" / "bgutil-server"


def is_ready(server: Path | str) -> bool:
    return (Path(server) / "node_modules" / READY_MARKER).is_file()


def mark_ready(server: Path | str) -> None:
    node_modules = Path(server) / "node_modules"
    if not node_modules.is_dir():
        return
    try:
        (node_modules / READY_MARKER).write_text(
            "probed OK by ChuuTube2Spout\n", encoding="utf-8")
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
    the same release, so they cannot disagree about protocol. deno runs the
    TypeScript directly, so no Node.js is required.

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
    """deno running the generator's own server, or None if this tree has none.

    2.0.0 merged the one-shot generator and the server into a single main.ts,
    which is what makes this possible: the server is now the same code, from
    the same release, as the plugin that talks to it. Version-matched by
    construction - unlike bgutil-pot.exe, which is a separate Rust port on its
    own numbering (0.8.x against the plugin's 2.x) and is refused by the very
    compatibility check that exists to stop mismatches.

    Only for 2.x trees: 1.3.1's generate_once.ts answers one request and exits.
    """
    script = generator_script(script_home)
    if script is None or script.name != "main.ts":
        return None
    deno = shutil.which("deno")
    if not deno:
        return None
    return [deno, *script_args(script_home), str(script)]


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
        # Which server is answering: "deno" (the generator's own, version-matched
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
        """Which server is answering: "deno", "exe", "external", or None.

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
        be: yt-dlp runs the generator itself, per request, via deno. Anything
        asking "are we gated?" wants this, not a process check.
        """
        return (self._script_home is not None and self._script_ok is not False
                ) or self.running

    def compatible(self) -> bool:
        """Will the installed plugin actually talk to this server?

        Pointing yt-dlp at a server the plugin refuses is worse than pointing
        it at nothing: you get a wall of warnings and the same failure.
        """
        if self._server_kind == "deno":
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
        token, the deno run fails, and the only visible consequence is a 360p
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
        # either way; the difference is that script mode pays for a deno
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
                # fresh deno run - and, on a cold tree, a fresh deno install.
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

    def _prepare_script(self, script_home: str) -> None:
        """Verify script mode really works, repairing it once if it does not.

        A release ships the generator's source but not its `node_modules`:
        deno lays that tree out as a farm of symlinks into `.deno/`, and both
        copying and zipping dereference those links, which leaves every package
        without its own dependencies. The tree has to be built on the machine
        that will use it - which is a one-off minute, and the same `deno
        install` that setup.ps1 runs from source.
        """
        ok, detail = script_probe(script_home)
        if not ok:
            self.status = "preparing the token generator (about a minute)"
            print(f"[pot] {self.status}", file=sys.stderr)
            installed, install_detail = install_script_deps(script_home)
            if not installed:
                print(f"[pot] deno install failed: {install_detail}",
                      file=sys.stderr)
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
        # the deno one that had just failed, which failed again the same way.
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
        print(f"[pot] {drive} refused to create the junctions deno builds "
              "node_modules from (\"Incorrect function\") - exFAT and FAT32 "
              f"drives cannot make them. Building it in {target} instead.",
              file=sys.stderr)
        installed, detail = install_script_deps(target)
        if not installed:
            print(f"[pot] deno install failed there too: {detail}",
                  file=sys.stderr)
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
        # Only a tree a probe has run. An unbuilt one cannot start - "Could not
        # resolve 'express'" - and trying anyway put a deno error at the top of
        # every first run's log, which a tester reasonably read as deno being
        # broken. _prepare_script builds it and then calls back in here.
        if script_home and is_ready(script_home):
            command = script_server_command(script_home)
            if command is not None:
                kind = "deno"
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
                if script_home:
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


def script_args(script_home: str) -> list[str]:
    """deno's arguments, matching BgUtilScriptDenoPTP._jsrt_args exactly.

    Scoped, not --allow-all: this runs third-party TypeScript. The first version
    of this granted only node_modules, which is narrower than the plugin - the
    generator calls existsSync on its cache directory and died on a permission
    it is normally given. A probe stricter than the real thing reports working
    setups as broken, which is worse than not probing at all.
    """
    cache = script_cache_dir(script_home)
    node_mods = os.path.join(script_home, "node_modules")

    def esc(*parts):
        return ",".join(p.replace(",", ",,") for p in parts)

    return ["run", "--allow-env", "--allow-net",
            f"--allow-ffi={esc(node_mods)}",
            f"--allow-write={esc(cache)}",
            f"--allow-read={esc(cache, node_mods)}"]


def script_env() -> dict:
    env = dict(os.environ)
    env["DENO_NO_PROMPT"] = "1"
    env["DENO_NO_UPDATE_CHECK"] = "1"
    env["FORCE_COLOR"] = "false"
    return env


def script_probe(script_home: str) -> tuple[bool, str]:
    """Run the generator for real. The only honest test that script mode works.

    The previous version of this ran the same command, threw the exit code
    away, and printed "warmed up" either way - so a release whose node_modules
    could not resolve reported itself ready and quietly served 360p. Checking
    the return code is the entire point.

    Doubles as the warm-up it used to be: deno's first run compiles the
    TypeScript, which on a cold cache takes longer than yt-dlp's 15 second
    availability window.
    """
    deno = shutil.which("deno")
    script = generator_script(script_home)
    if not deno:
        return False, "deno was not found on PATH"
    if script is None:
        return False, (f"no generator in {Path(script_home) / 'src'} - "
                       f"expected one of {', '.join(GENERATOR_NAMES)}")
    try:
        os.makedirs(script_cache_dir(script_home), exist_ok=True)
        # 2.0.0's main.ts is the server CLI and has no --version; asking for
        # one answers "error: unknown option '--version'", which this then
        # reported as "script mode is broken" on a setup that was fine.
        probe_flag = "--help" if script.name == "main.ts" else "--version"
        done = subprocess.run([deno, *script_args(script_home),
                               str(script), probe_flag],
                              capture_output=True, text=True, env=script_env(),
                              encoding="utf-8", errors="replace", timeout=180,
                              creationflags=getattr(subprocess,
                                                    "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        return False, f"could not run deno: {exc}"
    if done.returncode == 0:
        return True, (done.stdout or "").strip()
    detail = _ANSI.sub("", (done.stderr or done.stdout or "")).strip()
    last = [line for line in detail.splitlines() if line.strip()][-2:]
    return False, scrub("; ".join(last) or f"exit status {done.returncode}")


def install_script_deps(script_home: str) -> tuple[bool, str]:
    """Build the generator's node_modules with deno, in place.

    No --allow-scripts. That authorises npm lifecycle scripts for every package
    in the resolved tree, and the tree is re-resolved on each machine - so one
    compromised transitive dependency would run as the user. setup.ps1 makes
    the same call and the same choice; the packages that want scripts here are
    dev-only.
    """
    deno = shutil.which("deno")
    if not deno:
        return False, "deno was not found on PATH"
    try:
        done = subprocess.run([deno, "install"], cwd=script_home,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=900,
                              creationflags=getattr(subprocess,
                                                    "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        return False, f"could not run deno install: {exc}"
    if done.returncode == 0 and (Path(script_home) / "node_modules").is_dir():
        return True, ""
    detail = _ANSI.sub("", (done.stderr or done.stdout or "")).strip()
    last = [line for line in detail.splitlines() if line.strip()][-2:]
    return False, scrub("; ".join(last) or f"exit status {done.returncode}")


_provider = PotProvider()


def get_provider() -> PotProvider:
    return _provider
