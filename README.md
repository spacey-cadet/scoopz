# Scoopz

> TikTok collector + AI caption generator for Termux

Scoopz is a two-script Termux workflow for Android that watches a link inbox, downloads TikTok videos, and generates ready-to-post captions using OpenAI's vision API.

- **`collector.py`** — watches a link inbox file and downloads TikTok videos to `/sdcard/Pictures/scoopz`
- **`newsbreak_captions.py`** — opens a local thumbnail picker, lets you select up to 3 videos, and generates captions in parallel
- **`scoopz_env.sh`** — adds shortcut commands (`collect`, `caption`, `scoopz-help`) to your shell

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
