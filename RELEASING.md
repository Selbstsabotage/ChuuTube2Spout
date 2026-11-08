# Releasing

This document is for the maintainer. It explains how to build and publish a
release, and why a release has obligations that the source code does not.

## Source and release are different

- **The repository** contains only ChuuTube2Spout's own code, under
  GPL-3.0-or-later. Everything else is downloaded or pip-installed on the
  user's computer.
- **A release zip** contains Python, PySide6 and other libraries, built in by
  PyInstaller. Publishing it is **distribution of those libraries**, and their
  licences apply.

## What the release contains, and what it does not

`build.ps1` builds a folder and zips it. It contains:

- `ChuuTube2Spout.exe` and the bundled Python libraries in `_internal\`
- `setup.ps1`, `checksums.txt` and `setup-runtime.bat`, for the first-run setup
- `doctor.bat`, `check.bat`, `clear-cache.bat`, `update-ytdlp.bat`
- `README.md`, `SECURITY.md`, `THIRD-PARTY-NOTICES.md`, `LICENSE` and
  `licenses\`
- `settings.txt`, copied from `settings.txt.example`: every setting off
  except the output size (1920 × 1080, transparent bars). `build.ps1` stops if the shipped file differs from the
  template, so your own `settings.txt` cannot get into the zip.

It does **not** contain `mpv-2.dll`, `deno.exe`, `bgutil-pot.exe` or
`bgutil-server\`. These have the strictest licence terms (see
THIRD-PARTY-NOTICES.md), so each user downloads them from the publishing
projects during first-run setup, checked against `checksums.txt`.

Two ways this can go wrong:

- `setup.ps1 -Portable` puts local copies of those files into the project
  folder. The build does not copy them, but check the zip anyway.
  `build.ps1` reminds you at the end.
- A test on the build computer proves nothing about first-run setup, because
  the runtime is already there. Test on a computer that has never run the app.

## Licence duties for a release

**1. Corresponding source (GPL section 6).** Anyone who gets the exe can ask
for the source of everything in it. Build from a **tag** of this repository,
name the tag in the release notes, and list the versions of the bundled
components so their source can be found.

**2. Replaceable LGPL libraries (LGPL-3.0 section 4).** Users must be able to
replace the Qt libraries. In a folder build ("onedir") the Qt files sit next to
the exe and can be swapped. A single-file build ("onefile") hides them in the
exe and does not meet this condition. `build.ps1` builds onedir. Onedir also
triggers antivirus warnings less often.

**3. Licence texts in the zip.** The full texts, not links. `setup.ps1
-Licenses` fills `licenses\`; THIRD-PARTY-NOTICES.md lists the components.

**The libmpv build sets the minimum.** The libmpv builds that setup downloads
contain GPL components, so they are GPL. ChuuTube2Spout is GPL-3.0-or-later,
which is compatible. That is why this licence was chosen.

## Version numbers

`1.0` means **published**: uploaded, with a tag anyone can download. Every
version before that is `0.9.x`. If `0.9.9` is not published and more work
follows, the version gets a fourth part – `0.9.9.1`, `0.9.9.2` and so on –
instead of becoming `1.0`.

`chuutube2spout/__init__.py` has a `PUBLISHED` flag. It is `True` since `1.0`. While it
was `False`, the tests failed for any version of `1.0` or higher, and `build.ps1`
runs the tests before it builds. After `1.0`, versions count on: `1.0.1`,
`1.1` and so on.

## Release checklist

**Once, before the first release**

- [x] Replace `YOUR-USERNAME` in `.github/ISSUE_TEMPLATE/config.yml` (Selbstsabotage)
- [x] Put your name in the copyright line in `chuutube2spout/__init__.py` (Djinn)
- [x] Fill `checksums.txt` with a hash for every pinned download

**Every release**

1. [ ] Bump `__version__` in `chuutube2spout/__init__.py`. Before `1.0`, add a
   fourth part rather than going to `1.0`.
2. [ ] Bump the libmpv pin to the newest `mpv-dev-x86_64-*.7z`: `Mpv` in
   `$PINNED` in `setup.ps1`, and its line in `checksums.txt`. Check the hash
   against the SHA-256 GitHub shows for the file. Then play a clip with it.
   The publisher deletes all but its latest 30 builds; with an old pin, users
   get the untested fallback.
3. [ ] Check that every other pin still downloads (deno, 7zr, token generator).
4. [ ] Run `powershell -ExecutionPolicy Bypass -File setup.ps1`, so the virtual
   environment matches `requirements.txt`.
5. [ ] Run `powershell -ExecutionPolicy Bypass -File setup.ps1 -Licenses`, then
   work through "Before publishing a release" in THIRD-PARTY-NOTICES.md.
6. [ ] Commit, tag `vX.Y.Z` or `vX.Y.Z.N`, push the tag.
7. [ ] Run `powershell -ExecutionPolicy Bypass -File build.ps1`. It runs the
   tests, checks that the PO token plugin is the pinned version, and stops if
   either fails.
8. [ ] Test the zip **on a computer that has never run the app**, for example a
   clean virtual machine: unzip, start the exe, run the first-run setup, play
   a clip in both slots.
9. [ ] Open the zip. Check that it contains no `logs\`, `cache\`,
   `mpv-2.dll`, `deno.exe`, `bgutil-pot.exe` or `bgutil-server\`, and that
   in `settings.txt` only the three output-size lines are on (no `#`).
10. [ ] Put the SHA-256 that `build.ps1` prints into the release notes.
11. [ ] Upload the zip and name the tag it was built from.

## Good to know

**SmartScreen** warns about an unsigned exe until the download has built up a
reputation. A code-signing certificate avoids this but costs money every year.
Publishing the SHA-256 and telling users to select **More info**, then **Run
anyway**, is a reasonable alternative. Say so in the release notes.

**Antivirus false positives** are common with PyInstaller builds. They do not
mean something is wrong. Report the file to the antivirus vendor as a false
positive.

**Never ship your own `settings.txt`.** It contains your browser choice. It
is in `.gitignore`, the build ships a copy of the template instead, and
`build.ps1` stops if the shipped file is anything else – but check the zip
anyway.
