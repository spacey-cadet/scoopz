# Scoopz Setup Guide (Current Workflow)

This guide reflects the current scripts in this project:
- collector.py
- newsbreak_captions.py
- scoopz_env.sh

## Runtime Overview

- collector.py watches a link inbox file and downloads TikTok videos to /sdcard/Pictures/scoopz.
- newsbreak_captions.py opens a local thumbnail picker, lets you select up to 3 videos, and generates captions in parallel.
- After generation completes, all captions appear in a results panel with per-caption Copy buttons.

## Prerequisites (Termux)

Install required packages:

```bash
pkg update && pkg upgrade -y
pkg install -y python ffmpeg termux-api
pip install --upgrade pip
pip install requests
```

Give Termux storage permission (required for /sdcard access):

```bash
termux-setup-storage
```

## Install the Shell Script Commands

The project includes `scoopz_env.sh`, which adds shortcut commands like `collect`, `caption`, and `scoopz-help`.

1. Put this project at `~/scoopz` (or update `SCOOPZ_DIR` inside `scoopz_env.sh` to your real folder).
2. Source the script from your shell startup.

Run:

```bash
cd ~/scoopz
chmod +x scoopz_env.sh
echo 'source ~/scoopz/scoopz_env.sh' >> ~/.bashrc
source ~/.bashrc
```

Verify installation:

```bash
scoopz-help
```

You should see: `Scoopz ready` and the list of workflow commands.

## Required Folders and Files

The current code expects:

- Video output folder: /sdcard/Pictures/scoopz
- Thumbnail cache: /sdcard/Pictures/scoopz/.thumbs
- Temp extraction folder: /sdcard/Download/scoopz_temp
- URL inbox file: /sdcard/Download/links_inbox.txt
- Download history file: /sdcard/Download/scoopz_history.log
- OpenAI key file: ~/.config/openai_key.txt

Create missing files/folders:

```bash
mkdir -p /sdcard/Pictures/scoopz
mkdir -p /sdcard/Download
mkdir -p ~/.config
: > /sdcard/Download/links_inbox.txt
: > /sdcard/Download/scoopz_history.log
```

Add OpenAI API key:

```bash
echo "YOUR_OPENAI_API_KEY" > ~/.config/openai_key.txt
chmod 600 ~/.config/openai_key.txt
```

## Daily Workflow

### Step 1: Start collector

```bash
python collector.py
```

What happens:
- Polls /sdcard/Download/links_inbox.txt.
- Queues valid TikTok URLs.
- Downloads videos into /sdcard/Pictures/scoopz.
- Skips duplicates using /sdcard/Download/scoopz_history.log.

### Step 2: Generate captions

In another Termux session:

```bash
python newsbreak_captions.py
```

What happens:
- Script scans /sdcard/Pictures/scoopz for mp4 files.
- Generates thumbnails in /sdcard/Pictures/scoopz/.thumbs.
- Starts local picker on 127.0.0.1:8765.
- You select 1 to 3 videos and tap GO.
- For each selected video, ffmpeg extracts 4 frames.
- Captions are generated in parallel using OpenAI.
- When done, captions are shown in-page; tap Copy on the caption you want while posting.

Important note:
- Direct in-page copy avoids dependence on clipboard history limits.

## Current Defaults in newsbreak_captions.py

- MAX_BATCH_SIZE = 3
- NUM_FRAMES = 4
- EXTRACTION_WORKERS = 3
- API_WORKERS = 3
- PORT = 8765
- CLIPBOARD_AUTO_CYCLE = False
- CLIPBOARD_CYCLE_DELAY_SEC = 3.0

## Port Handling (8765)

Server startup is hardened:
- It tries to bind normally first.
- If port 8765 is in use, it attempts to free the listener process (best-effort using lsof/ss/netstat/fuser), then retries bind.

## Recommended Command Set

```bash
# terminal 1
python collector.py

# terminal 2
python newsbreak_captions.py
```

## Troubleshooting

- No videos appear in picker:
  - Verify .mp4 files exist in /sdcard/Pictures/scoopz.
- Caption script exits with key error:
  - Confirm ~/.config/openai_key.txt exists and is not empty.
- Copy button fails in browser:
  - Ensure you are using the page over http://127.0.0.1 and interact by tapping the button (clipboard API requires user gesture).
  - If your browser blocks clipboard API, try opening in Chrome.

## Minimal End-to-End Test

1. Start collector.py.
2. Add one TikTok URL to /sdcard/Download/links_inbox.txt.
3. Confirm an mp4 appears in /sdcard/Pictures/scoopz.
4. Start newsbreak_captions.py.
5. Pick up to 3 videos and tap GO.
6. Confirm the results panel appears and each caption can be copied with its Copy button.
