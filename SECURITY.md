# Security

ChuuTube2Spout runs on the computer that drives a live show. This document
says what the app stores, what it downloads and runs, what it does to keep
that safe, and where its limits are.

## Reporting a vulnerability

- **Exploitable problems:** report them privately on GitHub. Open the
  repository's **Security** tab and select **Report a vulnerability**. Only
  you and the maintainer see the report. Do not open a public issue. Allow a
  few days for a fix before you disclose it.
- **Everything else:** open a GitHub issue.

Logs and `check.bat` output are safe to attach: IP addresses, PO tokens and
signed links are replaced by `<redacted>` (`chuutube2spout/redact.py`). File
paths are not removed, and they contain your Windows user name. Output from
your own `yt-dlp -v` runs is not redacted – check it before you paste it.

## What the app stores

| What | Where | Removed |
|---|---|---|
| YouTube cookies (only with `COOKIES_FROM_BROWSER`) | A private temporary folder, not the app folder | When the app closes. Leftovers from a crashed run are removed at the next start (after 6 h) |
| Downloaded clips, named by title and video ID | `cache\` in the app folder | After 24 h, by the size limit, by **Clear cache on exit**, or by `clear-cache.bat` |
| yt-dlp's description of a recently loaded video. It contains a PO token and your IP address | `cache\` | After 45 min |
| Log files, redacted | `logs\` in the app folder | The oldest when there are more than 10 |
| Settings, including your browser choice | `settings.txt` in the app folder | When you delete it |
| Window size and position, selected slot | Registry: `HKEY_CURRENT_USER\Software\ChuuTube2Spout` | When you delete the key |

**The cookie file.** It contains only `youtube.com` and `googlevideo.com`
cookies. It never contains `google.com` cookies: a match on `google.com` would
also take the cookies of `accounts.google.com` and `mail.google.com`, which
give access to the whole Google account. yt-dlp does not need them. You can
widen `COOKIE_DOMAINS`, but it is a bad idea.

The file is kept out of the app folder because people copy and zip that
folder. Windows gives the temporary folder owner-only access.

**The clip folder** is a record of what you played. Delete it, or turn on
**Clear cache on exit**, if that matters.

## Network connections

| Destination | Why |
|---|---|
| YouTube and Google | Video pages, video files, PO tokens |
| GitHub | First-run setup downloads, and the challenge solver yt-dlp downloads at run time |
| `127.0.0.1` | The app's own token generator |

The app sends no usage data, no crash reports and does not check for updates.

## What setup downloads and runs

The first-run setup (`setup-runtime.bat`, or `setup.ps1` from source) downloads:

| Download | Source | Used how |
|---|---|---|
| `libmpv-2.dll` | shinchiro/mpv-winbuild-cmake on GitHub | Loaded into the app's process |
| `deno.exe` | denoland/deno on GitHub | Runs the token generator and yt-dlp's challenge solver |
| Token generator source | Brainicism/bgutil-ytdlp-pot-provider on GitHub | TypeScript, run by deno |
| npm packages | npm registry, through `deno install` | Dependencies of the token generator |
| `7zr.exe` | ip7z/7zip on GitHub | Only if Windows cannot unpack the libmpv archive itself |
| `bgutil-pot.exe` | jim60105/bgutil-ytdlp-pot-provider-rs on GitHub | Only when its version matches the plugin. With the current pins it is skipped |

A hijacked release of any of these would run code on every computer that runs
setup afterwards. These measures limit that risk:

- **Pinned versions and hashes.** Each download has a fixed version and its
  SHA-256 in `checksums.txt`. A file that does not match is refused.
- **libmpv fallback.** The publisher of libmpv deletes all but its latest 30
  builds, so a pinned build disappears after a few weeks. Setup then takes the
  newest build and checks it against the SHA-256 that GitHub publishes for that
  file. Without a published hash it installs nothing. Setup says when it uses
  this fallback.
- **Allowed hosts only.** Every download address must be on `github.com`,
  `githubusercontent.com` or `7-zip.org` before the download starts. Redirects
  are not checked separately; the hash check covers them.
- **Retries without trust.** A failed download is deleted and fetched again,
  up to three times. The hash check still decides.
- **Safe unpacking.** Before an archive is unpacked, its list of files is
  checked: a file with an absolute path or `..` is refused. After unpacking,
  any link inside the result is refused.
- **Exact file names.** Only `libmpv-2.dll` is taken from the archive, and
  executables are moved into place only after they are checked.
- **New, random temporary folders** for every download, so nothing prepared in
  advance in `%TEMP%` is used.
- **No npm install scripts.** `deno install` runs without `--allow-scripts`.

## Deliberate trust decisions

**A pasted link is untrusted input.** yt-dlp has options such as `--exec`
that run commands. A link that starts with `-` is refused, and `--` is always
passed before the link, so nothing in it is read as an option. If you change
the code, keep the link last and keep the `--`.

**yt-dlp downloads and runs JavaScript from GitHub** (`--remote-components
ejs:github`). It needs this to solve YouTube's challenges. It is remote code
by design. To turn it off, set `REMOTE_COMPONENTS=` in `settings.txt`; you then
get 360p at most.

**The token generator is third-party TypeScript.** deno runs it with access to
environment variables and the network, and with file access limited to its own
folder and cache. It does not get `--allow-all`.

**Port 4416.** The app starts its token generator on `127.0.0.1:4416`. If
another program already listens on that port, the app uses it without checking
what it is. Any program running as your user could do that and receive the
token requests.

**Stream mode** (`MODE=stream`, not the default) uses a relay on `127.0.0.1`
with a random port. It serves only the links it was given, each behind a random
handle. It is not an open proxy.

## Limits

- Anything running as your Windows user can read the cookie file while the app
  runs, read `settings.txt`, and use port 4416. The app does not protect
  against that.
- `clear-cache.bat` does not touch your browser's login.
