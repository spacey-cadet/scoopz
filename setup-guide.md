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

## 3. Install Termux companion apps
- **Termux:API** (F-Droid/Play Store) — unlocks clipboard + toast commands.
- **Termux:Widget** — optional but recommended; lets you pin one-tap scripts (used for captions).

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
This installs `adbutils`, `uiautomator2`, `requests`, and friends.
> Global Python users: just run the same command without activating `.venv`; it installs system-wide.

After dependencies finish, source the helper aliases once per shell:

```bash
source ~/scoopz/scoopz_env.sh
```

Add that line to `~/.bashrc` (already shown at the top of `scoopz_env.sh`) so commands such as `collect`, `next-caption`, and `upload` are always available.

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

## 10. Running the workflows (faster command layer)
All shell commands below assume you already ran `source ~/scoopz/scoopz_env.sh` (or opened a new Termux session after adding it to `.bashrc`).

- Start the collector before opening TikTok:
  ```bash
  collect
  ```
  Leave it running while you browse. Each Share → Copy Link event gets detected, downloaded, and mirrored to both `downloads/` and `~/storage/shared/scoopz_downloads/`.

- Captions are generated automatically: every downloaded MP4 gets a sibling `.txt` built from the TikTok title plus the standard tags `#viral #trending #funny #interesting #scoopz`.

- Before posting in NewsBreak, tap the caption helper:
  ```bash
  next-caption
  ```
  This copies the oldest caption to the clipboard, shows a toast (when Termux:API is installed), and moves the matching MP4/TXT pair into `downloads/done/` so you never double-post.

- Ready to publish? Plug into ADB (or use wireless) and run:
  ```bash
  upload
  ```
  The bot uploads up to 40 fresh videos, pasting from `~/storage/shared/scoopz_downloads/`.

## 11. Optional one-tap caption widget
1. Install Termux:Widget (Step 3) and long-press the Android home screen.
2. Run once inside Termux:
   ```bash
   install-next-caption-widget
   ```
   This creates `~/.shortcuts/next-caption.sh`, the script Termux:Widget will call.
3. Add the Termux:Widget to your home screen and pick `next-caption` from the list.

Daily flow: tap the widget on your way to NewsBreak; it copies the next caption, shows the first 40 characters via toast, and archives the matching media. No extra steps when you reach the caption box—just paste.

## 12. Clearing storage when space gets tight
Run the built-in cleaner whenever Termux storage or shared gallery space starts to fill up:

```bash
scoopz-clean           # deletes everything
scoopz-clean 5         # keep newest 5 videos, purge the rest
```

It removes old MP4/TXT pairs from `downloads/`, empties `downloads/done/`, wipes `~/storage/shared/scoopz_downloads/`, and triggers `termux-media-scan` so the Android gallery refreshes instantly.

## 13. Stopping the services
Press `Ctrl+C` in Termux to stop either script. Both maintain persistent queues/state so you can resume later without losing progress.
