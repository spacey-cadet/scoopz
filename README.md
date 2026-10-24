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

### Optimization techniques

**Downloads**
- Streamed to disk in 64 KB chunks and written straight into MediaStore (marked pending until complete), so a video is never held in RAM.
- At most 3 downloads at once, behind a semaphore.
- The same video shared under different links is detected by video id and reused instead of downloaded again.
- Automatic retries with exponential backoff (up to 3 attempts), and a half-written file is deleted on failure or cancel.
- Runs only when the network is connected, as expedited background work with a foreground notification, so Android doesn't kill it.
- Progress notifications update every 10%, not on every chunk.
- Unique work names per link, so double-sharing never queues the same download twice.

**Captioning**
- Frames are picked on the phone: 8 sampled, scored for sharpness, faces and on-screen text, and only the best 1-2 are sent, instead of sending every frame.
- One full frame is decoded at a time, and at most 3 small bitmaps are held. Every bitmap is recycled as soon as it is used.
- The stitched image is a 384 px-high JPEG in 16-bit colour whose quality is lowered until it is under ~140 KB, which keeps requests small and fast.
- Short responses (`max_tokens` 300) and a request timeout of about 45 s.
- A slow or failing model automatically falls back to the backup model instead of making you wait.
- Up to 3 captions run in parallel. Each card updates on its own as soon as its caption is ready.
- A rate limiter spaces requests to stay under the free tier's requests-per-minute cap, and a `429` response pauses all queued requests for the server's `Retry-After` time.
- An alternative image format is tried automatically if a model rejects the first one.

**Interface and storage**
- The library list is lazy, keyed by video, and fed by a Room `Flow` that only runs while the screen is visible.
- Thumbnails are decoded at 256 px, with the memory cache capped at 12% of app memory and the disk cache at 64 MB.
- The API key lives in encrypted preferences (Android Keystore), and app backup is turned off so it never leaves the phone.
- Release builds use R8 shrinking and resource shrinking to keep the APK small.
- The share target is a transparent activity that queues the link and closes instantly, so sharing never interrupts what you are doing.

**Planned**
- Instant text-only draft captions from the TikTok title, with an optional vision upgrade.
- A second free provider as a further fallback.
- Audio transcription.

### Error handling

Failures go through a small error layer instead of showing raw messages. Each failed card shows a **title**, **what happened** and **how to fix it**, with a **Settings** shortcut when the fix lives there, plus **Details** (the raw message) and **Ask AI**.

| Code | Shown as | Typical cause | Fix offered |
|------|----------|---------------|-------------|
| `E_NO_NETWORK` | No internet connection | Offline, DNS or connection failure | Check connection, Retry |
| `E_TIMEOUT` | The server took too long | Busy free-tier server | Retry later, faster or backup model |
| `E_NO_KEY` | No NVIDIA key saved | Key not entered | Open Settings, paste key |
| `E_BAD_KEY` | NVIDIA rejected your key (401) | Wrong or deleted key | New key in Settings |
| `E_MODEL_403` | Model not enabled for your key | Model needs *Try API* once | Enable on build.nvidia.com or change model |
| `E_MODEL_404` | Model not available | Retired or not offered to your account | Pick another model, clear backup |
| `E_RATE` | Too many requests (429) | Over ~40 requests/minute | Wait, lower the per-minute limit |
| `E_SERVER` | NVIDIA server problem (5xx) | Their outage | Try again later |
| `E_REJECTED` | Request rejected (400/413) | Not a vision model, or image refused | Choose a vision model |
| `E_BAD_REPLY` | Unusable answer | Empty or unreadable model reply | Redo, or another model |
| `E_NO_FRAMES` | Can't read the video file | Damaged, incomplete or deleted file | Delete the card, share again |
| `E_TIKWM` | Downloader service is down | tikwm unreachable | Retry later |
| `E_DL_EXPIRED` | Download link expired or blocked | Direct link stopped working | Retry (fresh link) |
| `E_STORAGE` | Couldn't save the video | Storage full or refused | Free space, Retry |
| `E_LINK` | This link couldn't be downloaded | Private, deleted, region locked or not a video | Check it plays in TikTok |
| `E_UNKNOWN` | Something unexpected went wrong | Anything not matched above | Retry, Ask AI, Details |

How it works: `ErrorExplainer.encode()` runs where a job fails and stores `CODE` plus the raw message on the card (no database change). The UI decodes it back into the matrix row. When several models failed, the main model's error is the headline and all errors stay under Details.

**Ask AI** sends only the error text to your NVIDIA model (main model, then backup) and shows a short plain-language explanation in a dialog. Tokens, API keys and links are redacted first, and no video or caption data is sent. It is hidden for errors where it can't help (no key, no internet, storage). To add a new known error, add a row in `error/ErrorExplainer.kt` and a matching rule in `classify()`.

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
    error/                    # Error matrix (ErrorExplainer) + optional AI explanations (ErrorAi)
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
