import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
import requests
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
INBOX_PATH   = Path("/sdcard/Download/links_inbox.txt")
EXPORT_DIR   = Path("/sdcard/Pictures/scoopz")
HISTORY_FILE = Path("/sdcard/Download/scoopz_history.log")

EXPORT_DIR.mkdir(parents=True, exist_ok=True)
if not HISTORY_FILE.exists(): 
    HISTORY_FILE.touch()

# ── Settings ──────────────────────────────────────────────────────────────────
MAX_WORKERS = 5            
POLL_RATE   = 0.5          
TIKWM_API   = "https://www.tikwm.com/api/"
TIKTOK_RE   = re.compile(r"https?://(?:(?:www|vt|vm)\.)?tiktok\.com/\S+", re.I)

# Reject tiny/partial files and retry instead of saving unreadable mp4s.
# You can override this if needed: SCOOPZ_MIN_BYTES=0
MIN_BYTES = int(os.environ.get("SCOOPZ_MIN_BYTES", "250000"))

# Download retries (per video). Lower = faster, higher = more resilient.
DOWNLOAD_RETRIES = int(os.environ.get("SCOOPZ_DOWNLOAD_RETRIES", "2"))

# Extra safety: reject videos that cannot yield a thumbnail frame.
# Set SCOOPZ_VALIDATE_THUMBNAIL=0 to disable this check.
VALIDATE_THUMBNAIL = os.environ.get("SCOOPZ_VALIDATE_THUMBNAIL", "1") != "0"

