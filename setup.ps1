<#
    ChuuTube2Spout setup (Windows)

    Creates a virtual environment, installs the Python dependencies, and fetches
    libmpv - the one piece that cannot come from pip.

    Run from this folder:
        powershell -ExecutionPolicy Bypass -File setup.ps1
#>

param(
    # Force every dependency into this folder, even ones already on your PATH,
    # so the whole thing can be copied to another machine.
    [switch]$Portable,

    # Record the SHA-256 of everything downloaded during this run into
    # checksums.txt, so later installs get exactly what you just tested.
    # Only fills in entries that are missing; it never overwrites one.
    [switch]$Pin,

    # Fetch only the native runtime - libmpv, deno, the token generator - and
    # skip Python entirely. This is what a downloaded RELEASE runs: the exe has
    # its own Python inside, but the third-party binaries are deliberately not
    # bundled, because redistributing them would make the person publishing the
    # release responsible for licence terms that upstream does not state. This
    # way each user fetches them from the original project, verified against
    # checksums.txt.
    [switch]$RuntimeOnly,

    # Rebuild licenses\ and licenses\INVENTORY.md from what is actually
    # installed, then stop. Read out of each package's own .dist-info rather
    # than from a list someone maintains by hand: a licence can change between
    # releases of a dependency, and the row that was right last year is not
    # evidence about the wheel in your venv today.
    [switch]$Licenses
)

$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 redraws Invoke-WebRequest's progress bar for every
# chunk it receives, which makes a large download many times slower than the
# link allows - and a download held open that long is one more chance for the
# connection to be dropped. Nothing reads the bar: the first-run window shows
# our own lines, not PowerShell's progress records.
$ProgressPreference = "SilentlyContinue"
# OR it in rather than assigning: a bare assignment also switches TLS 1.3 off
# for the rest of the process. Wrapped because the enum member does not exist on
# .NET < 4.5, and $ErrorActionPreference = "Stop" would kill the script here -
# on exactly the old machines the line exists to help.
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

function New-ScratchDir([string]$Tag) {
    # A fresh, unguessable directory each run. Fixed names under %TEMP% are
    # published in a public repo, and New-Item -Force silently adopts whatever
    # is already there - including a directory junction planted in advance,
    # which a later Remove-Item -Recurse would then follow out of the sandbox.
    $path = Join-Path $env:TEMP ("chuutube-" + $Tag + "-" + [guid]::NewGuid().ToString('n'))
    New-Item -ItemType Directory -Force -Path $path | Out-Null
    return $path
}

# ----------------------------------------------------------------- pins ------
# Every fetched artifact is pinned twice: to an exact VERSION here, and to an
# exact HASH in checksums.txt. They move together or not at all.
#
# This used to be half a pin. The URLs said "latest" while the hashes stood
# still, so the pair could not stay in agreement - and when upstream shipped,
# setup did not degrade, it refused, with a checksum mismatch indistinguishable
# from an attack. That landed on whoever ran setup next, which is a stranger.
#
# TO UPDATE one: bump its version here, delete its line from checksums.txt, run
# -Pin, confirm the app still works, then cross-check the recorded hash against
# the SHA-256 GitHub shows on the release asset itself before committing both
# files. That cross-check is the whole point: -Pin alone only records what you
# happened to download.
$PINNED = @{
    # The tested build. The publisher deletes all but its latest 30 builds,
    # so this one disappears within weeks - see Get-LatestMpvAsset for what
    # happens then. Bump it as part of every release.
    Mpv      = @{ Tag = "20261021"
                  Name = "mpv-dev-x86_64-20261021-git-e471496c56.7z" }
    Deno     = @{ Tag = "v2.9.7"
                  Name = "deno-x86_64-pc-windows-msvc.zip" }
    # From ip7z/7zip rather than 7-zip.org/a/7zr.exe: that URL is unversioned
    # and the site publishes no checksums at all, so a pin against it could
    # only ever mean "whatever was served the day I ran -Pin".
    SevenZip = @{ Tag = "26.03"; Name = "7zr.exe" }
    # A spare, not the primary. See the fetch block for why it is often skipped.
    PotExe   = @{ Tag = "v0.8.1"; Version = "0.8.1"
                  Name = "bgutil-pot-windows-x86_64.exe" }
}

function Assert-TrustedUrl([string]$Url) {
    # The download URLs are read out of GitHub API responses, not hardcoded, and
    # Invoke-WebRequest follows redirects silently. Assert where we ended up.
    if ($Url -notmatch '^https://([a-z0-9-]+\.)*(github\.com|githubusercontent\.com|7-zip\.org)/') {
        throw "refusing to download from an unexpected host: $Url"
    }
}

