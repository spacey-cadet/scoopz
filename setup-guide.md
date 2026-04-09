# Scoopz Setup Guide (Current Workflow)

This guide reflects the current scripts in this project:
- collector.py
- newsbreak_captions.py
- scoopz_env.sh
- newsbreak_captions.sh (legacy helper)

## What Changed

The workflow is now split into two independent stages:
1. Collect TikTok videos into the Scoopz folder.
2. Pick any saved video and generate a NewsBreak-ready caption with Gemini.

The old upload pipeline referenced in scoopz_env.sh (for example uploader.py, downloads/, data/queue.json) is not present in this repository anymore.

## Runtime Overview

- collector.py watches a link inbox file and auto-downloads TikTok videos.
- newsbreak_captions.py launches a local web picker with thumbnails, then generates caption + hashtags and copies output to clipboard.
- Both scripts are designed for Termux on Android paths (/sdcard/..., termux-* commands).

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

## Optional: Local Transcription (Recommended)

If you want captions to adapt when there *is* speech, enable local transcription and feed the transcript into Gemini.

The caption script supports two options:

### Option A (Recommended): `whisper.cpp` CLI

This is usually the fastest + most stable on Termux.

Set these environment variables before running [newsbreak_captions.py](newsbreak_captions.py):

- `SCOOPZ_TRANSCRIBE=1`
- `SCOOPZ_WHISPER_CPP_BIN` (path or command name for the whisper.cpp binary)
- `SCOOPZ_WHISPER_CPP_MODEL` (path to your model file)
- `SCOOPZ_WHISPER_LANG=en` (optional)

Example:

```bash
export SCOOPZ_TRANSCRIBE=1
export SCOOPZ_WHISPER_CPP_BIN=~/whisper.cpp/main
export SCOOPZ_WHISPER_CPP_MODEL=~/whisper.cpp/models/ggml-tiny.en.bin
export SCOOPZ_WHISPER_LANG=en
```

### Option B: OpenAI Whisper CLI (`whisper`)

This works if you already have it installed, but it can be slower on phones.

```bash
pip install -U openai-whisper
export SCOOPZ_TRANSCRIBE=1
export SCOOPZ_WHISPER_MODEL=tiny
export SCOOPZ_WHISPER_LANG=en
```

## Gemini Audio Upload Toggle

By default, [newsbreak_captions.py](newsbreak_captions.py) does **not** upload audio to Gemini (it prefers local transcription).

If you want to upload audio anyway, set:

```bash
export SCOOPZ_GEMINI_SEND_AUDIO=1
```

## Required Folders and Files

The code expects these locations:

- Video output folder: /sdcard/Pictures/scoopz
- URL inbox file: /sdcard/Download/links_inbox.txt
- Download history file: /sdcard/Download/scoopz_history.log
- Gemini key file: ~/.config/gemini_key.txt

Create missing files/folders:

```bash
mkdir -p /sdcard/Pictures/scoopz
mkdir -p /sdcard/Download
mkdir -p ~/.config
: > /sdcard/Download/links_inbox.txt
: > /sdcard/Download/scoopz_history.log
```

Add your Gemini API key:

```bash
echo "YOUR_GEMINI_API_KEY" > ~/.config/gemini_key.txt
chmod 600 ~/.config/gemini_key.txt
```

## Daily Workflow

### Step 1: Start collector

From project folder:

```bash
python collector.py
```

What happens:
- The script polls /sdcard/Download/links_inbox.txt every 0.5s.
- Any TikTok URL found is queued.
- Up to 5 downloads run in parallel.
- Files are saved as /sdcard/Pictures/scoopz/<video_id>.mp4.
- Duplicate video IDs are skipped using /sdcard/Download/scoopz_history.log.
- Termux notifications confirm active status and completed downloads.

How to feed links:
- Put TikTok links into /sdcard/Download/links_inbox.txt (one per line).
- Once read, the inbox is cleared automatically.

