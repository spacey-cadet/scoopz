# Scoopz Setup Guide (Current Workflow)

This guide reflects the current scripts in this project:
- collector.py
- newsbreak_captions.py
- scoopz_env.sh

## Runtime Overview

- collector.py watches a link inbox file and downloads TikTok videos to /sdcard/Pictures/scoopz.
- newsbreak_captions.py opens a local thumbnail picker, lets you select up to 8 videos, and generates captions in parallel.
- After generation completes, captions are auto-copied to clipboard one-by-one (with delay) to populate Gboard clipboard history.

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
- You select 1 to 8 videos and tap GO.
- For each selected video, ffmpeg extracts 4 frames and an audio clip.
- Captions are generated in parallel using OpenAI.
- When done, it auto-copies captions sequentially to clipboard history (Gboard-friendly) with a default delay of 2.0s between copies.

Important note:
- Android has one active clipboard slot.
- Multi-caption retrieval depends on keyboard clipboard history (Gboard clipboard memory).

## Current Defaults in newsbreak_captions.py

- MAX_BATCH_SIZE = 8
- NUM_FRAMES = 4
- EXTRACTION_WORKERS = 3
- API_WORKERS = 3
- PORT = 8765
- CLIPBOARD_AUTO_CYCLE = True
- CLIPBOARD_CYCLE_DELAY_SEC = 2.0

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

## Recommended Gboard Settings

Use these settings so the 8 auto-copied captions are retained in Gboard history:

1. Open any text field, tap the Gboard clipboard icon, and make sure Clipboard is turned on.
2. In Gboard settings, allow clipboard suggestions/history (wording may vary by Android version).
3. If your phone aggressively kills background apps, exclude Gboard from battery optimization.
4. If some entries are missed, increase CLIPBOARD_CYCLE_DELAY_SEC in newsbreak_captions.py to 2.5 or 3.0.
5. For captions you want to keep long-term, pin them in Gboard clipboard after generation.

## Troubleshooting

- No videos appear in picker:
  - Verify .mp4 files exist in /sdcard/Pictures/scoopz.
- Caption script exits with key error:
  - Confirm ~/.config/openai_key.txt exists and is not empty.
- Clipboard history missing some entries:
  - Ensure Gboard clipboard is enabled.
  - Increase CLIPBOARD_CYCLE_DELAY_SEC (for example 2.5 or 3.0).
- Clipboard not updating at all:
  - Ensure Termux:API app is installed and termux-api package exists in Termux.

## Minimal End-to-End Test

1. Start collector.py.
2. Add one TikTok URL to /sdcard/Download/links_inbox.txt.
3. Confirm an mp4 appears in /sdcard/Pictures/scoopz.
4. Start newsbreak_captions.py.
5. Pick up to 8 videos and tap GO.
6. Open Gboard clipboard and confirm copied captions are present in history.
