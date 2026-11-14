#!/bin/bash
# ChuuTubePoi setup (macOS)
#
# Creates a virtual environment, installs the Python packages, installs
# libmpv with Homebrew, fetches Node.js and builds the PO token generator.
# The Windows version of this script is setup.ps1; the two make the same
# choices, and the pins and hashes are shared (checksums.txt).
#
# Run from this folder:
#     bash setup.sh
#
# Before a release, fill licenses/ for the Mac app (as setup.ps1 -Licenses
# does on Windows):
#     bash setup.sh --licenses
#
# The macOS app (ChuuTubePoi.app) carries this script and runs it on its
# first start as
#     bash setup.sh --runtime-only
# with CHUUTUBE_ROOT set to its data folder: no Python, no venv - only
# libmpv (Homebrew), Node.js and the token generator.
#
# Needs: macOS 12 or newer, Python 3.10 to 3.14 (python.org or Homebrew),
# Homebrew (https://brew.sh) for libmpv, and the internet once.
#
# Written for the bash that ships with macOS (3.2): no arrays, no ${x,,}.

set -euo pipefail

# BASH_SOURCE, not $0: right when the tests source this file, too.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# Where things are installed: this folder, or the app's data folder.
ROOT="${CHUUTUBE_ROOT:-$HERE}"
# Where checksums.txt and settings.txt.example are: this folder, or the app.
RES="${CHUUTUBE_RESOURCES:-$HERE}"
VENV="$ROOT/.venv"
VENV_PY="$VENV/bin/python"

# --------------------------------------------------------------- pins -------
# The same Node.js release as setup.ps1, as a tarball for this Mac's
# processor. Its SHA-256 is in checksums.txt, like every other download.
NODE_VERSION="26.13.0"

