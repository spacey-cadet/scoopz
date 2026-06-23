# Scoopz

> TikTok collector + AI caption generator for Termux

Scoopz is a two-script Termux workflow for Android that watches a link inbox, downloads TikTok videos, and generates ready-to-post captions using OpenAI's vision API.

- **`collector.py`** — watches a link inbox file and downloads TikTok videos to `/sdcard/Pictures/scoopz`
- **`newsbreak_captions.py`** — opens a local thumbnail picker, lets you select up to 3 videos, and generates captions in parallel
- **`scoopz_env.sh`** — adds shortcut commands (`collect`, `caption`, `scoopz-help`) to your shell
- **`app/`** — FastAPI scaffold for exposing the collector, video library, and caption workflow over HTTP

---

## FastAPI Scaffold

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

## Prerequisites

### Termux Packages

```bash
pkg update && pkg upgrade -y
pkg install -y python ffmpeg termux-api
pip install --upgrade pip
pip install requests
```

### Storage Permission

```bash
termux-setup-storage
```

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/spacey-cadet/scoopz ~/scoopz
cd ~/scoopz
```

### 2. Install shell commands

```bash
chmod +x scoopz_env.sh
echo 'source ~/scoopz/scoopz_env.sh' >> ~/.bashrc
source ~/.bashrc
```

### 3. Verify

```bash
scoopz-help
```

You should see: `Scoopz ready` and the list of workflow commands.

---

## Required Folders & Files

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

## Daily Workflow

### Terminal 1 — Collector

```bash
python collector.py
```

- Polls `/sdcard/Download/links_inbox.txt`
- Downloads valid TikTok URLs to `/sdcard/Pictures/scoopz`
- Skips duplicates using `scoopz_history.log`

### Terminal 2 — Captions

```bash
python newsbreak_captions.py
```

- Scans `/sdcard/Pictures/scoopz` for mp4 files and generates thumbnails
- Starts local picker at `http://127.0.0.1:8765`
- Select 1–3 videos and tap **GO**
- `ffmpeg` extracts 4 frames per video; captions generated in parallel via OpenAI
- Results appear in-page with per-caption **Copy** buttons

---

## Configuration

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

### Port Handling

The server tries to bind on port `8765` normally. If the port is in use, it attempts to free the listener process (`lsof`/`ss`/`netstat`/`fuser`), then retries automatically.

---

## Troubleshooting

**No videos appear in picker**
- Verify `.mp4` files exist in `/sdcard/Pictures/scoopz`

**Caption script exits with key error**
- Confirm `~/.config/openai_key.txt` exists and is not empty

**Copy button fails in browser**
- Use `http://127.0.0.1` (not `localhost` or `https`)
- Clipboard API requires a direct user gesture — tap the button
- If blocked, try Chrome

---

## Minimal End-to-End Test

1. Start `collector.py`
2. Add one TikTok URL to `/sdcard/Download/links_inbox.txt`
3. Confirm an `.mp4` appears in `/sdcard/Pictures/scoopz`
4. Start `newsbreak_captions.py`
5. Pick up to 7 videos and tap **GO**
6. Confirm the results panel appears and each caption can be copied

---

## License

MIT