function Get-Verified([string]$Url, [string]$OutFile, [string]$Name,
                      [string]$ExpectedHash = "") {
    # Downloads, then checks the hash against checksums.txt if that file lists
    # this artifact - or against -ExpectedHash, for a file checksums.txt cannot
    # list because it did not exist when this release was made. Publishing a checksum for a binary you executed once is the
    # only thing that distinguishes "what the author tested" from "whatever the
    # supplier is serving today" - see SECURITY.md.
    Assert-TrustedUrl $Url
    # Three attempts. Observed on 0.9.9.17: "The request was aborted: The
    # connection was closed unexpectedly" on the 30 MB libmpv archive, twice in
    # a row, on a connection that fetched deno from GitHub fine in the same
    # run. Why the connection dropped is not known; a retry costs nothing when
    # the first attempt works. A partial file is deleted before each retry,
    # and the hash check below is what decides whether the result is usable.
    $attempts = 3
    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        try {
            Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
            break
        } catch {
            Remove-Item $OutFile -Force -ErrorAction SilentlyContinue
            if ($attempt -eq $attempts) { throw }
            Warn "  download of $Name failed (attempt $attempt of $attempts): $($_.Exception.Message)"
            Warn "  trying again"
            Start-Sleep -Seconds (3 * $attempt)
        }
    }
    $expected = if ($ExpectedHash) { $ExpectedHash } else { Get-ExpectedHash $Name }
    if ($expected) {
        $actual = (Get-FileHash $OutFile -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $expected.ToLower()) {
            Remove-Item $OutFile -Force -ErrorAction SilentlyContinue
            throw "checksum mismatch for $Name`n  expected $expected`n  got      $actual`nRefusing to use it."
        }
        if ($ExpectedHash) {
            Say "  verified $Name against the SHA-256 GitHub publishes for it"
        } else {
            Say "  verified $Name against checksums.txt"
        }
    } elseif ($Pin) {
        $actual = (Get-FileHash $OutFile -Algorithm SHA256).Hash.ToLower()
        Add-Content -Path (Join-Path $Root "checksums.txt") -Value "$actual  $Name"
        Say "  pinned $Name -> $actual"
    } else {
        Warn "  $Name is not listed in checksums.txt - cannot verify what was downloaded"
        Warn "  (re-run with -Pin once you have confirmed the app works, to record it)"
    }
}

function Get-LatestMpvAsset {
    # The newest libmpv build, for when the pinned one has been deleted:
    # shinchiro/mpv-winbuild-cmake's release workflow keeps only its latest 30
    # builds, and publishes almost daily. Observed 2026-10-31: the pinned
    # 20260903 was gone (404) six weeks after it was pinned, and every
    # first-run install failed. A fixed pin alone would do the same to every
    # published release a few weeks after it went out.
    #
    # Verified against the "sha256:" digest GitHub publishes for the asset.
    # Weaker than our own pin - it says the file is what the publisher
    # uploaded, not that this release was tested with it - and setup says so.
    # No digest, no install: it does not fall back to unverified.
    $api = "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"
    Assert-TrustedUrl $api
    $release = Invoke-RestMethod -Uri $api -UseBasicParsing `
                                 -Headers @{ Accept = "application/vnd.github+json" }
    # Exactly the dev archive for plain x86_64 - not -v3-, not aarch64, and
    # not the player build, which has no libmpv-2.dll in it.
    $asset = @($release.assets | Where-Object {
        $_.name -match '^mpv-dev-x86_64-\d{8}-git-[0-9a-f]+\.7z$' }) |
        Select-Object -First 1
    if (-not $asset) {
        throw "the newest build ($($release.tag_name)) has no mpv-dev-x86_64 archive"
    }
    if ("$($asset.digest)" -notmatch '^sha256:([0-9a-fA-F]{64})$') {
        throw "GitHub publishes no SHA-256 for $($asset.name) - refusing to install it unverified"
    }
    Assert-TrustedUrl $asset.browser_download_url
    return @{ Tag = "$($release.tag_name)"; Name = "$($asset.name)"
              Url = "$($asset.browser_download_url)"; Sha256 = $Matches[1].ToLower() }
}

function Get-ExpectedHash([string]$Name) {
    $file = Join-Path $Root "checksums.txt"
    if (-not (Test-Path $file)) { return $null }
    foreach ($line in Get-Content $file) {
        $t = $line.Trim()
        if ($t -eq "" -or $t.StartsWith("#")) { continue }
        $parts = $t -split '\s+', 2
        if ($parts.Count -eq 2 -and $parts[1].Trim() -eq $Name) { return $parts[0].Trim() }
    }
    return $null
}

# ------------------------------------------------------- zip-slip containment
# Whether a hostile archive can write outside the extraction folder otherwise
# depends on which extractor this Windows build happened to have, which is not
# a security property. Two checks, one either side of the extraction:
#
#   Assert-SafeEntries  BEFORE: the archive's own list of names. An entry that
#                       is rooted or climbs out with '..' is refused before a
#                       single byte is written. This is the check that can
#                       actually see an escape - once a file has been written
#                       outside the folder, nothing inside the folder shows it.
#   Assert-NoLinks      AFTER: nothing extracted may be a link. A symlink or
#                       junction inside the folder is the other way out, for
#                       whatever is written through it later.
#
# These replaced Assert-NoEscape in 0.9.9.17. It listed the folder and checked
# that every path started with the folder's path, which could never catch an
# escape - everything a listing of the folder returns is inside it. On a
# tester's machine it rejected every file of a genuine archive, and the
# first-run install stopped at libmpv. The likely reason, not yet confirmed on
# that machine: the folder's path came from Resolve-Path, which keeps an 8.3
# short name (C:\Users\LONGNA~1\...), while Get-ChildItem returned the long one.

function Test-UnsafeEntry([string]$Name) {
    # Rooted (\x, /x, \\server\share, C:x, C:\x) or climbing out ('..' as any
    # segment, with either slash). A name that merely CONTAINS '..' - say
    # "notes..txt" - is fine.
    if ($Name -match '^[\\/]' -or $Name -match '^[A-Za-z]:') { return $true }
    return [bool](@($Name -split '[\\/]') -contains '..')
}

function Assert-SafeEntries([string[]]$Names, [string]$Archive) {
    $leaf = Split-Path -Leaf $Archive
    # An archive that could not be listed is not extracted unchecked.
    if (-not $Names -or $Names.Count -eq 0) {
        throw "could not list the contents of $leaf - refusing to extract it unchecked"
    }
    foreach ($n in $Names) {
        if (Test-UnsafeEntry $n) {
            throw "refusing to extract ${leaf}: the entry '$n' would be written outside the extraction folder"
        }
    }
}

function Get-ZipEntryNames([string]$Zip) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($Zip)
    try { return @($z.Entries | ForEach-Object { $_.FullName }) }
    finally { $z.Dispose() }
}

function Get-7zEntryNames([string]$SevenZip, [string]$Archive) {
    # -slt prints a "Path = " line per entry, after a line of ten dashes. The
    # block before that line describes the archive itself and has a Path of
    # its own - the archive's filename - which is not an entry.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $out = @(& $SevenZip l -slt $Archive 2>$null); $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $prev }
    if ($code -ne 0) { return @() }
    $body = $false
    $names = @()
    foreach ($line in $out) {
        if ($line -eq "----------") { $body = $true; continue }
        if ($body -and $line.StartsWith("Path = ")) { $names += $line.Substring(7) }
    }
    return $names
}

function Assert-NoLinks([string]$Dir) {
    # No -ErrorAction SilentlyContinue: a part of the tree that cannot be read
    # is a part that was not checked.
    Get-ChildItem -LiteralPath $Dir -Recurse -Force | ForEach-Object {
        if ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "the archive contained a link, which is refused: $($_.FullName)"
        }
    }
}

$Root   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Venv   = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"

# The plugin version lives in two different places depending on how the app was
# installed. A source checkout has it in the venv; a built bundle has no venv at
# all - PyInstaller leaves the dist-info folder beside the frozen modules. Ask
# whichever one exists, so -RuntimeOnly is not left reaching for a python.exe
# that was never created.
function Get-PluginVersion {
    if (Test-Path $VenvPy) {
        $v = (& $VenvPy -c "from importlib.metadata import version; print(version('bgutil-ytdlp-pot-provider'))" 2>$null)
        if ($v) { return $v.Trim() }
    }
    $internal = Join-Path $Root "_internal"
    if (Test-Path $internal) {
        $dist = Get-ChildItem -Path $internal -Filter "bgutil_ytdlp_pot_provider-*.dist-info" `
                              -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($dist -and $dist.Name -match "^bgutil_ytdlp_pot_provider-(.+)\.dist-info$") {
            return $Matches[1]
        }
    }
    return $null
}

