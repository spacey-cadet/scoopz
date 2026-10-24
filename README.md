# Scoopz

> Share a TikTok link → download → AI caption, all in one Android app.

Scoopz started as a Termux workflow (two Python scripts plus a share-sheet trick). It is moving to a native Android app so the whole pipeline — link sharing, downloading, captioning — happens in one place with proper memory management and no terminal. The original Termux workflow is kept below and still works.

| Track | Status | Where |
|-------|--------|-------|
| **Android app (Kotlin, Jetpack Compose)** | Scaffolded, builds not yet verified | [`android/`](android/) |
| **Flutter app** | Planned, not started | — |
| Termux scripts + FastAPI scaffold | Working (legacy) | repo root, `app/` |

---

## Android app

### What it does

1. **Share** a TikTok link to *Scoopz* from any app's share sheet (or tap **+** to paste from the clipboard).
2. The link is stored in a local Room database queue — no inbox file, no polling.
3. A background **download worker** (WorkManager + foreground notification) resolves the link via tikwm and streams the mp4 into `Movies/Scoopz` through MediaStore. Up to 3 downloads run in parallel; duplicates are skipped by video id.
4. A **caption worker** picks the 1-2 most informative frames on-device (sharpness, faces and on-screen text, using ML Kit) and sends them, with the TikTok title, uploader and any on-screen text, to NVIDIA's free OpenAI-compatible endpoint. Up to 3 captions run at once, and a backup model takes over if the main one is slow. It applies the same prompt, POV/style rotation, caption cleanup and hashtag recipe as `newsbreak_captions.py`.
5. The **library screen** shows every video with status and thumbnail. Tap cards to select up to 3, then **Caption** or **Copy**. Failed items can be retried.

### Memory management

- Videos stream to disk in 64 KB chunks and are never held in RAM.
- Frame extraction decodes one frame at a time and recycles every bitmap immediately.
- Thumbnail memory cache is capped at 12% of app memory; the disk cache at 64 MB.
- Download and caption concurrency are bounded by semaphores, and API calls are spaced by a rate limiter.

### Captioning with NVIDIA free endpoints

1. Create a free key at <https://build.nvidia.com> (Settings → API Keys).
2. In the app: **Settings → NVIDIA API key**.
3. Default model: `meta/llama-3.2-11b-vision-instruct`, base URL `https://integrate.api.nvidia.com/v1`.

Notes:
- The free tier is roughly 40 requests/minute per key across all models. The app defaults to 30 (configurable, 1–40).
- Free vision models take one image per request, so the picked frames are stitched side by side (kept under ~140 KB).
- Requests time out after about 45 s and are retried on the **backup model** (Settings). Names in captions come from the TikTok title, hashtags and on-screen text. The app never identifies people from faces.
- If you get HTTP 403, open the model's page on build.nvidia.com and use *Try API* once to enable it for your key.
- Audio transcription is not implemented (no free speech-to-text model found); captions use frames only.

### Developer setup

**Requirements**

- Android Studio (current stable) with JDK 17
- Android SDK Platform 35 (compile/target); device or emulator with Android 10+ (API 29+)
- Internet access for the first Gradle sync

**Run it**

```bash
git clone https://github.com/spacey-cadet/scoopz
cd scoopz
git checkout android-app        # until merged into main
```

1. Open the `android/` folder in Android Studio and let Gradle sync. If prompted for a Gradle wrapper, accept it (or run `gradle wrapper --gradle-version 8.9` inside `android/`).
2. Select a device and press **Run**.

**Command line**

```bash
cd android
gradle :app:assembleDebug        # APK: app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

**CI:** `.github/workflows/android.yml` builds a debug APK, but it is currently manual-only (`workflow_dispatch`) while GitHub Actions billing is sorted out. Re-add a `push:` trigger once it is.

**First run checklist**

1. Open the app, allow notifications, go to **Settings** and paste your NVIDIA key.
2. In TikTok: **Share → Scoopz**. The video appears in the library, downloads, then gets a caption.
3. Tap **Copy** on a captioned card and paste it where you post.

### Project layout

```text
android/
  app/src/main/java/com/scoopz/app/
    ScoopzApp.kt              # Application + service locator (DB, HTTP, settings, captioner)
    MainActivity.kt           # Compose host: library <-> settings
    ShareReceiverActivity.kt  # Invisible share-sheet target: parse, queue, close
    data/                     # Room entities/DAO, encrypted Settings
    net/TikwmClient.kt        # Link -> direct mp4 URL
    work/                     # DownloadWorker, CaptionWorker, Enqueue helpers
    media/FramePicker.kt      # Picks the best 1-2 frames (ML Kit faces + text)
    caption/                  # NVIDIA client, prompt, POV/style rotation, hashtag rules, rate limiter
    ui/                       # Library + Settings screens, ViewModel
    util/LinkParser.kt        # Extract TikTok links from shared text
