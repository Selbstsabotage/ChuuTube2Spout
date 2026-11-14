<#
    ChuuTubePoi release build (Windows)

    Produces dist\ChuuTubePoi\ - a folder that runs on a machine with no
    Python and no setup step - and zips it.

        powershell -ExecutionPolicy Bypass -File build.ps1

    ONEDIR, NOT ONEFILE, and not by accident. PySide6/Qt is LGPL-3.0, which
    requires that a user be able to replace the library with their own build.
    In a folder build the Qt DLLs are ordinary files sitting next to the exe, so
    that condition is met by construction. A onefile build buries them in a
    self-extracting blob and does not. Onedir also trips antivirus far less.

    Everything bundled here is redistributed BY YOU. THIRD-PARTY-NOTICES.md and
    licenses\ must be filled in first; this script checks and refuses otherwise.
#>

$ErrorActionPreference = "Stop"
$Root   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$Dist   = Join-Path $Root "dist\ChuuTubePoi"

function Say($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "!!  $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "XX  $m" -ForegroundColor Red; exit 1 }

function Get-RequirementPin([string]$Path, [string]$Package) {
    # The exact version requirements.txt pins for $Package (`name==1.2.3`), or
    # $null when it is not pinned that way.
    $pattern = '^\s*' + [regex]::Escape($Package) + '\s*==\s*([^\s#;]+)'
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match $pattern) { return $Matches[1] }
    }
    return $null
}

if (-not (Test-Path $VenvPy)) { Die "Run setup.ps1 first - no .venv here." }

# --------------------------------------------------- licence preconditions ---
# Refusing here rather than warning: a release that ships GPL and LGPL libraries
# without their licence texts is not a paperwork slip, it is distribution you do
# not have permission for.
$licenceTexts = @(Get-ChildItem (Join-Path $Root "licenses") -Filter *.txt -ErrorAction SilentlyContinue)
if ($licenceTexts.Count -eq 0) {
    Warn "licenses\ is empty - this build bundles Qt (LGPL) and libmpv (GPL,"
    Warn "undocumented by its distributor) without their licence texts."
    Warn "Fine while you are testing. Fill it in before publishing - RELEASING.md."
}
if ((Get-Content (Join-Path $Root "THIRD-PARTY-NOTICES.md") -Raw) -match "\*fill in\*") {
    Warn "THIRD-PARTY-NOTICES.md still has '*fill in*' placeholders in it."
    Warn "Ship it like that and the notices are wrong. Continuing anyway."
}

# ------------------------------------------------------------ preflight ------
Say "Running the tests first"
& $VenvPy -m unittest discover -s (Join-Path $Root "tests") -q
if ($LASTEXITCODE -ne 0) { Die "tests failed - not building a release from this" }

$version = (& $VenvPy -c "import sys; sys.path.insert(0,r'$Root'); from chuutubepoi import __version__; print(__version__)").Trim()
Say "Building ChuuTubePoi $version"

# checksums.txt has to be populated. It is the only thing that lets whoever
# downloads this release verify that the binaries they fetch are the ones that
# were tested here - and since those binaries are fetched rather than bundled
# (see below), it is the whole of the guarantee.
$pins = @(Get-Content (Join-Path $Root "checksums.txt") |
          Where-Object { $_.Trim() -and -not $_.Trim().StartsWith("#") })
if ($pins.Count -eq 0) {
    Die "checksums.txt has no entries. Run 'setup.ps1 -Portable -Pin' first, confirm the app works, then build. Without it every user fetches whatever upstream is serving that day, unverified."
}
Say "checksums.txt pins $($pins.Count) artifact(s)"

# The PO token plugin bundled into the exe decides which generator source the
# installer fetches, and only the pinned one has a hash in checksums.txt. So
# the venv has to hold exactly the pinned version. 0.9.9.24 was built with
# 2.0.2 against a 2.0.0 pin - setup.ps1 used to upgrade it after installing
# the pin - and nothing noticed until the zip was taken apart.
$plugin = "bgutil-ytdlp-pot-provider"
$wanted = Get-RequirementPin (Join-Path $Root "requirements.txt") $plugin
if (-not $wanted) { Die "requirements.txt does not pin $plugin with ==" }
$installed = "$(& $VenvPy -c "from importlib.metadata import version; print(version('$plugin'))" 2>$null)".Trim()
if ($installed -ne $wanted) {
    Die "the venv has $plugin '$installed' but requirements.txt pins $wanted. Run setup.ps1 (it installs the pin), then build again."
}
Say "PO token plugin $installed, as pinned"