function Get-DistLicense($distInfo) {
    # Returns @{ Name; Version; Expression; Classifiers; Text; TextSource }
    $meta = Join-Path $distInfo.FullName "METADATA"
    if (-not (Test-Path $meta)) { return $null }
    $lines = Get-Content $meta

    $name = ($lines | Where-Object { $_ -match "^Name:\s*(.+)$" } |
             Select-Object -First 1) -replace "^Name:\s*", ""
    $ver = ($lines | Where-Object { $_ -match "^Version:\s*(.+)$" } |
            Select-Object -First 1) -replace "^Version:\s*", ""

    # Newest metadata says it outright; older ones only hint via classifiers.
    $expr = ($lines | Where-Object { $_ -match "^License-Expression:\s*(.+)$" } |
             Select-Object -First 1) -replace "^License-Expression:\s*", ""
    if (-not $expr) {
        $bare = ($lines | Where-Object { $_ -match "^License:\s*(.+)$" } |
                 Select-Object -First 1) -replace "^License:\s*", ""
        if ($bare -and $bare.Length -lt 80) { $expr = $bare }
    }
    $classifiers = @($lines | Where-Object { $_ -match "^Classifier: License :: " } |
                     ForEach-Object { $_ -replace "^Classifier: License :: ", "" })

    # Wheels built to PEP 639 put the text under .dist-info\licenses\; older
    # ones drop a LICENSE beside METADATA. Some ship nothing at all, which is
    # the case worth knowing about, so it is reported rather than papered over.
    $text = $null; $source = $null
    $candidates = @()
    $licDir = Join-Path $distInfo.FullName "licenses"
    if (Test-Path $licDir) {
        $candidates += Get-ChildItem $licDir -File -Recurse
    }
    $candidates += Get-ChildItem $distInfo.FullName -File |
                   Where-Object { $_.Name -match "^(LICEN[CS]E|COPYING|NOTICE)" }
    if ($candidates.Count -gt 0) {
        $text = ($candidates | ForEach-Object {
            "===== $($_.Name) =====`r`n" + (Get-Content $_.FullName -Raw)
        }) -join "`r`n`r`n"
        $source = ($candidates | ForEach-Object { $_.Name }) -join ", "
    }

    return @{ Name = $name; Version = $ver; Expression = $expr
              Classifiers = $classifiers; Text = $text; TextSource = $source }
}