```

Key dependencies: Compose Material 3, Room, WorkManager, OkHttp, Coil (+ video frame decoder), AndroidX Security Crypto (encrypted API key storage).

### Mapping from the Python scripts

| Python | Android |
|--------|---------|
| `collector.py` inbox polling | Share-sheet intent + Room queue (`ShareReceiverActivity`, `Enqueue`) |
| tikwm download with retries | `TikwmClient` + `DownloadWorker` |
| ffmpeg frame extraction | `MediaMetadataRetriever` in `FramePicker` |
| `CAPTION_PROMPT`, POV rotation, `normalize_hashtags` | `CaptionPrompt`, `CaptionStyle`, `Hashtags` |
| OpenAI gpt-4o | NVIDIA endpoint via `NvidiaCaptioner` |
| Local thumbnail picker page | Compose library screen with selection |
| `termux-notification` | Android notifications (WorkManager foreground service) |

### Known gaps

- The Android project has not been compiled yet; expect a few first-build fixes.
- No audio transcription.
- No unit tests yet (good first targets: `LinkParser`, `Hashtags`, `CaptionStyle`).

---

## Flutter (planned)

A Flutter version is wanted for cross-platform reach. The Kotlin app is the reference implementation. A Flutter port would reuse the same architecture, with platform channels (or plugins) for the Android-specific parts: share-sheet receiving, MediaStore saving, frame extraction and background downloads. Nothing has been started; track it in the repo issues.

---

## Termux workflow (original)

Scoopz is a two-script Termux workflow for Android that watches a link inbox, downloads TikTok videos, and generates ready-to-post captions using OpenAI's vision API.

- **`collector.py`** — watches a link inbox file and downloads TikTok videos to `/sdcard/Pictures/scoopz`
- **`newsbreak_captions.py`** — opens a local thumbnail picker, lets you select up to 3 videos, and generates captions in parallel
- **`scoopz_env.sh`** — adds shortcut commands (`collect`, `caption`, `scoopz-help`) to your shell
- **`app/`** — FastAPI scaffold for exposing the collector, video library, and caption workflow over HTTP

---

### FastAPI Scaffold

The API scaffold is intentionally thin: it defines the router layout, dependency injection points, Pydantic models, and lightweight services that can be wired into the existing scripts as the backend matures.

```bash
pip install -r requirements-api.txt
```

Those pins are chosen to stay friendly to Termux on Android; if you hit a build
error, make sure you are using the repo's `requirements-api.txt` exactly as
checked in and not `uvicorn[standard]`.

Default API prefix: `http://127.0.0.1:8000/api/v1`

When `scoopz_env.sh` is sourced, the API runs from Termux with the same project and media paths as the existing scripts. The normal workflow commands manage the API automatically:

```bash
collect      # starts the API, runs collector.py, stops its managed API on exit
caption      # starts the API, runs newsbreak_captions.py, stops its managed API on exit
caption-bg   # starts caption generator and API in the background
caption-stop # stops the background caption generator and its managed API
```

Manual API commands are still available for debugging or standalone use:

```bash
scoopz-api        # foreground server
scoopz-api-bg     # background server, logs to logs/api.log
scoopz-api-stop   # stop background server
scoopz-api-logs   # follow API logs
scoopz-api-open   # open API docs when termux-open-url is available
```

Override the bind address when needed:

```bash
export SCOOPZ_API_HOST=0.0.0.0
export SCOOPZ_API_PORT=8000
scoopz-api-bg
```

```text
app/
  main.py                 # FastAPI app factory
  api/
    deps.py               # Dependency providers
    router.py             # v1 router composition
    routes/               # Health, collector, videos, captions routes
  core/
    config.py             # Environment-driven settings
  models/                 # Pydantic request/response models
  services/               # Thin service layer behind route handlers
```

