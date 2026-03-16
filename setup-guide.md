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
> Prefer a venv so Scoopz dependencies stay isolated. If you already keep the scripts plus dependencies in the global Termux Python, skip this step and the later `source .venv/bin/activate` commands.

## 6. Install Python dependencies
```bash
pip install -r requirements.txt
```
This installs `adbutils`, `uiautomator2`, `yt-dlp`, and `watchdog`.
> Global Python users: just run the same command without activating `.venv`; it installs system-wide.

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

## 8. Pin the default ADB target in Termux (recommended)
Create a small env file that exports the device you just connected to so every Termux shell, Python script, or `adb` call points at it automatically.

```bash
mkdir -p ~/.config
cat <<'EOF' > ~/.config/scoopz-adb.env
export ANDROID_SERIAL=127.0.0.1:5555   # update if you use a different IP:port
export ADBHOST=127.0.0.1
export ADBPORT=5555
EOF
```

Source that file from your Termux shell (bash by default):

```bash
if ! grep -q 'scoopz-adb.env' ~/.bashrc; then
  echo 'source ~/.config/scoopz-adb.env' >> ~/.bashrc
fi
source ~/.bashrc
adb devices   # now always shows the pinned IP without picking from a list
```

Whenever the phone chooses a new wireless debugging port/IP, edit `~/.config/scoopz-adb.env` and rerun `source ~/.bashrc`. Exporting `ANDROID_SERIAL` keeps `adb`, `adbutils`, and `uiautomator2` from prompting you to choose a device even if an emulator or second phone is connected.

## 9. Prepare persistent data files (first run only)
The scripts auto-create their data files (`data/queue.json`, `data/urls.txt`, `data/uploaded.json`) and download/log directories. No manual work is required, but you can wipe them if you want to reset state.

## 10. Running the workflows
- Start the collector before opening TikTok:
  ```bash
  source .venv/bin/activate   # skip this line if you use global Python
  python collector.py
  ```
  Leave it running while you browse. Every Share → Copy Link action queues and downloads clips in the background.

- When you are ready to post on NewsBreak, make sure you are on the upload page inside the app, then run:
  ```bash
  source .venv/bin/activate   # skip if using global Python
  python uploader.py
  ```
  The bot uploads up to 40 fresh videos from `downloads/`, copies each file to `~/storage/shared/scoopz_downloads/`, reuses the default caption, and logs progress to `logs/uploader.log`.

## 11. Stopping the services
Press `Ctrl+C` in Termux to stop either script. Both maintain persistent queues/state so you can resume later without losing progress.