function Say($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "XX  $msg" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- Python -----
if ($RuntimeOnly) {
    Say "Runtime only - skipping Python, the virtual environment and pip"
} else {
Say "Looking for Python 3.10+"
$py = $null
foreach ($cand in @("py -3", "python", "python3")) {
    $parts = $cand.Split(" ")
    $exe = Get-Command $parts[0] -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    $argsList = @()
    if ($parts.Length -gt 1) { $argsList += $parts[1] }
    $argsList += @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
    try { $ver = & $parts[0] @argsList 2>$null } catch { continue }
    if (-not $ver) { continue }
    $maj, $min = $ver.Trim().Split(".")
    if ([int]$maj -eq 3 -and [int]$min -ge 10) {
        $py = $cand
        Say "Found Python $ver via '$cand'"
        break
    }
}
if (-not $py) {
    Die "No Python 3.10+ found. Install it from https://www.python.org/downloads/ and tick 'Add python.exe to PATH', then re-run this script."
}

# ------------------------------------------------------------------ venv -----
if (-not (Test-Path $VenvPy)) {
    Say "Creating virtual environment in .venv"
    $parts = $py.Split(" ")
    $argsList = @()
    if ($parts.Length -gt 1) { $argsList += $parts[1] }
    $argsList += @("-m", "venv", $Venv)
    & $parts[0] @argsList
    if (-not (Test-Path $VenvPy)) { Die "venv creation failed" }
} else {
    Say "Using existing .venv"
}

Say "Installing Python packages (this takes a minute - PySide6 is large)"
& $VenvPy -m pip install --upgrade pip --quiet
& $VenvPy -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { Die "pip install failed - see the output above" }
}

# ---------------------------------------------------------------- licences ---
if ($Licenses) {
    $licDir = Join-Path $Root "licenses"
    New-Item -ItemType Directory -Force -Path $licDir | Out-Null
    $sitePkgs = Join-Path $Root ".venv\Lib\site-packages"
    if (-not (Test-Path $sitePkgs)) { Die "no .venv yet - run setup.ps1 first" }

    Say "Reading licences out of the installed packages"
    $rows = @()
    $missing = @()
    # Texts that are in licenses\ but did not come out of a wheel. Tracked
    # separately because "the package shipped this" and "a person fetched this"
    # are different claims, and the value of a generated inventory is that it
    # does not blur them.
    $byHand = @()
    foreach ($dist in (Get-ChildItem $sitePkgs -Directory -Filter *.dist-info |
                       Sort-Object Name)) {
        $info = Get-DistLicense $dist
        if (-not $info) { continue }
        $declared = if ($info.Expression) { $info.Expression }
                    elseif ($info.Classifiers.Count) { $info.Classifiers -join "; " }
                    else { "NOT DECLARED" }
        if ($info.Text) {
            $file = "$($info.Name)-$($info.Version).txt"
            Set-Content -Path (Join-Path $licDir $file) -Value $info.Text `
                        -Encoding UTF8
        } else {
            # The wheel carried no text - but somebody may already have fetched
            # it by hand, which is exactly what the checklist in
            # THIRD-PARTY-NOTICES.md tells them to do. Look before declaring it
            # missing, or this file permanently contradicts the folder it is
            # describing, and the one step of the publish checklist that CAN be
            # finished can never be marked done.
            $file = ""
            foreach ($candidate in @("$($info.Name)-$($info.Version).txt",
                                     "$($info.Name).txt")) {
                if (Test-Path (Join-Path $licDir $candidate)) {
                    $file = $candidate
                    $byHand += $candidate
                    break
                }
            }
            if (-not $file) { $missing += "$($info.Name) $($info.Version)" }
        }
        $rows += [pscustomobject]@{
            Name = $info.Name; Version = $info.Version
            Declared = $declared; File = $file
            ByHand = ($file -and $byHand -contains $file)
        }
    }

    # The four that pip knows nothing about, because they are not pip packages.
    # Their texts have to be fetched from the projects themselves; the URLs are
    # recorded here so the next person does not have to go looking.
    $native = @(
        @{ Name = "libmpv (mpv)"; Why = "playback"
           Url = "https://raw.githubusercontent.com/mpv-player/mpv/master/Copyright" }
        @{ Name = "Spout2"; Why = "texture sharing, inside SpoutGL"
           Url = "https://raw.githubusercontent.com/leadedge/Spout2/master/LICENSE" }
        @{ Name = "deno"; Why = "runs the token generator"
           Url = "https://raw.githubusercontent.com/denoland/deno/main/LICENSE.md" }
        @{ Name = "bgutil-pot (Rust port)"; Why = "standalone token server"
           Url = "https://raw.githubusercontent.com/jim60105/bgutil-ytdlp-pot-provider-rs/master/LICENSE" }
    )
    foreach ($n in $native) {
        $out = Join-Path $licDir ("{0}.txt" -f ($n.Name -replace "[^A-Za-z0-9._-]", "-"))
        if (Test-Path $out) { continue }
        try {
            Assert-TrustedUrl $n.Url
            Invoke-WebRequest -Uri $n.Url -OutFile $out -UseBasicParsing
            Say "  fetched $($n.Name)"
        } catch {
            Warn "  could not fetch the licence for $($n.Name): $_"
            Warn "    get it from $($n.Url)"
        }
    }

    $lines = @(
        "# Licence inventory",
        "",
        "Generated by ``setup.ps1 -Licenses`` on $(Get-Date -Format 'yyyy-MM-dd').",
        "Read out of each package's own ``.dist-info``, so the versions and the",
        "declared licences are the ones in this venv - not a hand-kept list.",
        "Re-run it whenever a dependency moves.",
        "",
        "| Package | Version | Declared licence | Text |",
        "|---|---|---|---|"
    )
    foreach ($r in $rows) {
        $f = if (-not $r.File) { "**none shipped**" }
             elseif ($r.ByHand) { "``$($r.File)`` (fetched by hand)" }
             else { "``$($r.File)``" }
        $lines += "| $($r.Name) | $($r.Version) | $($r.Declared) | $f |"
    }
    $lines += @("", "## Not from pip", "",
                "| Component | Why it is here | Licence source |", "|---|---|---|")
    foreach ($n in $native) {
        $lines += "| $($n.Name) | $($n.Why) | $($n.Url) |"
    }
    if ($missing.Count) {
        $lines += @("", "## Shipped no licence text", "",
                    "These declare a licence, carry no text in their wheel, and have",
                    "no text in ``licenses\`` either - so one has to be fetched from",
                    "the project before publishing a binary. A row above marked",
                    "``(fetched by hand)`` has already been dealt with and is not",
                    "listed here.",
                    "")
        foreach ($m in $missing) { $lines += "- $m" }
    }
    Set-Content -Path (Join-Path $licDir "INVENTORY.md") -Value $lines -Encoding UTF8

    Say "licenses\ now holds $($rows.Count) package licence(s) + INVENTORY.md"
    if ($byHand.Count) {
        Say "$($byHand.Count) text(s) came from licenses\ rather than a wheel - kept"
    }
    if ($missing.Count) {
        Warn "$($missing.Count) package(s) shipped no licence text - see INVENTORY.md"
    }
    Say "Check INVENTORY.md against THIRD-PARTY-NOTICES.md before publishing."
    exit 0
}

# ---------------------------------------------------------------- libmpv -----
$dllTarget = Join-Path $Root "mpv-2.dll"
if (Test-Path $dllTarget) {
    Say "mpv-2.dll already present - skipping download"
} else {
    $mpvPin = $PINNED.Mpv
    Say "Fetching libmpv build $($mpvPin.Tag)"
    $tmp = New-ScratchDir "mpv"
    $archive = Join-Path $tmp $mpvPin.Name
    $asset = $null
    try {
        Get-Verified "https://github.com/shinchiro/mpv-winbuild-cmake/releases/download/$($mpvPin.Tag)/$($mpvPin.Name)" `
                     $archive $mpvPin.Name
        $asset = $mpvPin
    } catch {
        $why = "$_"
        if ($why -like "checksum mismatch*") {
            # Not "gone" - the file is there and is not what was tested. That
            # is what tampering looks like, so it is not a reason to go and
            # fetch something else instead.
            Warn "Could not fetch $($mpvPin.Name): $why"
        } else {
            Warn "Could not fetch the tested libmpv build $($mpvPin.Tag): $why"
            Warn "  Its publisher keeps only the latest 30 builds, so a tested one is"
            Warn "  deleted after a few weeks. Trying the newest build instead."
            try {
                $latest = Get-LatestMpvAsset
                $archive = Join-Path $tmp $latest.Name
                Get-Verified $latest.Url $archive $latest.Name $latest.Sha256
                Warn "  Using libmpv $($latest.Tag) - not the build this release was tested with."
                $asset = $latest
            } catch {
                Warn "The newest build could not be used either: $_"
            }
        }
    }

    if (-not $asset) {
        Warn "Do this manually, it takes a minute:"
        Warn "  1. Download the newest 'mpv-dev-x86_64-<date>-git-<id>.7z' from"
        Warn "     https://github.com/shinchiro/mpv-winbuild-cmake/releases"
        Warn "     (not the -v3- or aarch64 one)"
        Warn "  2. Extract it, find 'libmpv-2.dll'"
        Warn "  3. Copy it next to this script and ALSO rename the copy to 'mpv-2.dll'"
    } else {

        $extract = Join-Path $tmp "x"
        Remove-Item -Recurse -Force $extract -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $extract | Out-Null

        $extracted = $false
        $refused = $false
        # A refusal from either extractor's check lands here, so it reads as
        # a refusal rather than "7zr download failed" - and deno and the token
        # generator below still get their turn.
        try {
            # Windows 10+ ships bsdtar, which can usually read 7z. Listed
            # before it extracts; an archive tar cannot list goes to 7zr.
            $listed = $null
            try {
                $listed = @(& tar.exe -tf $archive 2>$null)
                if ($LASTEXITCODE -ne 0) { $listed = $null }
            } catch { $listed = $null }
            if ($listed) {
                Assert-SafeEntries $listed $archive
                try {
                    & tar.exe -xf $archive -C $extract 2>$null
                    if ($LASTEXITCODE -eq 0) { $extracted = $true }
                } catch { }
            }

            if (-not $extracted) {
                Say "Falling back to the standalone 7-Zip extractor"
                $sevenZip = Join-Path $tmp "7zr.exe"
                try {
                    # Downloaded and executed below, so it comes from the
                    # release that publishes a digest anyone can check rather
                    # than from an unversioned URL. See the pin table.
                    Get-Verified "https://github.com/ip7z/7zip/releases/download/$($PINNED.SevenZip.Tag)/$($PINNED.SevenZip.Name)" `
                                 $sevenZip $PINNED.SevenZip.Name
                } catch {
                    Warn "7zr download failed: $_"
                    $sevenZip = $null
                }
                if ($sevenZip) {
                    Assert-SafeEntries (Get-7zEntryNames $sevenZip $archive) $archive
                    & $sevenZip x $archive "-o$extract" -y | Out-Null
                    if ($LASTEXITCODE -eq 0) { $extracted = $true }
                }
            }
            if ($extracted) { Assert-NoLinks $extract }
        } catch {
            Warn "Did not install libmpv: $_"
            $extracted = $false
            $refused = $true
        }

        if ($refused) {
            # Already said why, above.
        } elseif ($extracted) {
            # Only the exact filename. The old "*mpv*.dll" fallback let the
            # archive decide which native DLL got installed under the name the
            # app loads into its own process.
            $dll = Get-ChildItem -Path $extract -Recurse -Filter "libmpv-2.dll" | Select-Object -First 1
            if ($dll) {
                Copy-Item $dll.FullName $dllTarget -Force
                Copy-Item $dll.FullName (Join-Path $Root "libmpv-2.dll") -Force
                Say "Installed mpv-2.dll ($([math]::Round($dll.Length / 1MB, 1)) MB)"
            } else {
                Warn "Archive extracted but no mpv DLL inside - extract it by hand from $extract"
            }
        } else {
            Warn "Could not extract $archive - open it manually and copy libmpv-2.dll next to this script as mpv-2.dll"
        }
        Remove-Item -Force $archive -ErrorAction SilentlyContinue
    }
}