class ScoopzHybrid:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.session = requests.Session()
        # Disguise the script slightly to prevent API blocking
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        self.done = 0
        self.failed = 0
        self.active = 0
        self.history = set(HISTORY_FILE.read_text().splitlines())
    
    def update_ui(self):
        queued = self.queue.qsize()
        sys.stdout.write(f"\r\033[K ⬇️  Active: {self.active} | ✅ Done: {self.done} | ❌ Fail: {self.failed} | 🕒 Queued: {queued}")
        sys.stdout.flush()
    
    def send_notification(self, title, msg):
        subprocess.run([
            "termux-notification", 
            "-t", title, 
            "-c", msg, 
            "--id", "scoopz_notif",
            "--priority", "low"
        ], capture_output=True)
    
    def add_to_history(self, video_id):
        self.history.add(video_id)
        with open(HISTORY_FILE, "a") as f:
            f.write(f"{video_id}\n")
    
    async def watch_inbox(self):
        while True:
            if INBOX_PATH.exists() and INBOX_PATH.stat().st_size > 0:
                try:
                    raw_content = INBOX_PATH.read_text().strip()
                    if raw_content:
                        found_urls = [l.strip() for l in raw_content.splitlines() if TIKTOK_RE.search(l)]
                        if found_urls:
                            INBOX_PATH.write_text("") # Clear inbox immediately
                            for url in found_urls:
                                await self.queue.put(url)
                            self.update_ui()
                except Exception:
                    pass
            await asyncio.sleep(POLL_RATE)

    def _sync_download(self, play_url, final_path):
        """Runs blocking requests operations safely in a thread"""
        temp_path = final_path.with_suffix('.mp4.part')

        def _can_extract_thumbnail(path: Path) -> bool:
            # Mirror picker behavior: if none of these seeks work,
            # this file will likely show as no-thumb in the web picker.
            seek_points = ["0.1", "0.5", "1", "2"]
            for seek in seek_points:
                try:
                    r = subprocess.run(
                        [
                            "ffmpeg",
                            "-nostdin",
                            "-v",
                            "error",
                            "-ss",
                            seek,
                            "-i",
                            str(path),
                            "-vframes",
                            "1",
                            "-f",
                            "null",
                            "-",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=12,
                        check=False,
                    )
                    if r.returncode == 0:
                        return True
                except FileNotFoundError:
                    # If ffmpeg is missing, do not block the download here.
                    return True
                except Exception:
                    pass
            return False

        def _looks_valid_mp4(path: Path) -> bool:
            try:
                if not path.exists():
                    return False
                if MIN_BYTES > 0 and path.stat().st_size < MIN_BYTES:
                    return False
            except Exception:
                return False

            # Prefer ffprobe if available (fast + accurate).
            try:
                r = subprocess.run(
                    [
                        "ffprobe",
                        "-v", "error",
                        "-select_streams", "v:0",
                        "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1",
                        str(path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if r.returncode != 0:
                    return False
                dur_s = (r.stdout or "").strip()
                try:
                    dur = float(dur_s)
                except Exception:
                    dur = 0.0
                return dur > 0.5
            except FileNotFoundError:
                # ffprobe not installed; best-effort fallback already checked size.
                pass
            except Exception:
                return False

            if VALIDATE_THUMBNAIL and not _can_extract_thumbnail(path):
                return False

            # Align with Scoopz usage: ffmpeg must be able to decode at least one frame.
            try:
                r = subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-i",
                        str(path),
                        "-map",
                        "0:v:0",
                        "-frames:v",
                        "1",
                        "-f",
                        "null",
                        "-",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=12,
                    check=False,
                )
                return r.returncode == 0
            except FileNotFoundError:
                # ffmpeg not installed: fall back to size-only.
                return True
            except Exception:
                return False
        
        retries = max(1, DOWNLOAD_RETRIES)

        # Retry loop for the actual video file
        for attempt in range(retries):
            try:
                with self.session.get(play_url, stream=True, timeout=30) as r:
                    r.raise_for_status()
                    with open(temp_path, "wb") as f:
                        shutil.copyfileobj(r.raw, f)

                # Validate before saving into the gallery folder.
                if not _looks_valid_mp4(temp_path):
                    try:
                        temp_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    if attempt < retries - 1:
                        time.sleep(2)
                    continue
                
                # Success! Rename to final mp4
                temp_path.rename(final_path)
                return True
                
            except Exception:
                # Clean up corrupted partial file if it failed
                if temp_path.exists():
                    temp_path.unlink()
                
                if attempt < retries - 1:
                    time.sleep(2) # Wait before retry
        return False

    async def download_worker(self):
        while True:
            url = await self.queue.get()
            self.active += 1
            self.update_ui()
            
            try:
                data = None
                # API Retry Loop
                for attempt in range(3):
                    try:
                        # TikWM sometimes prefers POST data over GET params
                        resp = await asyncio.to_thread(
                            self.session.post, 
                            TIKWM_API, 
                            data={"url": url, "hd": 1}, 
                            timeout=15
                        )
                        data = resp.json()
                        if data.get("code") == 0:
                            break
                        if data.get("code") == -1:
                            await asyncio.sleep(2)
                    except Exception:
                        await asyncio.sleep(2)
                
                if not data or data.get("code") != 0:
                    self.failed += 1
                    continue
                
                clip = data.get("data", {})
                video_id = clip.get("id", "unknown")
                
                # Check history early to save bandwidth
                if video_id in self.history:
                    self.done += 1
                    continue
                
                # Prioritize HD, fallback to standard, fail gracefully if it's a photo post
                play_url = clip.get("hdplay") or clip.get("play")
                if not play_url:
                    self.failed += 1 # Likely an image carousel, not a video
                    continue
                
                final_path = EXPORT_DIR / f"{video_id}.mp4"
                
                # Offload the heavy file downloading to a background thread
                success = await asyncio.to_thread(self._sync_download, play_url, final_path)
                
                if success:
                    subprocess.run(["termux-media-scan", str(final_path)], capture_output=True)
                    self.add_to_history(video_id)
                    self.done += 1
                    self.send_notification("Scoopz Downloaded", f"Saved {video_id} to Gallery")
                else:
                    self.failed += 1

            except Exception:
                self.failed += 1
            finally:
                self.active -= 1
                self.queue.task_done()
                self.update_ui()
    
    async def run(self):
        self.send_notification("Scoopz Active", "Watching for TikTok links...")
        print("📋 Starting collector — browse TikTok and Share → Copy Link\n")
        workers = [asyncio.create_task(self.download_worker()) for _ in range(MAX_WORKERS)]
        watcher = asyncio.create_task(self.watch_inbox())
        await asyncio.gather(watcher, *workers)

if __name__ == "__main__":
    try:
        asyncio.run(ScoopzHybrid().run())
    except KeyboardInterrupt:
        print("\n\n👋 Stopped.")