#!/bin/bash
# ChuuTubePoi release build (macOS)
#
# Builds dist/ChuuTubePoi.app and zips it. The macOS counterpart of
# build.ps1. Run on a Mac, in this folder, after setup.sh:
#
#     bash setup.sh
#     bash build-mac.sh
#
# The app runs without Python. What it does not contain, on purpose, as on
# Windows: libmpv (Homebrew), Node.js and the PO token generator. On its first
# start the app runs its bundled setup.sh --runtime-only, which fetches them
# into ~/Library/Application Support/ChuuTubePoi, checked against
# checksums.txt.
#
# A folder build (onedir), as on Windows: the Qt libraries (LGPL) stay
# ordinary files in the bundle that a user can replace.
#
# The app is not signed with a Developer ID and not notarised. On the Mac
# that built it, it opens normally. Copied to another Mac it is blocked
# once: System Settings > Privacy & Security > Open Anyway.
#
# Signed ad hoc by default. Then macOS sees every new build as a different
# app, and permissions given to the last one (Full Disk Access, for the
# Firefox login) have to be removed and given again (tester, 1.1). With a
# certificate of your own, made once in Keychain Access (free; RELEASING.md
# says how), every build is the same app to macOS and keeps them:
#
#     MAC_SIGN_IDENTITY="ChuuTubePoi Signing" bash build-mac.sh
#
# The icon comes twice: assets/ChuuTubePoi.icns for macOS 14 and 15, and
# Assets.car, compiled from the Icon Composer file assets/ChuuTubePoi.icon,
# for the Liquid Glass icons of macOS 26 and later. Compiling it needs Xcode
# 26 or later (from the App Store; the Command Line Tools alone do not have
# actool). For a test build without Xcode:
#
#     MAC_NO_ASSETS_CAR=1 bash build-mac.sh
#
# Written for the bash that ships with macOS (3.2).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
VENV_PY="$ROOT/.venv/bin/python"
APP="$ROOT/dist/ChuuTubePoi.app"
BUNDLE_ID="io.github.selbstsabotage.chuutubepoi"
# A code signing certificate's name, or "-" for ad hoc (see the top).
SIGN_ID="${MAC_SIGN_IDENTITY:--}"
# The Icon Composer file. Its name is the icon's name in Assets.car and in
# CFBundleIconName.
ICON_SOURCE="$ROOT/assets/ChuuTubePoi.icon"
ICON_NAME="ChuuTubePoi"

# What the tests and PyInstaller print goes here, not on the screen: it is
# thousands of lines, and the one that matters is easy to lose among them
# (tester, 1.1: the copied Terminal output ended before the failure).
LOG="$ROOT/build-mac.log"