# ------------------------------------------------------------------ deno -----
# yt-dlp needs a JavaScript runtime to solve YouTube's signature challenges.
# Without one it silently falls back to player clients that now require a PO
# token, and every media fetch comes back 403. deno.exe is a single portable
# binary, so we drop it in this folder rather than installing anything.
$denoTarget = Join-Path $Root "deno.exe"
if (Test-Path $denoTarget) {
    Say "deno.exe already present - skipping download"
} elseif ((Get-Command deno -ErrorAction SilentlyContinue) -and -not $Portable) {
    Say "deno is already on your PATH - skipping download"
    Say "  (run 'setup.ps1 -Portable' to keep a local copy instead)"
} else {
    $denoPin = $PINNED.Deno
    Say "Fetching deno $($denoPin.Tag) (JavaScript runtime for yt-dlp)"
    try {
        $denoTmp = New-ScratchDir "deno"
        $denoZip = Join-Path $denoTmp "deno.zip"
        Get-Verified "https://github.com/denoland/deno/releases/download/$($denoPin.Tag)/$($denoPin.Name)" `
                     $denoZip $denoPin.Name
        Assert-SafeEntries (Get-ZipEntryNames $denoZip) $denoZip
        Expand-Archive -Path $denoZip -DestinationPath $denoTmp -Force
        Assert-NoLinks $denoTmp
        $denoExe = Get-ChildItem -Path $denoTmp -Recurse -Filter "deno.exe" | Select-Object -First 1
        if ($denoExe) {
            Copy-Item $denoExe.FullName $denoTarget -Force
            Say "Installed deno.exe ($($denoPin.Tag))"
        } else {
            Warn "deno.zip downloaded but contained no deno.exe"
        }
        Remove-Item -Recurse -Force $denoTmp -ErrorAction SilentlyContinue
    } catch {
        Warn "Could not install deno automatically: $_"
        Warn "Download $($denoPin.Name) from"
        Warn "  https://github.com/denoland/deno/releases/tag/$($denoPin.Tag)"
        Warn "and put deno.exe next to this script."
    }
}

# ------------------------------------------------- PO token provider ---------
# Without this, YouTube serves the opening few MB of a video and answers 403 to
# everything after it. Two pieces: the yt-dlp plugin and the token generator.
#
# The plugin comes from requirements.txt with the rest, pinned there, and from
# nowhere else. Until 0.9.9.25 this section then ran
# `pip install --upgrade bgutil-ytdlp-pot-provider`, which quietly replaced the
# pin with the newest release: the 0.9.9.24 build bundled 2.0.2 against a
# 2.0.0 pin, and fetched a generator source checksums.txt has no hash for.
# build.ps1 now refuses to build when the venv and the pin disagree.

# `yt-dlp-get-pot` is a legacy shim that older plugin versions depended on.
# Current yt-dlp has its own provider framework, and leaving the shim installed
# makes it intercept every token request and fail with "No request handlers
# configured" - hundreds of warnings that mean nothing.
# pip writes "Skipping ... not installed" to stderr, and with
# $ErrorActionPreference = "Stop" PowerShell turns any native stderr output into
# a terminating error - which silently aborted the rest of this script.
# A frozen bundle has no pip and no venv, and nothing ever installed the shim
# into it, so there is nothing to remove there.
if (-not $RuntimeOnly) {
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $uninstall = (& $VenvPy -m pip uninstall -y yt-dlp-get-pot 2>&1 | Out-String)
    $ErrorActionPreference = $prevEAP
    if ($uninstall -match "Successfully uninstalled") {
        Say "Removed the obsolete yt-dlp-get-pot shim"
    }
}

$potTarget = Join-Path $Root "bgutil-pot.exe"
$potPin = $PINNED.PotExe

# The standalone Rust generator is a SPARE. What actually serves tokens is the
# deno script from the same release as the plugin (below); this only comes into
# play when deno is unavailable.
#
# It is also a separate project on its own version line - it stopped tracking
# upstream at 0.8.1 - and the plugin refuses to talk across a major-version gap.
# So when the majors disagree there is nothing to gain by fetching 40-odd MB
# that cannot answer. Same rule the app applies at runtime: see
# potprovider.versions_agree.
$pluginVer = Get-PluginVersion
$plugMajor = if ($pluginVer) { ($pluginVer -split '\.')[0] } else { $null }
$potMajor  = ($potPin.Version -split '\.')[0]

if (Test-Path $potTarget) {
    Say "bgutil-pot.exe already present - skipping download"
} elseif ($plugMajor -and $plugMajor -ne $potMajor) {
    Say "Skipping the standalone generator - nothing is wrong"
    Say "  It is on $($potPin.Version), the plugin is on $pluginVer, and the plugin"
    Say "  refuses across a major-version gap, so it could not answer anyway."
    Say "  Tokens come from deno script mode instead. Raise PotExe in the pin"
    Say "  table once a build that matches the plugin exists."
} else {
    Say "Fetching the PO token generator $($potPin.Tag)"
    $potUrl = "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/download/$($potPin.Tag)/$($potPin.Name)"
    $ok = $false
    # Staged through a temp file rather than written straight to the name the
    # app executes on every launch. "Length -gt 200KB" was never an integrity
    # check - it only told you the download finished.
    $potTmp = Join-Path (New-ScratchDir "pot") "bgutil-pot.exe"
    try {
        Get-Verified $potUrl $potTmp $potPin.Name
        if ((Get-Item $potTmp).Length -gt 200KB) {
            Move-Item $potTmp $potTarget -Force
            $ok = Test-Path $potTarget
        } else {
            Warn "Downloaded file is too small to be the generator - discarding"
        }
    } catch {
        Warn "Download failed: $_"
    }
    if ($ok) {
        Say "Installed bgutil-pot.exe ($([math]::Round((Get-Item $potTarget).Length / 1MB, 1)) MB)"
    } else {
        Remove-Item -Force $potTarget -ErrorAction SilentlyContinue
        Warn "Could not install the standalone PO token generator."
        Warn "Download '$($potPin.Name)' from"
        Warn "  https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/tag/$($potPin.Tag)"
        Warn "and save it next to this script as 'bgutil-pot.exe'."
        Warn "It is only a spare - deno script mode is the primary, so this bites"
        Warn "only if deno is unavailable too."
    }
}

# ------------------------------------- token generator, script mode ----------
# The plugin ships its own generator source. Fetching the copy that matches the
# installed plugin version removes the whole class of protocol mismatch, and
# deno runs the TypeScript directly - no Node.js needed.
# $pluginVer was read above, next to the standalone-generator check.
if ($pluginVer) { $pluginVer = $pluginVer.Trim() }
$scriptRoot = Join-Path $Root "bgutil-server"
$scriptSrc = Join-Path $scriptRoot "bgutil-ytdlp-pot-provider-$pluginVer\server\src"

# 2.0.0 merged the one-shot generator into the server's own main.ts; up to
# 1.3.1 it was a separate generate_once.ts. Accept either, so re-running this
# against an older tree does not decide it is broken and re-download it.
function Get-Generator([string]$srcDir) {
    foreach ($name in @("main.ts", "generate_once.ts")) {
        $candidate = Join-Path $srcDir $name
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

if ($pluginVer -and -not (Get-Generator $scriptSrc)) {
    Say "Fetching the token generator source matching plugin $pluginVer"
    try {
        Remove-Item -Recurse -Force $scriptRoot -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $scriptRoot | Out-Null
        $srcZip = Join-Path (New-ScratchDir "gen") "bgutil-$pluginVer.zip"
        Get-Verified "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/$pluginVer.zip" `
            $srcZip "bgutil-ytdlp-pot-provider-$pluginVer.zip"
        Assert-SafeEntries (Get-ZipEntryNames $srcZip) $srcZip
        Expand-Archive -Path $srcZip -DestinationPath $scriptRoot -Force
        Assert-NoLinks $scriptRoot
        Remove-Item -Force $srcZip -ErrorAction SilentlyContinue
        if (Get-Generator $scriptSrc) {
            Say "Token generator source installed"
        } else {
            Warn "Source downloaded but no main.ts or generate_once.ts was"
            Warn "  found in $scriptSrc - the layout may have changed again."
        }
    } catch {
        Warn "Could not fetch the generator source: $_"
        Warn "Without it only 360p is available. See the README."
    }
} elseif ($pluginVer) {
    Say "Token generator source for $pluginVer already present"
}

