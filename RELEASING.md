# Releasing

This document is for the maintainer. It explains how to build and publish a
release, and why a release has obligations that the source code does not.

## Source and release are different

- **The repository** contains only ChuuTubePoi's own code, under
  GPL-3.0-or-later. Everything else is downloaded or pip-installed on the
  user's computer.
- **A release zip** contains Python, PySide6 and other libraries, built in by
  PyInstaller. Publishing it is **distribution of those libraries**, and their
  licences apply.

## What the release contains, and what it does not

`build.ps1` builds a folder and zips it. It contains:

- `ChuuTubePoi.exe` and the bundled Python libraries in `_internal\`
- `setup.ps1`, `checksums.txt` and `setup-runtime.bat`, for the first-run setup
- `doctor.bat`, `check.bat`, `clear-cache.bat`, `update-ytdlp.bat`
- `README.md`, `SECURITY.md`, `THIRD-PARTY-NOTICES.md`, `LICENSE` and
  `licenses\`
- `settings.txt`, copied from `settings.txt.example`: every setting off
  except the output size (1920 × 1080, transparent bars). `build.ps1` stops if the shipped file differs from the
  template, so your own `settings.txt` cannot get into the zip.

It does **not** contain `mpv-2.dll`, `node\`, `deno.exe`, `bgutil-pot.exe`
or `bgutil-server\`. These have the strictest licence terms (see
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
contain GPL components, so they are GPL. ChuuTubePoi is GPL-3.0-or-later,
which is compatible. That is why this licence was chosen.

## Version numbers

`1.0` means **published**: uploaded, with a tag anyone can download. Every
version before that is `0.9.x`. If `0.9.9` is not published and more work
follows, the version gets a fourth part – `0.9.9.1`, `0.9.9.2` and so on –
instead of becoming `1.0`.

`chuutubepoi/__init__.py` has a `PUBLISHED` flag. It is `True` since `1.0`. While it
was `False`, the tests failed for any version of `1.0` or higher, and `build.ps1`
runs the tests before it builds. After `1.0`, versions count on: `1.0.1`,
`1.1` and so on.

## Release checklist

**Once, before the first release**

- [x] Replace `YOUR-USERNAME` in `.github/ISSUE_TEMPLATE/config.yml` (Selbstsabotage)
- [x] Put your name in the copyright line in `chuutubepoi/__init__.py` (Djinn)
- [x] Fill `checksums.txt` with a hash for every pinned download

**Every release**

1. [ ] Bump `__version__` in `chuutubepoi/__init__.py`. Before `1.0`, add a
   fourth part rather than going to `1.0`.
2. [ ] Bump the libmpv pin to the newest `mpv-dev-x86_64-*.7z`: `Mpv` in
   `$PINNED` in `setup.ps1`, and its line in `checksums.txt`. Check the hash
   against the SHA-256 GitHub shows for the file. Then play a clip with it.
   The publisher deletes all but its latest 30 builds; with an old pin, users
   get the untested fallback.
3. [ ] Check that every other pin still downloads (Node.js, 7zr, token
   generator). For a new Node.js pin, check its hash against `SHASUMS256.txt`
   on nodejs.org.
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
   `mpv-2.dll`, `node\`, `deno.exe`, `bgutil-pot.exe` or `bgutil-server\`, and that
   in `settings.txt` only the three output-size lines are on (no `#`).
10. [ ] Put the SHA-256 that `build.ps1` prints into the release notes.
11. [ ] Upload the zip and name the tag it was built from.

**For the macOS zip as well** (on the Mac, from the same tag)

12. [ ] `bash setup.sh`, then `bash setup.sh --licenses`, and check its
    `licenses/INVENTORY.md` as in step 5.
13. [ ] `MAC_SIGN_IDENTITY="ChuuTubePoi Signing" bash build-mac.sh`. Always
    the same certificate, so users keep their Full Disk Access. Xcode 26 or
    newer must be installed, for the icon (see "The macOS app").
14. [ ] Test the app on a Mac that has never run it, as in step 8: first
    start, Full Disk Access, a clip in both slots, transparent 4:3 bars in
    Resolume.
15. [ ] Upload the zip unchanged, with the SHA-256 that `build-mac.sh`
    prints.

## The macOS app