Say "Installing PyInstaller into .venv"
& $VenvPy -m pip install --upgrade pyinstaller --quiet
if ($LASTEXITCODE -ne 0) { Die "could not install pyinstaller" }

# ---------------------------------------------------------------- build ------
Remove-Item -Recurse -Force (Join-Path $Root "build"), (Join-Path $Root "dist") -ErrorAction SilentlyContinue

# --collect-all yt_dlp: the exe re-enters itself to BE yt-dlp (see
#   bootstrap.ytdlp_command), and nothing imports it at module scope, so static
#   analysis would not see it - nor its thousand lazy-loaded extractors.
# --collect-all yt_dlp_plugins + bgutil_ytdlp_pot_provider: yt-dlp finds the PO
#   token provider through a namespace package, which PyInstaller cannot follow.
#   Without it every fetch is capped at 360p, and nothing says why.
# --collect-all OpenGL: PyOpenGL chooses its platform backend at runtime.
$pyiArgs = @(
    "--noconfirm", "--clean",
    "--name", "ChuuTubePoi",
    "--onedir", "--console",
    # The exe's own icon, and the picture its windows show (bootstrap.icon_file).
    "--icon", (Join-Path $Root "assets\ChuuTubePoi.ico"),
    "--add-data", ((Join-Path $Root "assets\icon.png") + ";assets"),
    "--collect-all", "yt_dlp",
    "--collect-all", "yt_dlp_plugins",
    "--collect-all", "bgutil_ytdlp_pot_provider",
    "--collect-all", "OpenGL",
    "--hidden-import", "SpoutGL",
    "--exclude-module", "tkinter",
    "--exclude-module", "unittest",
    (Join-Path $Root "run.py")
)
& $VenvPy -m PyInstaller @pyiArgs
if ($LASTEXITCODE -ne 0) { Die "PyInstaller failed - see above" }
if (-not (Test-Path $Dist)) { Die "PyInstaller produced no dist folder" }

# ------------------------------------------------- assemble the release ------
# The native runtime is deliberately NOT in the zip: not libmpv, not Node.js,
# not the token generator. Bundling them means redistributing them, and they are the
# parts that carry the heaviest terms - libmpv's Windows builds are GPL because
# of their codec components, and both halves of the token generator are GPL-3.0.
# Shipping them would make whoever publishes this release answerable for all of
# it, including a licence libmpv's distributor does not state.
#
# So the zip carries setup.ps1 and checksums.txt, and each user fetches the
# native pieces from the original projects, verified against the hashes recorded
# here. setup-runtime.bat below is the one command that does it.
#
# What IS redistributed is whatever PyInstaller folds into _internal\ - Qt,
# numpy, yt-dlp and their dependencies. Those still need licenses\, which is
# what the guard at the top of this script is about.
Say "Native runtime: not bundled, fetched by the user (see setup-runtime.bat)"

Say "Adding setup.ps1, so the user can fetch the runtime"
foreach ($name in @("setup.ps1", "checksums.txt")) {
    Copy-Item (Join-Path $Root $name) $Dist -Force
    Say "  + $name"
}

# The first thing anyone should run after unzipping. A .bat because the people
# this is for are VJs, not PowerShell users, and "right-click, Run with
# PowerShell" on a .ps1 is blocked by the default execution policy anyway.
$runtimeBat = @"
@echo off
REM Fetches the pieces that are not in the zip: libmpv, Node.js and the PO token
REM generator. They come from their own projects rather than from this release
REM so that nobody is redistributing them on your behalf. Needs the internet
REM once; after this the app runs offline apart from fetching clips.
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File setup.ps1 -RuntimeOnly
echo.
pause
"@
Set-Content -Path (Join-Path $Dist "setup-runtime.bat") -Value $runtimeBat -Encoding ASCII
Say "  + setup-runtime.bat  (run this first)"