# deno fetches the npm packages on demand but will not materialise node_modules
# by itself - without this step every token request dies with
# "Could not resolve \"commander\" ... Did you forget to run `deno install`?"
$serverDir = Join-Path $scriptRoot "bgutil-ytdlp-pot-provider-$pluginVer\server"
if ((Get-Generator (Join-Path $serverDir "src")) -and -not (Test-Path (Join-Path $serverDir "node_modules"))) {
    Say "Installing the generator's dependencies with deno (one-time, a few minutes)"
    $denoCmd = if (Test-Path (Join-Path $Root "deno.exe")) { Join-Path $Root "deno.exe" } else { "deno" }
    $prevEAP2 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    Push-Location $serverDir
    # No --allow-scripts. That authorises npm lifecycle/postinstall scripts for
    # every package in the resolved tree - and there is no lockfile, so the tree
    # is re-resolved on each machine. One compromised transitive dependency would
    # then run as the user, at setup time, on every install. The packages that
    # want scripts here are dev-only; the token generator works without them.
    $denoOut = (& $denoCmd install 2>&1 | Out-String)
    $denoExit = $LASTEXITCODE
    # What deno says when the drive cannot make the NTFS junctions node_modules
    # is built from: "failed to create junction ... Incorrect function. (os
    # error 1)". exFAT and FAT32 have no reparse points, and some network and
    # sync drives refuse them too - so this is keyed on the refusal, not on the
    # filesystem's name. Retrying on the same drive only spends another minute
    # failing the same way.
    $linkFailure = $denoOut -match "junction|symlink|reparse|Incorrect function"
    if ($denoExit -ne 0 -and -not $linkFailure) {
        # A second attempt, in case the first was a transient registry error.
        # Deliberately NOT a retry with --allow-scripts: if the tree genuinely
        # cannot install without running postinstall scripts, that is a decision
        # for a human, not a silent fallback.
        $denoOut = (& $denoCmd install 2>&1 | Out-String)
        $denoExit = $LASTEXITCODE
        $linkFailure = $denoOut -match "junction|symlink|reparse|Incorrect function"
    }
    Pop-Location
    $ErrorActionPreference = $prevEAP2
    # The exit code, not the folder. A deno install that dies half way has
    # already created node_modules - which is how this reported "installed" on
    # a tester's drive where nothing could resolve.
    if ($denoExit -eq 0) {
        Say "Generator dependencies installed"
    } elseif ($linkFailure) {
        Warn "This drive refused to create the junctions deno builds the token"
        Warn "generator's dependencies from (`"Incorrect function`") - exFAT and"
        Warn "FAT32 drives cannot make them. Nothing to fix here: the app builds"
        Warn "them under %LOCALAPPDATA%\ChuuTube2Spout on its first launch"
        Warn "instead, which takes about a minute."
    } else {
        Warn "deno install failed:"
        Warn ($denoOut -split "`n" | Select-Object -Last 5)
    }
}