Initial endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/health` | Health check |
| `GET` | `/api/v1/collector/state` | Inbox/history counts |
| `POST` | `/api/v1/collector/links` | Append links to the collector inbox |
| `GET` | `/api/v1/videos` | List downloaded `.mp4` files |
| `GET` | `/api/v1/videos/{video_id}` | Read one video record |
| `POST` | `/api/v1/captions/jobs` | Queue a placeholder caption job |
| `GET` | `/api/v1/captions/jobs/{job_id}` | Read a caption job |
| `GET` | `/api/v1/captions/jobs/{job_id}/status` | Read caption job status |

---

### Prerequisites

#### Termux Packages

```bash
pkg update && pkg upgrade -y
pkg install -y python ffmpeg termux-api
pip install --upgrade pip
pip install requests
```

#### Storage Permission

```bash
termux-setup-storage
```

---

### Installation

#### 1. Clone the repo

```bash
git clone https://github.com/spacey-cadet/scoopz ~/scoopz
cd ~/scoopz
```

#### 2. Install shell commands

```bash
chmod +x scoopz_env.sh
echo 'source ~/scoopz/scoopz_env.sh' >> ~/.bashrc
source ~/.bashrc
```

#### 3. Verify

```bash
scoopz-help
```

You should see: `Scoopz ready` and the list of workflow commands.

---

### Required Folders & Files

```bash
mkdir -p /sdcard/Pictures/scoopz
mkdir -p /sdcard/Download
mkdir -p ~/.config
: > /sdcard/Download/links_inbox.txt
: > /sdcard/Download/scoopz_history.log
```

Add your OpenAI API key:

```bash
echo "YOUR_OPENAI_API_KEY" > ~/.config/openai_key.txt
chmod 600 ~/.config/openai_key.txt
```

| Path | Purpose |
|------|---------|
| `/sdcard/Pictures/scoopz` | Video output |
| `/sdcard/Pictures/scoopz/.thumbs` | Thumbnail cache |
| `/sdcard/Download/scoopz_temp` | Temp extraction |
| `/sdcard/Download/links_inbox.txt` | URL inbox |
| `/sdcard/Download/scoopz_history.log` | Download history |
| `~/.config/openai_key.txt` | OpenAI API key |

---

### Daily Workflow

#### Terminal 1 — Collector

```bash
python collector.py
```

- Polls `/sdcard/Download/links_inbox.txt`
- Downloads valid TikTok URLs to `/sdcard/Pictures/scoopz`
- Skips duplicates using `scoopz_history.log`

#### Terminal 2 — Captions

```bash
python newsbreak_captions.py
```

- Scans `/sdcard/Pictures/scoopz` for mp4 files and generates thumbnails
- Starts local picker at `http://127.0.0.1:8765`
- Select 1–3 videos and tap **GO**
- `ffmpeg` extracts 4 frames per video; captions generated in parallel via OpenAI
- Results appear in-page with per-caption **Copy** buttons

---

### Configuration

Defaults in `newsbreak_captions.py`:

| Setting | Default |
|---------|---------|
| `MAX_BATCH_SIZE` | `3` |
| `NUM_FRAMES` | `4` |
| `EXTRACTION_WORKERS` | `3` |
| `API_WORKERS` | `3` |
| `PORT` | `8765` |
| `CLIPBOARD_AUTO_CYCLE` | `False` |
| `CLIPBOARD_CYCLE_DELAY_SEC` | `3.0` |

#### Port Handling

The server tries to bind on port `8765` normally. If the port is in use, it attempts to free the listener process (`lsof`/`ss`/`netstat`/`fuser`), then retries automatically.

---

### Troubleshooting

**No videos appear in picker**
- Verify `.mp4` files exist in `/sdcard/Pictures/scoopz`

**Caption script exits with key error**
- Confirm `~/.config/openai_key.txt` exists and is not empty

**Copy button fails in browser**
- Use `http://127.0.0.1` (not `localhost` or `https`)
- Clipboard API requires a direct user gesture — tap the button
- If blocked, try Chrome

---

### Minimal End-to-End Test

1. Start `collector.py`
2. Add one TikTok URL to `/sdcard/Download/links_inbox.txt`
3. Confirm an `.mp4` appears in `/sdcard/Pictures/scoopz`
4. Start `newsbreak_captions.py`
5. Pick up to 7 videos and tap **GO**
6. Confirm the results panel appears and each caption can be copied

---

## License

MIT