say()  { printf '==> %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*" >&2; }
die()  { printf 'XX  %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------ helpers -------
# Everything that can be checked without a Mac is a function, so the test
# suite can run it on its own (CHUUTUBE_SETUP_SOURCE_ONLY=1).

expected_hash() {
    # The SHA-256 checksums.txt lists for file name $1, or nothing.
    awk -v n="$1" '$0 !~ /^[[:space:]]*#/ && NF == 2 && $2 == n { print $1; exit }' \
        "$RES/checksums.txt"
}

trusted_url() {
    # Only the hosts setup.ps1 allows. curl follows redirects (GitHub sends
    # archive downloads on to codeload.github.com); the hash check covers
    # where they lead.
    case "$1" in
        https://nodejs.org/*|https://github.com/*) return 0 ;;
        *) return 1 ;;
    esac
}

fetch_verified() {
    # fetch_verified URL OUTFILE NAME
    # Downloads URL to OUTFILE and checks it against checksums.txt. Stricter
    # than setup.ps1 on one point: a file with no hash there is refused, not
    # used with a warning. This script has no -Pin mode to fill one in.
    local url="$1" out="$2" name="$3" want got attempt
    trusted_url "$url" || die "refusing to download from an unexpected host: $url"
    want="$(expected_hash "$name")"
    [ -n "$want" ] || die "$name is not listed in checksums.txt - refusing to use it unverified"
    for attempt in 1 2 3; do
        if curl --fail --location --silent --show-error \
                --proto '=https' --proto-redir '=https' --tlsv1.2 \
                -o "$out" "$url"; then
            break
        fi
        rm -f "$out"
        [ "$attempt" = 3 ] && die "download of $name failed three times"
        warn "  download of $name failed (attempt $attempt of 3) - trying again"
        sleep $((3 * attempt))
    done
    got="$(shasum -a 256 "$out" | awk '{ print $1 }')"
    if [ "$got" != "$want" ]; then
        rm -f "$out"
        die "checksum mismatch for $name
  expected $want
  got      $got
Refusing to use it."
    fi
    say "  verified $name against checksums.txt"
}

unsafe_entries() {
    # Reads archive entry names on stdin; prints any that are absolute or
    # climb out with '..'. Checked BEFORE unpacking, as in setup.ps1: once a
    # file has been written outside the folder, nothing inside shows it.
    grep -E '(^/|(^|/)\.\.(/|$))' || true
}

links_escaping() {
    # links_escaping DIR - prints every symlink under DIR that points outside
    # DIR, or at nothing. setup.ps1 refuses links outright; the Node.js
    # tarball has to keep its own (bin/npm -> ../lib/node_modules/npm/bin/
    # npm-cli.js), so here a link is refused only if it leads out.
    # Plain shell: the macOS app has no Python to ask, and macOS 12 has no
    # realpath command. Each link is checked by its own target; a link that
    # leads to another link is caught when that one is checked.
    local root link target dir base resolved
    root="$(cd "$1" && pwd -P)" || return 1
    find "$root" -type l | while IFS= read -r link; do
        target="$(readlink "$link")"
        case "$target" in
            /*) dir="$(dirname "$target")" ;;
            *)  dir="$(dirname "$link")/$(dirname "$target")" ;;
        esac
        base="$(basename "$target")"
        if [ "$base" = "." ] || [ "$base" = ".." ]; then
            dir="$dir/$base"
            base=""
        fi
        if ! resolved="$(cd "$dir" 2>/dev/null && pwd -P)"; then
            echo "$link"
            continue
        fi
        [ -n "$base" ] && resolved="$resolved/$base"
        case "$resolved" in
            "$root"|"$root"/*) ;;
            *) echo "$link" ;;
        esac
    done
}

find_brew() {
    # Homebrew's brew: on PATH, or where it installs itself. An app started
    # from the Finder has only /usr/bin:/bin on its PATH.
    local b
    for b in "$(command -v brew 2>/dev/null || true)" /opt/homebrew/bin/brew /usr/local/bin/brew; do
        if [ -n "$b" ] && [ -x "$b" ]; then
            echo "$b"
            return 0
        fi
    done
    return 1
}

node_tarball_name() {
    # node_tarball_name MACHINE - the Node.js archive for `uname -m`.
    case "$1" in
        arm64|aarch64) echo "node-v$NODE_VERSION-darwin-arm64.tar.gz" ;;
        x86_64)        echo "node-v$NODE_VERSION-darwin-x64.tar.gz" ;;
        *) return 1 ;;
    esac
}

find_libmpv() {
    # The first libmpv.dylib in the folders the app searches (bootstrap.py:
    # the app's lib/, then Homebrew on Apple Silicon, then on Intel).
    local d
    for d in "$ROOT/lib" /opt/homebrew/lib /usr/local/lib; do
        if [ -e "$d/libmpv.dylib" ]; then
            echo "$d/libmpv.dylib"
            return 0
        fi
    done
    return 1
}

npm_ci() {
    # npm_ci NODE NPM_CLI - in the current folder. The same command as the
    # app's potprovider.npm_ci_command and as setup.ps1: ci (the lockfile,
    # every package checked against its recorded hash) and no install
    # scripts.
    "$1" "$2" ci --ignore-scripts --no-audit --no-fund
}

# Components pip knows nothing about, for --licenses: name=URL of the text.
# libmpv and Node.js are not in the app (setup fetches them), but their
# terms are what users agree to by running setup; the Syphon framework is
# inside the app, in syphon-python. Plain text from the projects themselves.
NATIVE_LICENSES="libmpv (mpv)=https://raw.githubusercontent.com/mpv-player/mpv/master/Copyright
Node.js=https://raw.githubusercontent.com/nodejs/node/main/LICENSE
Syphon framework=https://raw.githubusercontent.com/Syphon/Syphon-Framework/main/License.txt"

# Packages whose wheels carry no licence text (macOS wheels, 1.1). The text
# comes from the project and is saved as licenses/<package>.txt, under the
# exact name pip uses. The PySide6 wheels name LGPL-3.0 as the licence the app
# uses Qt under; the text is Qt's own copy.
QT_LGPL=https://raw.githubusercontent.com/qtproject/pyside-pyside-setup/dev/LICENSES/LGPL-3.0-only.txt
PYOBJC=https://raw.githubusercontent.com/ronaldoussoren/pyobjc/master
PACKAGE_LICENSES="bgutil-ytdlp-pot-provider=https://raw.githubusercontent.com/Brainicism/bgutil-ytdlp-pot-provider/master/LICENSE
PyOpenGL=https://raw.githubusercontent.com/mcfletch/pyopengl/master/license.txt
pyobjc-core=$PYOBJC/pyobjc-core/License.txt
pyobjc-framework-IOSurface=$PYOBJC/pyobjc-framework-IOSurface/License.txt
pyobjc-framework-Metal=$PYOBJC/pyobjc-framework-Metal/License.txt
PySide6=$QT_LGPL
PySide6_Addons=$QT_LGPL
PySide6_Essentials=$QT_LGPL
PySide6_Pdf=$QT_LGPL
PySide6_WebEngine=$QT_LGPL
shiboken6=$QT_LGPL"

fetch_text() {
    # fetch_text NAME URL OUTFILE - one licence text; kept if already there.
    local name="$1" url="$2" out="$3"
    [ -f "$out" ] && return 0
    # Texts, not code: the one place raw.githubusercontent.com is fetched.
    case "$url" in
        https://raw.githubusercontent.com/*) ;;
        *) warn "not fetching the licence of $name from $url"; return 0 ;;
    esac
    if curl -fsSL --proto '=https' --tlsv1.2 "$url" -o "$out"; then
        say "  fetched the licence of $name"
    else
        rm -f "$out"
        warn "could not fetch the licence of $name - get it from $url"
    fi
}

licenses() {
    # licenses - fills licenses/ from the venv, for the Mac app. Run after a
    # full setup, before build-mac.sh. See collect_licenses.py.
    [ -x "$VENV_PY" ] || die "no .venv yet - run 'bash setup.sh' first"
    local dir="$ROOT/licenses" site name url line
    mkdir -p "$dir"
    site="$("$VENV_PY" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")"
    local args=()
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        name="${line%%=*}"
        url="${line#*=}"
        args+=("$line")
        fetch_text "$name" "$url" \
            "$dir/$(printf '%s' "$name" | sed 's/[^A-Za-z0-9._-]/-/g').txt"
    done <<EOF
$NATIVE_LICENSES
EOF
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        name="${line%%=*}"
        url="${line#*=}"
        fetch_text "$name" "$url" "$dir/$name.txt"
    done <<EOF
$PACKAGE_LICENSES
EOF
    "$VENV_PY" "$ROOT/collect_licenses.py" "$dir" "$site" "${args[@]}" \
        || warn "some licence texts are missing - see licenses/INVENTORY.md"
    say "Check licenses/INVENTORY.md against THIRD-PARTY-NOTICES.md before publishing."
}

# --------------------------------------------------------------- main -------
main() {
    [ "$(uname -s)" = "Darwin" ] || die "This script is for macOS. On Windows, run setup.ps1."
    if [ "${1:-}" = "--licenses" ]; then
        licenses
        return
    fi
    local runtime_only=""
    [ "${1:-}" = "--runtime-only" ] && runtime_only=1
    mkdir -p "$ROOT"
    cd "$ROOT"
    SCRATCH="$(mktemp -d -t chuutube-setup)"
    trap 'rm -rf "$SCRATCH"' EXIT

    if [ -n "$runtime_only" ]; then
        say "Setting up the app's runtime in $ROOT"
    else
        setup_python
    fi
    setup_runtime "$runtime_only"
}

setup_python() {
    # ----------------------------------------------------------- Python -----
    # 3.10 to 3.14: syphon-python, the Syphon binding, supports exactly those.
    say "Looking for Python 3.10 to 3.14"
    local py="" cand ver
    for cand in python3.13 python3.12 python3.14 python3.11 python3.10 python3; do
        command -v "$cand" >/dev/null 2>&1 || continue
        ver="$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
        case "$ver" in
            3.10|3.11|3.12|3.13|3.14) py="$cand"; say "Found Python $ver ($cand)"; break ;;
        esac
    done
    [ -n "$py" ] || die "No Python 3.10 to 3.14 found. Install Python 3.13 from https://www.python.org/downloads/macos/ and run this again."

    if [ ! -x "$VENV_PY" ]; then
        say "Creating virtual environment in .venv"
        "$py" -m venv "$VENV" || die "venv creation failed"
    else
        say "Using existing .venv"
    fi
    say "Installing Python packages (this takes a minute - PySide6 is large)"
    "$VENV_PY" -m pip install --upgrade pip --quiet
    "$VENV_PY" -m pip install -r "$ROOT/requirements.txt" || die "pip install failed - see the output above"
}

setup_runtime() {
    # setup_runtime [RUNTIME_ONLY] - libmpv, Node.js, the token generator and
    # settings.txt. RUNTIME_ONLY: run by the app, which has no venv.
    local runtime_only="${1:-}"
    # ----------------------------------------------------------- libmpv -----
    # There is no macOS counterpart of the Windows libmpv download. Homebrew
    # keeps mpv current for both Apple Silicon and Intel.
    local mpvlib="" brew=""
    if mpvlib="$(find_libmpv)"; then
        say "libmpv found: $mpvlib"
    elif brew="$(find_brew)"; then
        say "Installing libmpv with Homebrew: brew install mpv (a few minutes)"
        "$brew" install mpv || warn "brew install mpv failed - see the output above"
        mpvlib="$(find_libmpv || true)"
    else
        warn "Homebrew is not installed, so libmpv cannot be installed."
        warn "Install Homebrew from https://brew.sh, then run this script again."
    fi

    # ------------------------------------------------------------- node -----
    # yt-dlp's challenge solver and the token generator run on Node.js. The
    # whole release goes into the folder "node"; nothing is installed
    # system-wide, and a Node.js elsewhere on this Mac is not used.
    local node_dir="$ROOT/node" node_exe="$ROOT/node/bin/node"
    local npm_cli="$ROOT/node/lib/node_modules/npm/bin/npm-cli.js"
    local have="" tarball
    [ -x "$node_exe" ] && have="$("$node_exe" --version 2>/dev/null || true)"
    if [ "$have" = "v$NODE_VERSION" ] && [ -f "$npm_cli" ]; then
        say "Node.js $have already present - skipping download"
    elif tarball="$(node_tarball_name "$(uname -m)")"; then
        say "Fetching Node.js v$NODE_VERSION (JavaScript runtime)"
        fetch_verified "https://nodejs.org/dist/v$NODE_VERSION/$tarball" \
                       "$SCRATCH/$tarball" "$tarball"
        local bad
        bad="$(tar -tzf "$SCRATCH/$tarball" | unsafe_entries)"
        [ -z "$bad" ] || die "refusing $tarball: it would write outside its folder:
$bad"
        mkdir "$SCRATCH/node"
        tar -xzf "$SCRATCH/$tarball" -C "$SCRATCH/node"
        bad="$(links_escaping "$SCRATCH/node")"
        [ -z "$bad" ] || die "refusing $tarball: links that lead outside it:
$bad"
        local inner="$SCRATCH/node/${tarball%.tar.gz}"
        [ -x "$inner/bin/node" ] || die "$tarball held no bin/node"
        rm -rf "$node_dir"
        mv "$inner" "$node_dir"
        say "Installed Node.js v$NODE_VERSION in the folder 'node'"
    else
        warn "No Node.js build for this processor ($(uname -m))."
    fi

    # -------------------------------------------- token generator source ----
    # The source matching the installed plugin version, so the two cannot
    # disagree about protocol. Same archive and hash as on Windows.
    local plugin_ver server_dir
    # The app says which plugin it carries; from source, the venv is asked.
    plugin_ver="${CHUUTUBE_PLUGIN_VERSION:-}"
    if [ -z "$plugin_ver" ] && [ -z "$runtime_only" ]; then
        plugin_ver="$("$VENV_PY" -c "from importlib.metadata import version; print(version('bgutil-ytdlp-pot-provider'))" 2>/dev/null || true)"
    fi
    if [ -z "$plugin_ver" ]; then
        [ -n "$runtime_only" ] && die "the app did not say which PO token plugin it carries (CHUUTUBE_PLUGIN_VERSION)"
        die "the PO token plugin is not installed - pip install failed?"
    fi
    server_dir="$ROOT/bgutil-server/bgutil-ytdlp-pot-provider-$plugin_ver/server"
    if [ -f "$server_dir/src/main.ts" ] || [ -f "$server_dir/src/generate_once.ts" ]; then
        say "Token generator source for $plugin_ver already present"
    else
        say "Fetching the token generator source matching plugin $plugin_ver"
        local zip="$SCRATCH/bgutil-ytdlp-pot-provider-$plugin_ver.zip"
        fetch_verified "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/$plugin_ver.zip" \
                       "$zip" "bgutil-ytdlp-pot-provider-$plugin_ver.zip"
        local bad
        bad="$(unzip -Z1 "$zip" | unsafe_entries)"
        [ -z "$bad" ] || die "refusing the generator archive: it would write outside its folder:
$bad"
        rm -rf "$ROOT/bgutil-server"
        mkdir -p "$ROOT/bgutil-server"
        unzip -q "$zip" -d "$ROOT/bgutil-server"
        bad="$(links_escaping "$ROOT/bgutil-server")"
        [ -z "$bad" ] || die "refusing the generator archive: links that lead outside it:
$bad"
        say "Token generator source installed"
    fi

    # --------------------------------------------- token generator build ----
    # Node.js runs it compiled from build/. npm ci, then tsc - as setup.ps1
    # does, and as the app repeats by itself if this fails.
    if [ -f "$server_dir/build/main.js" ] && [ -f "$server_dir/build/generate_once.js" ] \
            && [ -d "$server_dir/node_modules" ]; then
        say "Token generator already built"
    elif [ ! -x "$node_exe" ] || [ ! -f "$npm_cli" ]; then
        warn "Node.js is not installed (see above), so the token generator cannot be built."
    else
        say "Building the token generator with Node.js (one-time, a few minutes)"
        export npm_config_update_notifier=false npm_config_fund=false
        export npm_config_audit=false npm_config_color=false
        if ! (cd "$server_dir" && npm_ci "$node_exe" "$npm_cli"); then
            warn "npm ci failed - trying once more"
            (cd "$server_dir" && npm_ci "$node_exe" "$npm_cli") || true
        fi
        (cd "$server_dir" && "$node_exe" node_modules/typescript/bin/tsc) || true
        if [ -f "$server_dir/build/main.js" ] && [ -f "$server_dir/build/generate_once.js" ]; then
            say "Token generator built"
        else
            warn "Building the token generator failed - see the output above."
            warn "The app tries again on its first start."
        fi
    fi

    # --------------------------------------------------------- settings -----
    if [ ! -f "$ROOT/settings.txt" ]; then
        cp "$RES/settings.txt.example" "$ROOT/settings.txt"
        say "Created settings.txt - edit it to change options"
    else
        say "settings.txt is in place - edit it to change options"
    fi

    # ---------------------------------------------------------- verdict -----
    # Everything above warns and carries on. libmpv is the one piece the app
    # cannot open without.
    [ -n "$mpvlib" ] || die "libmpv is not installed, so the app cannot start. Install Homebrew from https://brew.sh, then run 'brew install mpv' in Terminal."

    if [ -n "$runtime_only" ]; then
        say "Done."
        return 0
    fi
    say "Running diagnostics"
    "$VENV_PY" "$ROOT/run.py" --doctor || true
    echo
    say "Done. Start the app with:  bash run.sh"
}

if [ "${CHUUTUBE_SETUP_SOURCE_ONLY:-}" != "1" ]; then
    main "$@"
fi