# ------------------------------------------ plugin / server sanity -----------
# Do NOT pin the plugin backwards to match an older server: the 0.8.x plugin
# fails to import against current yt-dlp ("cannot import name select_proxy"),
# which silently removes the provider altogether. Keep the plugin current and
# report a mismatch instead.
$scriptModeReady = ($null -ne (Get-Generator (Join-Path $serverDir "src"))) -and `
                   (Test-Path (Join-Path $serverDir "node_modules"))
if ($scriptModeReady) {
    # Script mode is what actually issues tokens when it is present: the plugin
    # and the generator come from the same release. The standalone .exe is then
    # a spare nothing calls, and warning about its version reads as a fault when
    # the app is working perfectly.
    Say "Token generator: script mode (plugin and generator are the same release)"
} elseif (Test-Path $potTarget) {
    $potVersion = ""
    try {
        $raw = (& $potTarget --version 2>&1 | Out-String)
        if ($raw -match "(\d+\.\d+\.\d+)") { $potVersion = $Matches[1] }
    } catch { }
    $pluginVersion = Get-PluginVersion
    if ($potVersion -and $pluginVersion) {
        $a = $potVersion.Split(".")[0]; $b = $pluginVersion.Trim().Split(".")[0]
        if ($a -ne $b) {
            Warn "Token plugin $($pluginVersion.Trim()) and generator $potVersion are on different major versions."
            Warn "The plugin will decline to use it, so no PO token is issued."
            Warn "ChuuTube2Spout will skip the generator rather than spam warnings."
        } else {
            Say "Token plugin $($pluginVersion.Trim()) matches generator $potVersion"
        }
    }
}

# --------------------------------------------------------- stale cache -------
$cache = Join-Path $Root "cache"
$parts = @(Get-ChildItem -Path $cache -Filter "*.part" -ErrorAction SilentlyContinue)
if ($parts.Count -gt 0) {
    Say "Removing $($parts.Count) partial download(s) left by earlier attempts"
    $parts | Remove-Item -Force -ErrorAction SilentlyContinue
}

# -------------------------------------------------------------- settings -----
$settings = Join-Path $Root "settings.txt"
if (-not (Test-Path $settings)) {
    Copy-Item (Join-Path $Root "settings.txt.example") $settings -ErrorAction SilentlyContinue
    if (Test-Path $settings) { Say "Created settings.txt - edit it to change options" }
} else {
    Say "settings.txt is in place - edit it to change options"
}

# --------------------------------------------------------------- verdict -----
# Everything above warns and carries on, so that one failed download does not
# cost the others. Which means the script has to say at the end whether what
# it left behind can actually start. mpv-2.dll is the one piece the app cannot
# open without; deno and the token generator missing cost quality, not the
# window, and the app reports them itself.
#
# Until 0.9.9.18 this printed "Runtime installed" either way. The first-run
# window read exit code 0 as success and closed itself - taking the warning
# that said why libmpv was missing with it - and the user met a dead-end
# "missing dependency" dialog instead.
if (-not (Test-Path $dllTarget)) {
    Write-Host ""
    Die "mpv-2.dll is not installed, so the app cannot start. The warnings above say why - run this again to retry."
}

# -------------------------------------------------------------- diagnose -----
if ($RuntimeOnly) {
    Write-Host ""
    Say "Runtime installed. Start the app with ChuuTube2Spout.exe"
    Say "If anything looks wrong, run:  ChuuTube2Spout.exe --doctor"
} else {
    Say "Running diagnostics"
    & $VenvPy (Join-Path $Root "run.py") --doctor
    Write-Host ""
    Say "Done. Start the app with run.bat"
    Say "If a video refuses to play, run:  check.bat <youtube-url>"
}
