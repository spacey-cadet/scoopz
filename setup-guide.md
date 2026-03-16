# Termux Setup Guide

Follow these steps once on your Android device inside Termux.

## 1. Grant storage access
```bash
termux-setup-storage
```
This creates the `~/storage` bridge. The automation scripts copy finished downloads into `~/storage/shared/scoopz_downloads` so NewsBreak can see them.

## 2. Install core packages
```bash
pkg update -y && pkg upgrade -y
pkg install -y python git termux-api android-tools
```
- `android-tools` provides the `adb` binary.
- `termux-api` exposes clipboard access for the collector workflow.

## 3. Install Termux:API companion app
From F-Droid or the Play Store, install **Termux:API** (matches the CLI package) so `termux-clipboard-get` works.

## 4. Clone or copy this project
Place the repo (or the two scripts plus data folders) under `~/Documents/Space/scoopz`. Adjust paths in the commands below if you keep it elsewhere.

## 5. Create a Python virtual environment (recommended)
```bash
cd ~/Documents/Space/scoopz
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

## 6. Install Python dependencies
```bash
pip install -r requirements.txt
```
This installs `adbutils`, `uiautomator2`, `yt-dlp`, and `watchdog`.

## 7. Enable wireless ADB on the phone
1. Enable Developer Options and USB debugging.
2. Connect the phone to a PC (or to itself via Termux if already rooted) and run:
   ```bash
   adb devices
   adb tcpip 5555
   adb connect 127.0.0.1:5555    # replace with actual IP if connecting from another device
   ```
3. Back in Termux, verify the connection:
   ```bash
   adb devices
   ```
   You should see `127.0.0.1:5555` (or your device IP) listed. `collector.py` and `uploader.py` use this host/port.

## 8. Prepare persistent data files (first run only)
The scripts auto-create their data files (`data/queue.json`, `data/urls.txt`, `data/uploaded.json`) and download/log directories. No manual work is required, but you can wipe them if you want to reset state.

## 9. Running the workflows
- Start the collector before opening TikTok:
  ```bash
  source .venv/bin/activate   # if you created the venv
  python collector.py
  ```
  Leave it running while you browse. Every Share → Copy Link action queues and downloads clips in the background.

- When you are ready to post on NewsBreak, make sure you are on the upload page inside the app, then run:
  ```bash
  source .venv/bin/activate
  python uploader.py
  ```
  The bot uploads up to 40 fresh videos from `downloads/`, copies each file to `~/storage/shared/scoopz_downloads/`, reuses the default caption, and logs progress to `logs/uploader.log`.

## 10. Stopping the services
Press `Ctrl+C` in Termux to stop either script. Both maintain persistent queues/state so you can resume later without losing progress.