### Step 2: Generate captions

In another Termux session:

```bash
python newsbreak_captions.py
```

What happens:
- Script scans /sdcard/Pictures/scoopz for mp4 files.
- It generates thumbnail previews in /sdcard/Pictures/scoopz/.thumbs.
- It starts local server on 127.0.0.1:8765.
- A persistent notification opens the picker page.
- Tap a video card to process that video.
- The selected video is compressed, uploaded to Gemini, analyzed, then deleted from Gemini.
- Output is returned as:
  - caption
  - hashtags
- Final text is clipped to max 150 characters and copied to clipboard.

## How the Code Works

## collector.py

Main responsibilities:
- Inbox watcher:
  - watch_inbox() reads links_inbox.txt, extracts TikTok URLs, pushes to async queue.
- Download workers:
  - download_worker() calls TikWM API with retry logic.
  - Downloads clip["play"] stream to mp4.
- History dedupe:
  - add_to_history() persists video IDs to scoopz_history.log.
- Notifications:
  - send_notification() posts low-priority Termux notifications.
- Runtime:
  - run() starts watcher + N workers and keeps process alive.

Important settings at top:
- MAX_WORKERS = 5
- POLL_RATE = 0.5
- TIKWM_API = https://www.tikwm.com/api/

## newsbreak_captions.py

Main responsibilities:
- UI and video selection:
  - build_html() renders mobile grid UI.
  - Handler.do_GET() handles /, /pick, /status.
- Thumbnail pipeline:
  - generate_thumbnails() uses ffmpeg to create first-frame jpg.
- Gemini pipeline:
  - compress_video() reduces upload size.
  - upload_video() uploads via resumable API.
  - wait_for_processing() waits for ACTIVE state.
  - generate_caption() sends prompt and retries on 429 limits.
  - format_result() enforces 150-char combined output.
  - delete_file() removes uploaded Gemini file.
- Clipboard and UX:
  - copy_to_clipboard() stores final result.
  - notify() and toast() provide status updates.

Important settings at top:
- MODEL = gemini-2.5-flash
- SCOOPZ_DIR = /sdcard/Pictures/scoopz
- TEMP_COMPRESSED = /sdcard/Download/nb_caption_compressed.mp4
- PORT = 8765

## Helper scripts

## scoopz_env.sh

Contains shell helpers and aliases, but parts of it still reference files not present in this repo:
- uploader.py
- downloads/
- data/uploaded.json
- data/queue.json

You can still use it for custom command wrappers, but treat upload/status sections as legacy until updated to the current collector + caption workflow.

## newsbreak_captions.sh

This helper currently calls:

```bash
python newsbreak_caption.py "$1"
```

That filename does not exist in this repository (current script is newsbreak_captions.py), so this helper should be treated as legacy or corrected before use.

## Recommended Command Set (Current)

Use these as your active workflow commands:

```bash
# terminal 1
python collector.py

# terminal 2
python newsbreak_captions.py
```

## Troubleshooting

- No videos appear in picker:
  - Verify files exist in /sdcard/Pictures/scoopz and end with .mp4.
- Collector does not download:
  - Confirm links are valid TikTok URLs.
  - Confirm links are being written to /sdcard/Download/links_inbox.txt.
- Caption script exits with key error:
  - Check ~/.config/gemini_key.txt exists and is non-empty.
- Port conflict on 8765:
  - Script already tries to kill previous listener; restart script if needed.
- Clipboard not updating:
  - Ensure Termux:API app is installed and termux-api package is installed in Termux.

## Minimal End-to-End Test

1. Start collector.py.
2. Add one TikTok URL to /sdcard/Download/links_inbox.txt.
3. Confirm an mp4 appears in /sdcard/Pictures/scoopz.
4. Start newsbreak_captions.py.
5. Open picker from notification, tap video, wait for "Caption Ready".
6. Paste clipboard output into Notes to confirm result format.
