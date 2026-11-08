# ChuuTube2Spout

ChuuTube2Spout plays YouTube videos and sends them to **Spout**, so that
Resolume Arena and other Spout-capable VJ software can use them as live
sources. Inspired by *yt2spout*.

- Two slots, A and B, play at the same time. Each has its own Spout sender.
- Clips are downloaded before they play, so they never buffer mid-set.
- It runs on Windows. It is free software under the GPL (see [Licence](#licence)).

Disclaimer: Contains AI generated code. Not affiliated with YouTube or Google.

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Using the app](#using-the-app)
- [Settings](#settings)
- [Signing in to YouTube](#signing-in-to-youtube)
- [Troubleshooting](#troubleshooting)
- [Known bugs](#known-bugs)
- [Reporting a problem](#reporting-a-problem)
- [Privacy and security](#privacy-and-security)
- [Running from source](#running-from-source)
- [How it works](#how-it-works)
- [Licence](#licence)
- [Changelog](#changelog)

## Requirements

- Windows 10 or 11, 64-bit
- A graphics card with OpenGL 3.3
- Software that receives Spout, for example Resolume Arena or Avenue
- About 500 MB of disk space for the app, plus space for clips
  (a 3-minute 1080p clip is about 80 MB)
- An internet connection: once for the first-run install, then to download clips

## Install

1. Download `ChuuTube2Spout-<version>-win64.zip` from the Releases page.
2. Unzip it into a folder you can write to, for example `D:\VJ\ChuuTube2Spout`.
   Do not use `Program Files`: the app keeps its downloads and logs in its own
   folder.
3. Start `ChuuTube2Spout.exe`.
   Windows SmartScreen may warn that the app is unrecognised, because it is
   not code-signed. Select **More info**, then **Run anyway**.
4. A one-time setup window opens. Select **Download and install** and a few minutes.
5. Optional, but very much recommended unless you enjoy 240p footage: [Signing in to YouTube](#signing-in-to-youtube)

**Result:** the app opens. From then on it only goes online to download clips.

**Why there is a setup step:** three parts of the runtime – libmpv (video
playback), deno (a JavaScript runtime) and the YouTube token generator – are
other projects' work under their own licences. The zip does not include them.
Setup downloads each one from the project that publishes it and checks it
against `checksums.txt`. Details: [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

**If setup fails:** the window says **That did not work** and shows the
reason. Select **Try again**, or close the app and run `setup-runtime.bat` in
the same folder to see the full output.

**To move the app:** copy the whole folder. Everything it needs is inside.

**To update:** unzip the new release into a new folder. Copy your
`settings.txt` from the old folder into it, then start the new
`ChuuTube2Spout.exe`. Unzipping over the old folder replaces your
`settings.txt` with the new, empty one.

**To uninstall:**

1. Delete the app folder.
2. Optional, to remove the rest:
   - the registry key `HKEY_CURRENT_USER\Software\ChuuTube2Spout` (window
     size and position, selected slot),
   - the folder `%LOCALAPPDATA%\ChuuTube2Spout`, if it exists. It is used only
     when the app's own drive cannot hold the token generator.

deno and yt-dlp also keep caches in `%LOCALAPPDATA%\deno` and
`%USERPROFILE%\.cache\yt-dlp`. Other programs may use these too, so delete
them only if nothing else needs them. The app does not change `PATH` or
autostart, and needs no administrator rights.

## Quick start

1. In Resolume, add the Spout sources **ChuuTube2Spout-A** and
   **ChuuTube2Spout-B**. They are available as soon as the app is open, and
   are transparent until something plays.
2. Copy a YouTube link in your browser.
3. In slot A, select **Paste**. The download starts in the background: a
   green line runs along the bottom of the URL field.
4. Select **Load**.

**Result:** the clip plays in slot A and on `ChuuTube2Spout-A`. Load slot B the
same way, then switch or crossfade between the two sources in Resolume.

## Using the app

### Slot controls

| Control | What it does |
|---|---|
| URL field | Holds the link to play. A typed link starts downloading after a short pause. |
| **Paste** | Puts the clipboard into the field and starts downloading at once. |
| **Load** | Plays the link in the field. |
| **Clear** | Empties the field, stops the slot and makes its output transparent. |
| Seek bar | Shows the position. Drag it to jump. |
| ⏮ | Back to the start. |
| ▶ / ⏸ | Play or pause. |
| Quality list | Highest resolution to download. See [Quality](#quality). |
| Status line | What the slot is doing, or what it is playing. |

The bar at the bottom of the window shows the sender names, the output size,
the frame rate, the **Clear cache on exit** option and the **?** button.

### Loading a clip

- **Background download.** As soon as a link is in the field, the app starts
  downloading it. A green line along the bottom of the field shows progress.
  When the clip is ready, the **Load** button fills green and loading is
  instant.
- **Loading.** Select **Load** at any time. If the download is still running,
  the button shows `Loading 42%`.
- **Playing early.** A long download starts playing before it has finished.
  The status line then says `playing while it downloads`.

This lets you prepare the next clip while the current one is still playing.

### Spout output

- Each slot has one sender: `ChuuTube2Spout-A` and `ChuuTube2Spout-B`.
- Both senders always send 1920 × 1080 pixels. A clip of another shape, for
  example 4:3, is scaled to fit and keeps its shape. The bars beside or above
  it are transparent, so Resolume shows the layers beneath them. The preview
  shows transparent areas as a dark checkerboard.
- Keep this fixed size in Resolume. Resolume fits a clip to its layer only
  once. If the size changes with each clip, a 16:9 clip that follows a 4:3
  clip is squashed.
- To change the size or the colour of the bars, see "Output size" at the top
  of `settings.txt`. Set the size to your Resolume composition.
- To send each clip at its own size instead, put a `#` in front of the three
  output-size lines in `settings.txt`. `SENDER_MAX_WIDTH` and
  `SENDER_MAX_HEIGHT` then limit the size; larger clips are scaled down and
  keep their shape.
- An empty slot sends a transparent picture, so Resolume shows the layers
  beneath it. The preview shows a transparent slot as a dark checkerboard.
  `PREVIEW_GRID=0` shows it black instead. `EMPTY_TRANSPARENT=0` sends black,
  as versions before 0.9.9.27 did.

### Quality

The quality list sets an upper limit. The app downloads the best version at or
below that limit. If a video has nothing that small, it takes the next size up.
The status line shows what you actually got. Changing the quality reloads the
clip at the same position.

### Audio

Audio is **off** by default, because the sound in a VJ set usually comes from
somewhere else. To turn it on, add `FETCH_AUDIO=1` to `settings.txt`. Each slot
then has a speaker button. Only one slot can be heard at a time: turning one on
turns the other off.

### Keyboard

The keys act on the selected slot, which has a green border. Select a slot with
`1` / `2`, or by using any control in it.

| Key | Action |
|---|---|
| `1` / `2` | Select slot A / B |
| `Space` | Play / pause |
| `←` / `→` | Seek 5 s |
| `Shift`+`←` / `→` | Seek 60 s |
| `Home` | Back to the start |
| `M` | Audio on / off (only with `FETCH_AUDIO=1`) |
| `Ctrl`+`1` / `Ctrl`+`2` | Go to the URL field of slot A / B |
| `F` | Resize the window so that both previews are exactly 16:9 |

### Downloaded clips

Clips are stored in the `cache` folder next to the app. Loading the same clip
again is instant.

- Clips older than 24 hours are deleted when the app starts and after each
  download.
- If the folder grows beyond 10 GB, the clips used longest ago are deleted
  first. A clip that is playing, or was downloaded in the last 30 minutes, is
  never deleted.
- **Clear cache on exit** empties the folder when you close the app.
- `clear-cache.bat` empties it at any time. Deleting the folder by hand is
  also safe.

## Settings

All settings are in `settings.txt` in the app folder. It lists every setting
with an explanation. A line that starts with `#` is off. Three lines are on:
the output size at the top (1920 × 1080, transparent bars, see
[Spout output](#spout-output)). Every other setting is at its default.

To change a setting:

1. Close the app.
2. Open `settings.txt` in a text editor.
3. Find the line, and remove the `#` at its start.
4. Change the value, save the file and start the app.

The most useful ones:

| Setting | Default | What it does |
|---|---|---|
| `COOKIES_FROM_BROWSER` | – | Signs in to YouTube with a browser's login, e.g. `waterfox`, `firefox`, `chrome` |
| `FETCH_AUDIO` | `0` | `1` downloads audio and shows the speaker buttons |
| `SENDER_WIDTH`, `SENDER_HEIGHT` | `1920`, `1080` (on in `settings.txt`) | Output size of both senders. Without them, each clip is sent at its own size |
| `SENDER_BACKGROUND` | `transparent` (on in `settings.txt`) | Colour of the bars: `transparent`, `black` or `#RRGGBB` |
| `SENDER_FIT` | `fit` | A clip of a different shape: `fit` (bars), `fill` (crop), `stretch` (distort) |
| `SENDER_MAX_WIDTH`, `SENDER_MAX_HEIGHT` | – | Without a fixed size: largest output size; larger clips are scaled down |
| `EMPTY_TRANSPARENT` | `1` | `0` makes an empty slot send black instead of transparency |
| `PREVIEW_GRID` | `1` | `0` shows an empty slot black in the preview instead of a checkerboard |
| `LOOP` | `1` | `0` stops at the end instead of looping |
| `HWDEC` | `auto-safe` | `no` turns off hardware video decoding |
| `WINDOW_WIDTH` | – | Window width at every start, in pixels. `1320` is the default size |
| `WINDOW_SCALE` | `1` | Window size at start: `max` fills the screen, `1.5` or `2` enlarges |
| `CACHE` | `cache` | Folder for downloaded clips |
| `CACHE_MAX_AGE_H` | `24` | Hours a clip is kept. `0` turns this off |
| `CACHE_MAX_GB` | `10` | Size limit of the clip folder, in GB |
| `CLEAR_CACHE_ON_EXIT` | `0` | Starting state of the **Clear cache on exit** option |
| `LOG_KEEP` | `10` | How many log files to keep |

Each setting can also be given as an environment variable with the prefix
`CHUUTUBE2SPOUT_`, for example `CHUUTUBE2SPOUT_HWDEC=no`. An environment
variable overrides `settings.txt`.

## Signing in to YouTube

Signing in is technically optional, but it avoids most "confirm you're not a bot" and
age-check refusals. The app uses the login of a browser on the same computer:

1. Sign in to YouTube in your browser.
2. Add one line to `settings.txt`, with the name of your browser:
   ```
   COOKIES_FROM_BROWSER=firefox
   ```
3. Start the app.

| Browser | Can stay open |
|---|---|
| Firefox, Waterfox, LibreWolf, Floorp, Zen | Yes |
| Chrome, Edge, Brave, Vivaldi, Opera | No – close it before starting the app |

For a specific Firefox profile, give its path: `COOKIES_FROM_BROWSER=firefox:C:\path\to\profile`.

The app reads only the YouTube cookies, keeps them in a private temporary file,
and deletes that file when it closes. Details: [SECURITY.md](SECURITY.md).

## Troubleshooting

Two tools in the app folder help to find the cause:

- `doctor.bat` checks the runtime, the Spout library, the graphics driver and
  the token generator, and names what is missing.
- `check.bat "<link>"` tests why one link does not play. Put the link in
  quotes.

| Problem | What to do |
|---|---|
| A message says the video engine is missing | Run `setup-runtime.bat` and read the last lines it prints. |
| "Sign in to confirm you're not a bot", or the slot says "YouTube asks for a sign-in" | Sign in: see [Signing in to YouTube](#signing-in-to-youtube). |
| A link does not load, or stops after a few seconds (HTTP 403) | Run `check.bat "<link>"`. Turn off a VPN if you use one. Sign in. |
| Only 360p is available | The token generator is not working. Run `doctor.bat`. |
| A link that used to work no longer loads | YouTube has changed something. A newer release usually fixes it; `update-ytdlp.bat` shows the version in use. |
| Black picture, or a crash when loading | Set `HWDEC=no`. |
| The preview is upside down | Set `PREVIEW_FLIP=0`. |
| The picture in Resolume is upside down | Set `SPOUT_INVERT=1`. |
| Resolume does not list the senders | Check that the app is running and Resolume's Spout source list is refreshed. |
| The preview stutters, or the buttons react slowly | Set `SENDER_MAX_WIDTH` / `SENDER_MAX_HEIGHT` smaller. To measure, set `PERF_LOG_S=1` and read the log. |
| Moving the window leaves a smeared trail | Set `DRAG_SETTLE_MS=220`. |

## Known bugs

These problems are known and not fixed yet.

| Problem | What to do |
|---|---|
| Clicking or holding the window's title bar stops the picture for about half a second, in the preview and in Resolume. Windows waits that long to tell a click from a double-click, and the app cannot draw during the wait. | Place the window before the show. Do not click its title bar during a set. Moving the window by dragging works once the half second has passed. |
| Without signing in, YouTube sometimes refuses a video (HTTP 403). | Press **Load** again. Signing in helps; see [Signing in to YouTube](#signing-in-to-youtube). |
| `MODE=hybrid` does not work for YouTube. | The app falls back to a normal download. Leave `MODE` at its default. |
| The play and jump-to-start symbols can look like coloured emoji, depending on the Windows version. | No effect on the buttons. New symbols are planned for after version 1.0. |
| Rarely, the log ends with `Windows fatal exception: access violation` after the app is closed. | No effect on the show. If it happens while the app is running, report it with the log. |

## Reporting a problem

Every run writes a log file to the `logs` folder in the app folder. The **?**
button shows the name of the current file. The last 10 logs are kept.

Login data is removed from the log automatically: IP addresses, tokens and
signed links are replaced by `<redacted>`. File paths stay in, and they
contain your Windows user name.

Open an issue on GitHub and attach:

- the log file of the run with the problem,
- the output of `doctor.bat`,
- your Windows version and graphics card.

Report security problems privately: see [SECURITY.md](SECURITY.md).

## Privacy and security

- The app connects only to YouTube and Google (videos and tokens), GitHub
  (setup downloads, and the challenge solver yt-dlp needs) and `127.0.0.1`
  (its own token generator).
- It sends no usage data and does not check for updates.
- The `cache` folder is a record of the clips you played.

[SECURITY.md](SECURITY.md) explains what the app stores, what setup downloads,
and how each download is checked.

## Running from source

For development, or to run the app without the release zip.

1. Install Python 3.12 from python.org.
2. In the project folder, run:
   ```
   powershell -ExecutionPolicy Bypass -File setup.ps1
   ```
   This creates a virtual environment in `.venv`, installs the Python
   packages, downloads the runtime and creates `settings.txt` from
   `settings.txt.example`.
3. Start the app with `run.bat`.

| Task | Command |
|---|---|
| Run the tests | `.venv\Scripts\python.exe -m unittest discover -s tests` |
| Check the setup | `doctor.bat` |
| Build the release zip | `powershell -ExecutionPolicy Bypass -File build.ps1` |

`build.ps1` runs the tests first and stops if one fails. Before publishing a
build, read [RELEASING.md](RELEASING.md).

## How it works

| Part | Job |
|---|---|
| yt-dlp | Finds and downloads the clip |
| Token generator (bgutil, run by deno) | Creates the token YouTube needs for anything above 360p |
| libmpv | Decodes and plays the clip on the graphics card |
| Spout2 | Shares each picture with Resolume as a GPU texture |
| PySide6 (Qt) | Window, controls and preview |

The picture stays on the graphics card from decoding to Spout. It is never
copied to main memory.

Clips are downloaded, not streamed, because YouTube cuts off video players that
read its links directly. A downloaded clip also seeks instantly,
loops cleanly and keeps playing if the network drops.

## Licence

GNU General Public License v3.0 or later. The full text is in
[LICENSE](LICENSE). Copyright © 2026 Djinn.

You may use, change and share the app. If you share a changed version, you
must share its source under the same licence. There is no warranty.

The components in the release zip, and their licences, are listed in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## Changelog

Changes in each release from version 1.0 on, newest first.
