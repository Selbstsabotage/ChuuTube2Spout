# SPDX-License-Identifier: GPL-3.0-or-later
"""Getting YouTube cookies out of a browser, without needing it closed.

Two jobs here.

**Finding the profile.** yt-dlp supports "firefox:PROFILE_PATH". Waterfox is a
Firefox fork with an identical cookie store, so pointing the firefox reader at a
Waterfox profile works - it just has to be found first, because yt-dlp will not
look there.

**Exporting once, instead of on every fetch.** Handing yt-dlp
`--cookies-from-browser` means it reads and re-parses the whole cookie store on
every single invocation, and it reads the live database out from under a browser
that is still writing to it. Instead we copy the store - including its
write-ahead log, so a login from five minutes ago is actually in there - read it
with the sqlite3 module in the standard library, and write a plain cookies.txt
that yt-dlp is handed with `--cookies`.

That means **Waterfox can stay open**, which matters: it is where the URLs are
being copied from in the first place. Copying the file is also the only way to
read it safely while the browser holds it; reading the original in place is what
produces "database is locked".

**Scope, and why it is narrow.** Only cookies for youtube.com and
googlevideo.com are written out. An earlier version also matched google.com,
which sounds harmless and is not: a suffix match on it takes in
accounts.google.com, mail.google.com and pay.google.com, i.e. SID, HSID, SSID
and __Secure-*PSID - the master Google account cookies, which authenticate Gmail
and Drive and satisfy an already-2FA'd session on their own. yt-dlp only ever
reads the jar for youtube.com, so those bought nothing and risked an account.

**Lifetime.** The jar is a live login, so it lives in a private temp directory
for the length of one run and is deleted on the way out. It deliberately does
not go in the app folder: this app is meant to be portable, the whole directory
gets copied to other machines, and a secret that travels with a zip is a secret
you have given away.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

# Forks that store cookies in Firefox's format, and where they keep profiles.
FIREFOX_FORKS = {
    "waterfox": ("Waterfox",),
    "librewolf": ("librewolf", "LibreWolf"),
    "floorp": ("Floorp",),
    "zen": ("zen",),
}

# Firefox itself is not a "fork" - yt-dlp finds it unaided - but we still need
# its profile path to export cookies ourselves.
PROFILE_FOLDERS = dict(FIREFOX_FORKS,
                       firefox=("Mozilla/Firefox", "Firefox", "firefox"))

# The sidecar files SQLite keeps beside a database in WAL mode. Copying the
# .sqlite alone gets you the cookies as of the last checkpoint, which can be
# hours stale - so a fresh login appears to have not happened.
DB_SIDECARS = ("-wal", "-shm")


def profile_roots(folder_names) -> list[Path]:
    bases = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        value = os.environ.get(env)
        if value:
            bases.append(Path(value))
    home = Path.home()
    bases += [home / ".mozilla", home / "Library" / "Application Support"]
    return [base / name / "Profiles" for base in bases for name in folder_names]


def find_profile(browser: str) -> str | None:
    """Newest profile directory containing a cookie store, or None."""
    folders = PROFILE_FOLDERS.get(browser.lower())
    if not folders:
        return None
    candidates = []
    for root_dir in profile_roots(folders):
        if not root_dir.is_dir():
            continue
        for profile in root_dir.iterdir():
            cookies = profile / "cookies.sqlite"
            if cookies.is_file():
                candidates.append((cookies.stat().st_mtime, profile))
    if not candidates:
        return None
    return str(max(candidates)[1])


def resolve(value: str) -> tuple[str, str | None]:
    """Turn a config value into what yt-dlp expects.

    "waterfox" -> ("firefox:<profile path>", None)
    anything else is passed straight through - yt-dlp already understands
    "chrome", "firefox:PATH", "firefox::PROFILE" and so on.
    Returns (argument, warning).
    """
    value = (value or "").strip()
    if not value:
        return "", None
    name = value.split(":", 1)[0].lower()
    if name in FIREFOX_FORKS and ":" not in value:
        profile = find_profile(name)
        if profile:
            return f"firefox:{profile}", None
        return "", (f"no {name} profile with cookies found - "
                    f"pass the path yourself, e.g. firefox:C:\\path\\to\\profile")
    return value, None


# --- exporting ---------------------------------------------------------------

# Deliberately NOT "google.com". A suffix match on that also matches
# accounts.google.com, mail.google.com and pay.google.com - meaning SID, HSID,
# SSID and __Secure-*PSID, the master Google account cookies. Those authenticate
# Gmail and Drive, not just YouTube, and on their own they satisfy a session
# that has already passed 2FA. yt-dlp only reads the jar for youtube.com, so
# exporting the rest bought nothing and risked an account.
DEFAULT_DOMAINS = ("youtube.com", "googlevideo.com")

# The cookies a signed-in YouTube session actually rests on. Everything else the
# browser has accumulated for youtube.com is ballast that still gets sent on
# every single request, and YouTube answers a request whose headers are too big
# with "HTTP Error 413: Request Entity Too Large" - which is not a warning, it
# ends the load. A tester hit exactly that: the webpage fetch 413'd four times
# and the whole chain died before a format was ever chosen.
ESSENTIAL_COOKIES = frozenset({
    # Account identity and its refreshing halves.
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDCC", "__Secure-3PSIDCC", "SIDCC",
    "LOGIN_INFO",
    # Session, consent and the visitor identity the PO token is bound to.
    "PREF", "YSC", "CONSENT", "SOCS", "GPS", "wide",
    "VISITOR_INFO1_LIVE", "VISITOR_PRIVACY_METADATA",
    "__Secure-ROLLOUT_TOKEN",
})

# Per-surface tokens YouTube sets by the dozen as you browse. Never needed here,
# and they are the ones that grow a jar until it is refused.
BALLAST_PREFIXES = ("ST-",)

# Roughly how much Cookie: header YouTube will take. The real limit is a total
# header budget, so leave room for the user agent, the auth hash and the rest.
MAX_JAR_KB = 6.0

# Firefox has renamed these columns over the years; ask for what we need and
# tolerate a store that spells it differently rather than failing outright.
_QUERY = ("SELECT host, path, isSecure, expiry, name, value "
          "FROM moz_cookies")


def profile_path(value: str) -> Path | None:
    """The Firefox-family profile directory a config value points at, if any.

    Chrome-family browsers encrypt their cookie store with a key held by the OS
    keyring, so there is nothing useful to do here - those fall back to letting
    yt-dlp handle it.
    """
    value = (value or "").strip()
    if not value:
        return None
    name, _, rest = value.partition(":")
    name = name.lower()
    if rest and not rest.startswith(":"):
        candidate = Path(rest)
        return candidate if (candidate / "cookies.sqlite").is_file() else None
    if name not in PROFILE_FOLDERS:
        return None
    found = find_profile(name)
    return Path(found) if found else None


def _copy_store(profile: Path, into: Path) -> Path:
    """Snapshot the cookie database so the browser can keep using the original."""
    source = profile / "cookies.sqlite"
    target = into / "cookies.sqlite"
    shutil.copyfile(source, target)
    for suffix in DB_SIDECARS:
        sidecar = source.with_name(source.name + suffix)
        if sidecar.is_file():
            try:
                shutil.copyfile(sidecar, target.with_name(target.name + suffix))
            except OSError:
                pass          # the checkpointed rows are still usable
    return target


def _netscape_line(host, path, secure, expiry, name, value) -> str:
    host = str(host or "")
    return "\t".join([
        host,
        "TRUE" if host.startswith(".") else "FALSE",
        str(path or "/"),
        "TRUE" if secure else "FALSE",
        str(int(expiry or 0)),
        str(name or ""),
        str(value or ""),
    ])


def export(value: str, dest: Path, domains=DEFAULT_DOMAINS) -> tuple[Path | None, str | None]:
    """Write a Netscape cookies.txt for `value`'s browser. (path, warning)."""
    profile = profile_path(value)
    if profile is None:
        return None, None            # not a Firefox-family profile; not an error

    workdir = Path(tempfile.mkdtemp(prefix="chuutube-cookies-"))
    try:
        copy = _copy_store(profile, workdir)
        connection = sqlite3.connect(str(copy))
        try:
            rows = connection.execute(_QUERY).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        return None, f"could not read {profile.name}: {exc}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    wanted = tuple(d.lower() for d in domains if d)
    # The profile *name* only. The full path contains the Windows account name,
    # and this header travels inside any copy of the file.
    lines = ["# Netscape HTTP Cookie File",
             f"# Written by ChuuTube2Spout from the {profile.name} profile", ""]
    picked: list[tuple[str, int, str]] = []      # name, header cost, file line
    for host, path, secure, expiry, name, cookie_value in rows:
        low = str(host or "").lower().lstrip(".")
        if wanted and not any(low == d or low.endswith("." + d) for d in wanted):
            continue
        name = str(name or "")
        if name.startswith(BALLAST_PREFIXES):
            continue
        cost = len(name) + len(str(cookie_value or "")) + 3   # name=value; 
        picked.append((name, cost, _netscape_line(host, path, secure, expiry,
                                                  name, cookie_value)))

    if not picked:
        return None, (f"no cookies for {', '.join(wanted)} in {profile.name} - "
                      "are you signed in to YouTube in that browser?")

    note = None
    if sum(cost for _, cost, _ in picked) > MAX_JAR_KB * 1024:
        # Sending them all would be refused outright, so send the ones that
        # carry the login and drop the rest. Better a smaller jar than a 413.
        essential = [c for c in picked if c[0] in ESSENTIAL_COOKIES]
        if essential:
            note = (f"jar was over {MAX_JAR_KB:.0f} KB, which YouTube refuses "
                    f"with HTTP 413 - kept the {len(essential)} cookies the "
                    f"login needs, dropped {len(picked) - len(essential)}")
            picked = essential
        else:
            note = (f"jar is over {MAX_JAR_KB:.0f} KB and none of it is a "
                    "recognised YouTube login cookie - expect HTTP 413")

    lines.extend(line for _, _, line in picked)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_private(dest, "\n".join(lines) + "\n")
    except OSError as exc:
        return None, f"could not write {dest}: {exc}"
    return dest, note