# The source tree's .bat files all run ".venv\Scripts\python.exe run.py", which
# a release does not have - so they are written fresh here against the exe
# instead of being copied. 3.4.2 shipped with none of them at all, which left
# testers with no doctor, no check and no way to update yt-dlp.
Say "Adding the helper scripts"
$bats = @{
    "doctor.bat" = "ChuuTubePoi.exe --doctor"
    "clear-cache.bat" = "ChuuTubePoi.exe --clear-cache"
}
foreach ($name in $bats.Keys) {
    @(
        "@echo off",
        "setlocal",
        "cd /d ""%~dp0""",
        $bats[$name],
        "pause",
        "endlocal"
    ) | Set-Content -Path (Join-Path $Dist $name) -Encoding ASCII
    Say "  + $name"
}

# check.bat takes an argument, so it needs the usage guard the others do not.
# ARG is assigned and tested with "defined" rather than compared directly: a
# pasted URL can contain a quote, and `if "%~1"==""` unbalances the parse.
@(
    "@echo off",
    "rem Work out why a URL will not play.",
    "rem   check.bat ""https://www.youtube.com/watch?v=...""",
    "rem Quote the URL: cmd treats = and & as argument separators.",
    "setlocal",
    "cd /d ""%~dp0""",
    "set ""ARG=%~1""",
    "if not defined ARG (",
    "    echo usage: check.bat ""https://www.youtube.com/watch?v=...""",
    "    pause",
    "    exit /b 1",
    ")",
    "ChuuTubePoi.exe --check %*",
    "pause",
    "endlocal"
) | Set-Content -Path (Join-Path $Dist "check.bat") -Encoding ASCII
Say "  + check.bat"

# NOT a copy of the source update-ytdlp.bat. That one runs pip, and a release
# has no pip and no site-packages to install into - yt-dlp lives inside the exe.
# A script that appeared to update it and silently changed nothing would be
# worse than not shipping one.
@(
    "@echo off",
    "echo yt-dlp is built into ChuuTubePoi.exe and cannot be updated on its own.",
    "echo.",
    "echo If a URL that used to work has stopped loading, YouTube has most likely",
    "echo changed something and a newer release will carry a newer yt-dlp.",
    "echo.",
    "ChuuTubePoi.exe --yt-dlp --version",
    "pause"
) | Set-Content -Path (Join-Path $Dist "update-ytdlp.bat") -Encoding ASCII
Say "  + update-ytdlp.bat  (reports the bundled version)"

Say "Adding the documents"
foreach ($name in @("LICENSE", "THIRD-PARTY-NOTICES.md", "SECURITY.md",
                    "README.md")) {
    Copy-Item (Join-Path $Root $name) $Dist -Force
    Say "  + $name"
}
Copy-Item (Join-Path $Root "licenses") (Join-Path $Dist "licenses") -Recurse -Force
Say "  + licenses\ ($($licenceTexts.Count) texts)"

# The release ships settings.txt itself, made from the template, so users see
# one settings file instead of a .example to copy (K, 0.9.9.30). Never the
# settings.txt in this folder: on this machine that one holds YOUR browser
# choice. Whatever is in dist\ is replaced, and checked again below.
$template = Join-Path $Root "settings.txt.example"
$shipped = Join-Path $Dist "settings.txt"
Copy-Item $template $shipped -Force
Say "  + settings.txt  (from settings.txt.example: output size on, the rest off)"