`build-mac.sh` builds `ChuuTubePoi.app` on a Mac and zips it with the
documents. Run it in the source folder, after `setup.sh`:

```
bash setup.sh
bash build-mac.sh
```

- It runs the tests, checks the PO token plugin pin, builds with PyInstaller
  (a folder build inside the app, so Qt stays replaceable), signs the app ad
  hoc, and refuses to zip if the app contains libmpv, `node`,
  `bgutil-server`, `cache`, `logs` or a `settings.txt`.
- Before zipping, it runs the built app twice: `--yt-dlp --version` and
  `--syphon-check`.
- What the tests and PyInstaller print goes to `build-mac.log`, not to the
  screen. If a step fails, the script shows the end of that step's output.
  Send the whole file with a bug report.
- The zip is `dist/ChuuTubePoi-<version>-macos-<processor>.zip`. The app
  is built for the processor of the Mac that builds it. It needs macOS 14
  or newer, because PySide6 6.12 does.
- **Publish the zip exactly as `build-mac.sh` made it.** Unpacking and
  zipping it again on Windows loses the links inside the app and breaks its
  signature. Looking into it on Windows is fine.
- **Licence texts:** `bash setup.sh --licenses` before the build, as
  `setup.ps1 -Licenses` on Windows. Without it, the build warns that the
  zip is a test build.
- **Icon:** the app carries two icons. macOS 14 and 15 show
  `assets/ChuuTubePoi.icns`. macOS 26 and newer show the Liquid Glass icon
  from `Assets.car`, which the build compiles from the Icon Composer file
  `assets/ChuuTubePoi.icon` with Xcode's `actool`. This needs Xcode 26 or
  newer from the App Store, opened once; the Command Line Tools alone are
  not enough. Without Xcode the build stops before PyInstaller. For a test
  build without it: `MAC_NO_ASSETS_CAR=1 bash build-mac.sh`. macOS 26 may
  then show the `.icns` inside a grey frame.
- To change the icon: edit `assets/ChuuTubePoi.icon` in Icon Composer, and
  export `ChuuTubePoi.icns`, `ChuuTubePoi.ico` (Windows) and `icon.png`
  (1024 x 1024, the window icon) from the same design.
- The app contains `setup.sh`, `checksums.txt` and `settings.txt.example`. On
  its first start it runs `setup.sh --runtime-only`, which installs libmpv
  with Homebrew if it is missing, and downloads Node.js and the token
  generator into `~/Library/Application Support/ChuuTubePoi`.
- **Keeping permissions across builds (free).** Signed ad hoc, every build
  is a new app to macOS: Full Disk Access given to the last build has to be
  removed and given again. A self-made certificate avoids that. Once, on
  the Mac that builds:
  1. Open **Keychain Access**, then **Keychain Access > Certificate
     Assistant > Create a Certificate**.
  2. Name: `ChuuTubePoi Signing`. Identity Type: **Self Signed Root**.
     Certificate Type: **Code Signing**. Turn on **Let me override
     defaults**, select **Continue**, and set **Validity Period (days)** to
     `3650` (the default is one year). Accept the rest, then **Create**.
  3. Build with it, every time:
     ```
     MAC_SIGN_IDENTITY="ChuuTubePoi Signing" bash build-mac.sh
     ```
     The first time, the keychain asks whether `codesign` may use the key:
     select **Always Allow**.
  4. Once, after the first build signed this way: remove the old
     ChuuTubePoi entry from Full Disk Access and add the new app. Later
     builds keep the permission.
  macOS remembers the permission by the app's name and certificate, not by
  the Mac that built it. So it holds for everyone: a user who allowed 1.1
  keeps the permission with 1.2, as long as both are signed with this
  certificate.
  - **Back it up.** In Keychain Access, right-click the certificate,
    **Export**, as a `.p12` file with a password. Without it, the next
    release has to be signed with a new certificate, and every user has to
    allow the app again. With it, any Mac can build the next release.
  - It does not make macOS trust the app: each downloaded version is
    blocked once (**Open Anyway**), as before.
- The app is not signed with a Developer ID and not notarised. macOS blocks
  it once on other Macs; users allow it in **System Settings > Privacy &
  Security**. A Developer ID costs a yearly Apple Developer Program fee.

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