def _write_private(dest: Path, text: str) -> None:
    """Write a file only the current user can read.

    A cookie jar written with default permissions is readable by anyone else
    with an account on the machine - which on a shared gig laptop is not a
    theoretical set of people.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    handle = os.open(str(dest), flags, 0o600)
    try:
        os.write(handle, text.encode("utf-8"))
    finally:
        os.close(handle)
    try:
        os.chmod(str(dest), 0o600)   # existing files keep their old mode
    except OSError:
        pass


# --- where the jar lives ------------------------------------------------------
# NOT in the app folder. This app is meant to be portable - the README tells
# people to copy the whole directory to another machine, and `setup.ps1
# -Portable` exists to encourage it. A cookie jar sitting in `cache/` gets
# copied, zipped and shared along with everything else, and the person doing it
# has no reason to think the folder contains a live login. So it goes in a
# private temp directory for the lifetime of the process, and is deleted on the
# way out.

_SESSION_DIR: Path | None = None


def session_dir() -> Path:
    """Per-process private directory for the exported cookie jar."""
    global _SESSION_DIR
    if _SESSION_DIR is None:
        _SESSION_DIR = Path(tempfile.mkdtemp(prefix="chuutube-session-"))
        atexit.register(cleanup)
    return _SESSION_DIR


def cleanup() -> None:
    """Remove the exported jar. Safe to call more than once."""
    global _SESSION_DIR
    if _SESSION_DIR is not None:
        shutil.rmtree(_SESSION_DIR, ignore_errors=True)
        _SESSION_DIR = None


def sweep_stale(older_than_hours: float = 6.0) -> int:
    """Delete leftovers from runs that were killed before they could clean up.

    The snapshot taken during an export is the *whole* cookie store, not the
    filtered subset - so a crash at the wrong moment strands every cookie the
    browser holds in %TEMP%. Normal exits handle themselves; this is for the
    power cut.
    """
    removed = 0
    cutoff = time.time() - older_than_hours * 3600
    try:
        root = Path(tempfile.gettempdir())
        stale = list(root.glob("chuutube-cookies-*")) + \
            list(root.glob("chuutube-session-*"))
    except OSError:
        return 0
    for path in stale:
        if _SESSION_DIR is not None and path == _SESSION_DIR:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def ensure(value: str, cache_dir=None, max_age_minutes: float = 60.0,
           domains=DEFAULT_DOMAINS) -> tuple[Path | None, str | None]:
    """A cookies.txt no older than `max_age_minutes`, re-exported if it is."""
    dest = Path(cache_dir or session_dir()) / "cookies.txt"
    try:
        age = (time.time() - dest.stat().st_mtime) / 60.0
        if age < max_age_minutes:
            return dest, None
    except OSError:
        pass
    exported, warning = export(value, dest, domains)
    if exported is None and dest.is_file():
        # A stale file beats no file: cookies outlive the refresh interval by a
        # long way, and a locked profile now should not end a set.
        return dest, warning
    return exported, warning