# ------------------------------------------------- what must not ship -------
# Items 1 and 2 of the "before uploading" list used to be things a human was
# asked to check by opening the zip. Nobody opens the zip on the tenth build,
# and the one time it matters is the build where something got left behind -
# a -Portable run, or a first-run install done from dist\ to test the
# installer, which is a thing this project now actually does.
#
# PyInstaller's dist folder is wiped above, so this should never fire. It is
# here because the cost of it firing wrongly is ten seconds and the cost of it
# not existing is publishing somebody's session log or a GPL runtime nobody
# meant to redistribute.
$forbidden = @(
    @{ Path = "cache";           Why = "a record of what you played" }
    @{ Path = "logs";            Why = "your session logs" }
    @{ Path = "mpv-2.dll";       Why = "GPL runtime - the user fetches this" }
    @{ Path = "libmpv-2.dll";    Why = "GPL runtime - the user fetches this" }
    @{ Path = "node";            Why = "Node.js - the user fetches this" }
    @{ Path = "deno.exe";        Why = "no longer used (before 1.1)" }
    @{ Path = "bgutil-pot.exe";  Why = "GPL-3.0 - the user fetches this" }
    @{ Path = "bgutil-server";   Why = "GPL-3.0 - the user fetches this" }
    @{ Path = ".venv";           Why = "your build environment" }
)
$found = @()
foreach ($item in $forbidden) {
    $full = Join-Path $Dist $item.Path
    if (Test-Path $full) { $found += "  $($item.Path) - $($item.Why)" }
}
$want = (Get-FileHash $template -Algorithm SHA256).Hash
$have = if (Test-Path $shipped) { (Get-FileHash $shipped -Algorithm SHA256).Hash } else { "" }
if ($have -ne $want) {
    $found += "  settings.txt - not the template; may hold your browser choice"
}
if ($found.Count) {
    Write-Host ""
    foreach ($line in $found) { Write-Host $line -ForegroundColor Red }
    Die "the build folder holds $($found.Count) thing(s) that must not be published. Delete them from dist\ChuuTubePoi and build again."
}
Say "Nothing in the build that should not be published"

# ------------------------------------------------------- smoke test ---------
# The single most likely way this build is broken: yt-dlp did not get collected,
# so the app launches fine and then cannot fetch anything. Ask the exe.
Say "Checking the built exe can run yt-dlp"
$exe = Join-Path $Dist "ChuuTubePoi.exe"
$ytv = (& $exe --yt-dlp --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not ($ytv -match "\d{4}\.\d{2}\.\d{2}")) {
    Die "the built exe cannot run yt-dlp (got: '$ytv'). The build is not usable - check the --collect-all flags."
}
Say "  yt-dlp $ytv responds from inside the exe"

# ------------------------------------------------------------------ zip ------
$zip = Join-Path $Root "dist\ChuuTubePoi-$version-win64.zip"
Say "Zipping"
Compress-Archive -Path $Dist -DestinationPath $zip -Force
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()

Write-Host ""
Say "Done: $zip"
Write-Host "    SHA-256: $hash" -ForegroundColor Green
Write-Host ""
Warn "Before uploading:"
Warn "  1. Test it on a machine that has never run this - NOT this one. The"
Warn "     exe's first run takes a different path from run.bat, which is how"
Warn "     the -RuntimeOnly crash in 0.9.9.5 reached a build."
Warn "  2. Put the SHA-256 and the git tag in the release notes"
Write-Host ""
Warn "On the clean machine, in this order - each one is a path that has broken:"
Warn "  * FIRST RUN WITH NO RUNTIME. Double-click the exe with no libmpv"
Warn "    present. It must OFFER to install rather than just failing, the"
Warn "    install must finish, and the app must then open. Read the dialog:"
Warn "    if it fails it should point DOWN to the log, not up."
Warn "  * Senders before any video. Open Resolume with both slots empty."
Warn "    ChuuTubePoi-A and -B must already be in the source list,"
Warn "    transparent at the canvas size."
Warn "  * The canvas holds. Load a 4:3 or vertical clip and confirm it"
Warn "    arrives at the same size as everything else, with bars, unsquashed."
Warn "  * Sender lifetime: load a second clip, Clear a slot, close the app."
Warn "    Resolume should keep both senders across clip changes and Clear,"
Warn "    and not be left holding a dead sender after the app exits."
Warn "  * Cache expiry: with 'Clear cache on exit' unticked, confirm cache\"
Warn "    survives a restart and that clips older than CACHE_MAX_AGE_H are"
Warn "    gone after the next launch."
Warn "  * A log turned up in logs\ and the Help box names it."