say()  { printf '==> %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*" >&2; }
die()  { printf 'XX  %s\n' "$*" >&2; exit 1; }

logged() {
    # logged TITLE COMMAND... - run COMMAND with its output in $LOG. If it
    # fails, show the end of what it wrote and say where the rest is.
    local title="$1" start
    shift
    printf '\n===== %s =====\n' "$title" >>"$LOG"
    start="$(wc -l <"$LOG")"
    if "$@" >>"$LOG" 2>&1; then
        return 0
    fi
    warn "$title failed. The end of its output:"
    tail -n +"$((start + 1))" "$LOG" | tail -n 60 >&2
    warn "All of it is in $LOG - please send that file."
    return 1
}

# The bundle must hold nothing that is fetched at first start or that is
# yours. Libraries: searched anywhere in the bundle. Folders and files: in
# the bundle's own folders only (Contents/Resources, Contents/Frameworks),
# where setup or a test run would have left them - a Python package deep
# inside may well have a folder called "node".
FORBIDDEN_LIBS="libmpv*.dylib libavcodec*.dylib"
FORBIDDEN_TOP="node bgutil-server cache logs settings.txt .venv"

forbidden_in() {
    # forbidden_in APP - prints whatever in APP must not be published.
    local name
    for name in $FORBIDDEN_LIBS; do
        find "$1" -name "$name" -print
    done
    for name in $FORBIDDEN_TOP; do
        find "$1" -maxdepth 3 -name "$name" -print
    done
}

broken_frameworks() {
    # broken_frameworks APP - prints every versioned *.framework folder in APP
    # that PyInstaller copied flat. A framework's top level must hold only
    # "Versions" and links into it; PyInstaller copies a framework that
    # comes as a package's data file by file, and the links become folders.
    # codesign then refuses the whole app: "unsealed contents present in the
    # root directory of an embedded framework" (tester, 1.1, Syphon.framework
    # from syphon-python). Qt's frameworks are not touched: PyInstaller's own
    # hooks build those correctly.
    local fw entry
    find "$1/Contents" -type d -name '*.framework' | while IFS= read -r fw; do
        [ -d "$fw/Versions" ] || continue
        for entry in "$fw"/*; do
            [ -e "$entry" ] || continue
            [ "$(basename "$entry")" = "Versions" ] && continue
            if [ ! -L "$entry" ]; then
                echo "$fw"
                break
            fi
        done
    done
}

repair_frameworks() {
    # repair_frameworks APP - gives each broken framework its links back, in
    # place, and prints where it is. There is no whole original to copy
    # from: a wheel cannot hold links, so the framework is already flat in
    # the venv (tester, 1.1 - copying it from there changed nothing).
    #   Versions/Current becomes a link to the one real version folder.
    #   Each other top-level entry moves into that version, unless it is
    #   there already, and becomes a link to Versions/Current/<name>.
    # A second, partial copy under Contents/Resources becomes a link to the
    # one under Contents/Frameworks.
    local app="$1" fw rel twin ups dir ver entry name
    broken_frameworks "$app" | sort | while IFS= read -r fw; do
        case "$fw" in
            "$app/Contents/Frameworks/"*) rel="${fw#"$app/Contents/Frameworks/"}" ;;
            "$app/Contents/Resources/"*)  rel="${fw#"$app/Contents/Resources/"}" ;;
            *) echo "unexpected place for a framework: $fw" >&2; exit 1 ;;
        esac
        twin="$app/Contents/Frameworks/$rel"
        if [ "$fw" != "$twin" ] && [ -d "$twin" ] && [ ! -L "$twin" ]; then
            ups=".."
            dir="$(dirname "$rel")"
            while [ "$dir" != "." ]; do
                ups="../$ups"
                dir="$(dirname "$dir")"
            done
            rm -rf "$fw"
            ln -s "$ups/Frameworks/$rel" "$fw"
            continue
        fi
        if [ ! -L "$fw/Versions/Current" ]; then
            ver=""
            for entry in "$fw/Versions"/*; do
                [ -d "$entry" ] || continue
                [ "$(basename "$entry")" = "Current" ] && continue
                ver="$(basename "$entry")"
                break
            done
            if [ -n "$ver" ]; then
                rm -rf "$fw/Versions/Current"
            elif [ -d "$fw/Versions/Current" ]; then
                ver="A"
                mv "$fw/Versions/Current" "$fw/Versions/$ver"
            else
                echo "no version folder in $fw" >&2
                exit 1
            fi
            ln -s "$ver" "$fw/Versions/Current"
        fi
        for entry in "$fw"/*; do
            name="$(basename "$entry")"
            [ "$name" = "Versions" ] && continue
            [ -L "$entry" ] && continue
            if [ -e "$fw/Versions/Current/$name" ]; then
                rm -rf "$entry"
            else
                mv "$entry" "$fw/Versions/Current/$name"
            fi
            ln -s "Versions/Current/$name" "$entry"
        done
        echo "$fw"
    done
}

actool_major() {
    # actool_major ACTOOL - the Xcode version of an actool (26 for 26.1), or
    # nothing. Read from its own --version, as the developer folder it comes
    # from may be called anything.
    local dev
    dev="$(dirname "$(dirname "$(dirname "$1")")")"
    DEVELOPER_DIR="$dev" "$1" --version --output-format human-readable-text 2>/dev/null \
        | sed -n 's/.*short-bundle-version[^0-9]*\([0-9][0-9]*\).*/\1/p' | head -n 1 \
        || true
}

find_actool() {
    # Prints the first actool that can compile an Icon Composer file: Xcode
    # 26 or later. DEVELOPER_DIR first, then the selected Xcode, then the
    # usual places - xcode-select often points at the Command Line Tools,
    # which have no actool, while Xcode is installed all the same.
    local dev tool major
    for dev in "${DEVELOPER_DIR:-}" "$(xcode-select -p 2>/dev/null || true)" \
               /Applications/Xcode.app/Contents/Developer \
               /Applications/Xcode*.app/Contents/Developer; do
        [ -n "$dev" ] || continue
        tool="$dev/usr/bin/actool"
        [ -x "$tool" ] || continue
        major="$(actool_major "$tool")"
        [ -n "$major" ] && [ "$major" -ge 26 ] || continue
        echo "$tool"
        return 0
    done
    return 0
}

compile_icon() {
    # compile_icon ACTOOL OUTDIR - Assets.car from the Icon Composer file.
    # With the Xcode the actool belongs to (DEVELOPER_DIR), or it cannot
    # find icontool. Deployment target 26: the older Macs get the .icns.
    local tool="$1" out="$2" dev
    dev="$(dirname "$(dirname "$(dirname "$tool")")")"
    mkdir -p "$out"
    DEVELOPER_DIR="$dev" "$tool" "$ICON_SOURCE" --compile "$out" \
        --output-format human-readable-text --notices --warnings --errors \
        --output-partial-info-plist "$out/partial.plist" \
        --app-icon "$ICON_NAME" --include-all-app-icons \
        --enable-on-demand-resources NO --development-region en \
        --target-device mac --minimum-deployment-target 26.0 \
        --platform macosx || return 1
    [ -f "$out/Assets.car" ] || { echo "actool wrote no Assets.car" >&2; return 1; }
}

pyinstaller_args() {
    # The PyInstaller command line, one argument per line.
    #
    # --collect-all yt_dlp: the app re-enters itself to BE yt-dlp, and
    #   nothing imports it at module scope - nor its lazy-loaded extractors.
    # --collect-all yt_dlp_plugins + bgutil_ytdlp_pot_provider: the PO token
    #   provider is found through a namespace package. Without it every fetch
    #   is capped at 360p, and nothing says why.
    # --collect-all OpenGL: PyOpenGL chooses its platform backend at runtime.
    # --collect-all syphon: the Syphon framework travels inside the package.
    # --collect-all Metal / IOSurface + the PyObjC imports: the Syphon output
    #   (syphon_out._MetalPath) and the App Nap opt-out (macos.py), all
    #   imported inside functions, where PyInstaller does not look.
    # --add-data: what the first-run setup needs, next to the code.
    # --icon / assets/icon.png: the app's icon in the Finder and the Dock,
    #   and the picture its windows show (bootstrap.icon_file).
    cat <<EOF
--noconfirm
--clean
--name
ChuuTubePoi
--onedir
--windowed
--osx-bundle-identifier
$BUNDLE_ID
--collect-all
yt_dlp
--collect-all
yt_dlp_plugins
--collect-all
bgutil_ytdlp_pot_provider
--collect-all
OpenGL
--collect-all
syphon
--collect-all
Metal
--collect-all
IOSurface
--hidden-import
objc
--hidden-import
Foundation
--hidden-import
AppKit
--hidden-import
mpv
--exclude-module
tkinter
--exclude-module
unittest
--add-data
$ROOT/setup.sh:.
--add-data
$ROOT/checksums.txt:.
--add-data
$ROOT/settings.txt.example:.
--icon
$ROOT/assets/ChuuTubePoi.icns
--add-data
$ROOT/assets/icon.png:assets
$ROOT/run.py
EOF
}

main() {
    [ "$(uname -s)" = "Darwin" ] || die "This script is for macOS. On Windows, run build.ps1."
    cd "$ROOT"
    [ -x "$VENV_PY" ] || die "No .venv here - run 'bash setup.sh' first."

    : >"$LOG"

    # ------------------------------------------------------- preflight ------
    # As in build.ps1: a warning while testing, a must before publishing.
    if ! ls "$ROOT/licenses/"*.txt >/dev/null 2>&1; then
        warn "licenses/ is empty - the app bundles Qt (LGPL) and other libraries"
        warn "without their licence texts. Fine for testing; before publishing:"
        warn "    bash setup.sh --licenses"
    fi

    # Before the build, not after it: no Xcode is found out in a second,
    # not after minutes of PyInstaller.
    local actool=""
    if [ "${MAC_NO_ASSETS_CAR:-}" = "1" ]; then
        warn "MAC_NO_ASSETS_CAR=1: no Liquid Glass icon - macOS 26 and later may"
        warn "show the .icns in a grey frame. Fine for testing, not for publishing."
    else
        [ -d "$ICON_SOURCE" ] || die "assets/ChuuTubePoi.icon is missing"
        actool="$(find_actool)"
        [ -n "$actool" ] || die "Xcode 26 or later is needed for the app icon (Assets.car from
    assets/ChuuTubePoi.icon). Install Xcode from the App Store, open it once,
    then build again. For a test build without it:
    MAC_NO_ASSETS_CAR=1 bash build-mac.sh"
        say "Icon: $actool (Xcode $(actool_major "$actool"))"
    fi

    say "Running the tests first (about a minute)"
    logged "The tests" "$VENV_PY" -m unittest discover -s "$ROOT/tests" \
        || die "tests failed - not building a release from this"
    say "  $(grep -E '^Ran [0-9]+ tests' "$LOG" | tail -n 1), all passed"

    local version
    version="$("$VENV_PY" -c "import sys; sys.path.insert(0, '$ROOT'); from chuutubepoi import __version__; print(__version__)")"
    say "Building ChuuTubePoi $version"

    # The plugin built into the app decides which generator source the
    # first-run setup fetches; only the pinned one has a hash.
    local wanted installed
    wanted="$(sed -n 's/^[[:space:]]*bgutil-ytdlp-pot-provider[[:space:]]*==[[:space:]]*\([^[:space:];#]*\).*/\1/p' "$ROOT/requirements.txt" | head -n 1)"
    [ -n "$wanted" ] || die "requirements.txt does not pin bgutil-ytdlp-pot-provider with =="
    installed="$("$VENV_PY" -c "from importlib.metadata import version; print(version('bgutil-ytdlp-pot-provider'))" 2>/dev/null || true)"
    [ "$installed" = "$wanted" ] \
        || die "the venv has bgutil-ytdlp-pot-provider '$installed' but requirements.txt pins $wanted. Run setup.sh, then build again."
    say "PO token plugin $installed, as pinned"

    say "Installing PyInstaller into .venv"
    "$VENV_PY" -m pip install --upgrade pyinstaller --quiet \
        || die "could not install pyinstaller"

    # ----------------------------------------------------------- build ------
    rm -rf "$ROOT/build" "$ROOT/dist"
    local args=()
    local line
    while IFS= read -r line; do
        args+=("$line")
    done <<EOF
$(pyinstaller_args)
EOF
    say "Building the app with PyInstaller (a few minutes)"
    logged "PyInstaller" "$VENV_PY" -m PyInstaller "${args[@]}" \
        || die "PyInstaller failed"
    [ -d "$APP" ] || die "PyInstaller produced no app"

    # Version and the oldest macOS, in the app's Info.plist.
    local plist="$APP/Contents/Info.plist"
    plutil -replace CFBundleShortVersionString -string "$version" "$plist"
    plutil -replace CFBundleVersion -string "$version" "$plist"
    # PySide6 6.12 needs macOS 14; the app could not start on anything older.
    plutil -replace LSMinimumSystemVersion -string "14.0" "$plist"
    plutil -replace NSHighResolutionCapable -bool true "$plist"

    # ------------------------------------------------------------ icon ------
    # macOS 26 and later take CFBundleIconName from Assets.car; macOS 14 and
    # 15 take CFBundleIconFile, the .icns PyInstaller put in. Both before
    # signing: the signature covers them.
    [ "$(plutil -extract CFBundleIconFile raw "$plist" 2>/dev/null)" = "ChuuTubePoi.icns" ] \
        && [ -f "$APP/Contents/Resources/ChuuTubePoi.icns" ] \
        || die "the app has no ChuuTubePoi.icns - check --icon in pyinstaller_args"
    if [ -n "$actool" ]; then
        logged "Compiling the icon" compile_icon "$actool" "$ROOT/build/icon" \
            || die "actool could not compile assets/ChuuTubePoi.icon"
        cp "$ROOT/build/icon/Assets.car" "$APP/Contents/Resources/Assets.car"
        plutil -replace CFBundleIconName -string "$ICON_NAME" "$plist"
        say "Icon: Assets.car for macOS 26 and later, ChuuTubePoi.icns for older"
    fi

    # ------------------------------------------- what must not ship -------
    local found
    found="$(forbidden_in "$APP")"
    if [ -n "$found" ]; then
        printf '%s\n' "$found" >&2
        die "the app holds things that must not be published (above). Find out what put them there, then build again."
    fi
    say "Nothing in the app that should not be published"

    # ----------------------------------------------- frameworks, whole ------
    local restored fw
    restored="$(repair_frameworks "$APP")" \
        || die "a framework in the app is broken and could not be repaired (above)"
    [ -z "$(broken_frameworks "$APP")" ] \
        || die "still broken after the repair: $(broken_frameworks "$APP")"

    # ------------------------------------------------------------ sign ------
    # The whole bundle, after the Info.plist change. Apple Silicon runs
    # nothing unsigned. The repaired frameworks first, each by itself.
    if [ "$SIGN_ID" = "-" ]; then
        say "Signing ad hoc (macOS will ask for Full Disk Access again after"
        say "  installing this build - see MAC_SIGN_IDENTITY in RELEASING.md)"
    else
        # Not -v: that lists only certificates macOS trusts, and a self-made
        # one is not trusted (tester, 1.1: "no certificate named ..." while
        # it was in the keychain). codesign signs with it all the same.
        security find-identity -p codesigning | grep -qF "\"$SIGN_ID\"" \
            || die "no code signing certificate named '$SIGN_ID' in your keychain (RELEASING.md says how to make one)"
        say "Signing with '$SIGN_ID'"
    fi
    if [ -n "$restored" ]; then
        while IFS= read -r fw; do
            say "  repaired ${fw#"$APP/"}"
            logged "Signing ${fw##*/}" codesign --force --sign "$SIGN_ID" "$fw" \
                || die "codesign failed"
        done <<EOF
$restored
EOF
    fi
    logged "Signing" codesign --force --deep --sign "$SIGN_ID" "$APP" \
        || die "codesign failed (with a certificate: did the keychain ask for access? Allow it, then build again)"
    logged "Checking the signature" codesign --verify --deep --strict "$APP" \
        || die "the signature does not verify"

    # ----------------------------------------------------- smoke tests ------
    local exe="$APP/Contents/MacOS/ChuuTubePoi"
    say "Checking the app can run yt-dlp"
    local ytv
    ytv="$("$exe" --yt-dlp --version 2>&1 || true)"
    printf '%s\n' "$ytv" | grep -Eq '^[0-9]{4}\.[0-9]{2}\.[0-9]{2}' \
        || die "the app cannot run yt-dlp (got: '$ytv'). Check the --collect-all flags. Please send $LOG too."
    say "  yt-dlp $ytv"

    say "Checking the app's Syphon output (the window does not open)"
    "$exe" --syphon-check 2>&1 | tee -a "$LOG" \
        || die "--syphon-check failed in the built app - see above, and $LOG"

    # ------------------------------------------------------------- zip ------
    # A folder with the app and the documents beside it, as in the Windows
    # zip: the licence texts travel with what they cover. ditto, not zip:
    # it keeps the symlinks and signatures inside the bundle.
    local arch zip hash stage name
    arch="$(uname -m)"
    # Not dist/ChuuTubePoi: PyInstaller's own folder build is there.
    stage="$ROOT/dist/release/ChuuTubePoi"
    zip="$ROOT/dist/ChuuTubePoi-$version-macos-$arch.zip"
    say "Zipping"
    mkdir -p "$stage"
    ditto "$APP" "$stage/ChuuTubePoi.app"
    for name in LICENSE README.md SECURITY.md THIRD-PARTY-NOTICES.md; do
        cp "$ROOT/$name" "$stage/"
    done
    if [ -d "$ROOT/licenses" ]; then
        ditto "$ROOT/licenses" "$stage/licenses"
    fi
    if ! ls "$stage/licenses/"*.txt >/dev/null 2>&1; then
        warn "This zip has no licence texts: a test build, not for publishing."
        warn "For a release: bash setup.sh --licenses, then build again."
    fi
    ditto -c -k --sequesterRsrc --keepParent "$stage" "$zip"
    hash="$(shasum -a 256 "$zip" | awk '{ print $1 }')"

    echo
    say "Done: $zip"
    echo "    SHA-256: $hash"
    echo
    warn "Before publishing: test the zip on a Mac that has never run the app."
    warn "  * First start: it offers the one-time setup (Node.js, token"
    warn "    generator; libmpv via Homebrew if missing), then the window opens."
    warn "  * Resolume: ChuuTubePoi-A and -B upright, 4:3 bars transparent."
    warn "  * Help names the log and the settings folder."
}

if [ "${CHUUTUBE_BUILD_SOURCE_ONLY:-}" != "1" ]; then
    main "$@"
fi
