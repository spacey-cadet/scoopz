# Scoopz Setup Guide (Hybrid Evidence Mode)

This repository contains:
- collector.py (Termux/phone)
- newsbreak_captions.py (Termux/phone picker + evidence upload)
- scoopz_laptop_server.py (laptop “brain”: OCR + Whisper + local LLM)
- scoopz_env.sh (Termux helper commands)

## What This Does Now

The workflow is split across two devices:

1) Phone (Termux): download videos + pick exactly 8 + extract a small “evidence pack”
  - 2 JPEG frames per video
  - optional short audio clip (for Whisper)
  - uploads evidence packs to your laptop over Wi‑Fi

2) Laptop: runs OCR + Whisper locally, and uses OpenAI to write captions from text-only evidence



## Runtime Overview

- collector.py watches a link inbox file and downloads TikTok videos to `/sdcard/Pictures/scoopz`.
- newsbreak_captions.py runs a local web picker on your phone (127.0.0.1:8765).
- When you press Generate, the phone extracts evidence packs and POSTs them to `SCOOPZ_REMOTE_URL + /batch`.
- scoopz_laptop_server.py receives the evidence and generates captions locally.

## Phone Setup (Termux)

Assumes this repo lives at `~/scoopz` on the phone.

Install required packages:

```bash
pkg update && pkg upgrade -y
pkg install -y python ffmpeg termux-api
pip install --upgrade pip
pip install -r ~/scoopz/requirements.txt
```

Optional (recommended): use a Python virtual environment on Termux:

```bash
cd ~/scoopz
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

Give Termux storage permission (required for /sdcard access):

```bash
termux-setup-storage
```

Create required folders/files:

```bash
mkdir -p /sdcard/Pictures/scoopz
mkdir -p /sdcard/Download
: > /sdcard/Download/links_inbox.txt
: > /sdcard/Download/scoopz_history.log
```

Set your laptop server URL (replace with your laptop IP):

```bash
export SCOOPZ_REMOTE_URL="http://192.168.0.103:9000"
```

Audio upload toggle (recommended ON if most videos have speech):

```bash
export SCOOPZ_TRANSCRIBE=1
```

Tip: to persist these, add them to `~/.bashrc` or put them in `~/scoopz/scoopz_env.sh` and `source` it.

## Laptop Setup (Server)

Assumes this repo lives on your laptop (any path is fine). If you have the repo open in VS Code on the laptop already, you’re good.

Install base dependencies (Ubuntu/Debian example):

```bash
sudo apt update
sudo apt install -y python3 python3-pip ffmpeg git build-essential cmake
sudo apt install -y tesseract-ocr  # optional but recommended for on-screen text
```

Recommended: create a Python virtual environment for the server:

```bash
cd /path/to/scoopz
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

You also need local model binaries:

- whisper.cpp (`whisper-cli`) + a ggml model (base.en/tiny.en)

### Build whisper.cpp (CPU)

```bash
cd ~/Downloads
git clone https://github.com/ggerganov/whisper.cpp
cd whisper.cpp
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j

# download a model (base.en is a good default)
bash ./models/download-ggml-model.sh base.en

# optional: put whisper-cli on PATH
mkdir -p ~/bin
cp -f build/bin/whisper-cli ~/bin/
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.bashrc
```

Important: both projects may also build a generic binary called `main`. Avoid using `main` for Scoopz to prevent running the wrong executable.

Set required env vars on the laptop:

```bash
export SCOOPZ_TRANSCRIBE=1
export SCOOPZ_WHISPER_BIN="$HOME/bin/whisper-cli"
export SCOOPZ_WHISPER_MODEL="$HOME/Downloads/whisper.cpp/models/ggml-base.en.bin"
export SCOOPZ_SERVER_PORT=9000

# OpenAI caption generation (required)
export SCOOPZ_OPENAI_API_KEY="YOUR_KEY_HERE"
export SCOOPZ_OPENAI_MODEL="gpt-4o-mini"   # change if you prefer
export SCOOPZ_OPENAI_TIMEOUT_S=120
export SCOOPZ_OPENAI_TEMP=0.8

# Controls response length
export SCOOPZ_LLM_MAX_TOKENS=900
```

Data sent to OpenAI:
- OCR text extracted from frames (lower-thirds/tickers)
- Whisper transcript text (if enabled)
- The caption-formatting prompt

Data NOT sent to OpenAI:
- Images, audio, or video files

Start the server:

```bash
python3 scoopz_laptop_server.py
```

Make sure your firewall allows inbound TCP `9000` on your LAN. If you use `ufw`:

```bash
sudo ufw allow 9000/tcp
```

Confirm the server is running locally:

```bash
curl http://127.0.0.1:9000/health
```

You should see: `{ "ok": true }`.

Then confirm your phone can reach it over Wi‑Fi:

```bash
curl http://<LAPTOP_IP>:9000/health
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

In another Termux session (ensure `SCOOPZ_REMOTE_URL` is set):

```bash
python newsbreak_captions.py
```

What happens:
- Script scans `/sdcard/Pictures/scoopz` for mp4 files.
- Generates thumbnails in `/sdcard/Pictures/scoopz/.thumbs`.
- Starts local picker server on `127.0.0.1:8765`.
- You select exactly 8 videos.
- Phone extracts 2 frames (+ optional audio) per selected video.
- Phone uploads evidence packs to the laptop server.
- Laptop generates captions and returns them.
- Phone copies captions to clipboard sequentially.

## Notes

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

If captions fail with “Set SCOOPZ_REMOTE_URL…”, set it in the same Termux session (or add it to your `~/.bashrc` / `scoopz_env.sh`).

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
- Phone says “Set SCOOPZ_REMOTE_URL…” or batch fails immediately:
  - Export `SCOOPZ_REMOTE_URL` in the same Termux session running the picker.
  - Verify laptop server is running and reachable: `curl http://<LAPTOP_IP>:9000/health`.
- Laptop server runs but phone can’t connect:
  - Ensure both devices are on the same Wi‑Fi.
  - Allow port 9000 in firewall (`sudo ufw allow 9000/tcp`).
  - Use the laptop’s LAN IP (usually `192.168.x.x`), not `127.0.0.1`.
- Port conflict on 8765:
  - Script already tries to kill previous listener; restart script if needed.
- Clipboard not updating:
  - Ensure Termux:API app is installed and termux-api package is installed in Termux.

## Minimal End-to-End Test

1. On laptop: start `python3 scoopz_laptop_server.py` and confirm `curl http://127.0.0.1:9000/health` works.
2. On phone: confirm `curl http://<LAPTOP_IP>:9000/health` works.
3. On phone: run `python collector.py` and add TikTok links to `/sdcard/Download/links_inbox.txt` until you have at least 8 mp4s.
4. On phone: run `python newsbreak_captions.py`, open the picker, select exactly 8 videos, and tap Generate.
5. Confirm captions are copied to clipboard one-by-one.
