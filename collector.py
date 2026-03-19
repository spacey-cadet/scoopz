import asyncio
import os
import re
import shutil
import subprocess
import requests
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
INBOX_PATH   = Path("/sdcard/Download/links_inbox.txt")
EXPORT_DIR   = Path("/sdcard/Pictures/scoopz")
HISTORY_FILE = Path("/sdcard/Download/scoopz_history.log")

EXPORT_DIR.mkdir(parents=True, exist_ok=True)
if not HISTORY_FILE.exists(): HISTORY_FILE.touch()

# ── Settings ──────────────────────────────────────────────────────────────────
MAX_WORKERS = 5            
POLL_RATE   = 0.5          
TIKWM_API   = "https://www.tikwm.com/api/"
TIKTOK_RE   = re.compile(r"https?://(?:(?:www|vt|vm)\.)?tiktok\.com/\S+", re.I)

class ScoopzHybrid:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.session = requests.Session()
        self.done = 0
        self.failed = 0
        self.active = 0
        self.history = set(HISTORY_FILE.read_text().splitlines())

    def update_ui(self):
        queued = self.queue.qsize()
        print(f"\r ⬇️ Active: {self.active} | ✅ Done: {self.done} | ❌ Fail: {self.failed} | 🕒 Queued: {queued}    ", end="", flush=True)

    def send_notification(self, title, msg):
        """Sends a native Android notification."""
        subprocess.run([
            "termux-notification", 
            "-t", title, 
            "-c", msg, 
            "--id", "scoopz_notif",
            "--priority", "low" # Low priority so it doesn't vibrate your hand off
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
                            INBOX_PATH.write_text("") 
                            for url in found_urls:
                                await self.queue.put(url)
                            self.update_ui()
                except Exception: pass 
            await asyncio.sleep(POLL_RATE)

    async def download_worker(self):
        while True:
            url = await self.queue.get()
            self.active += 1
            self.update_ui()
            
            try:
                data = None
                for attempt in range(3): 
                    try:
                        resp = await asyncio.to_thread(self.session.get, TIKWM_API, params={"url": url, "hd": "1"}, timeout=12)
                        data = resp.json()
                        if data.get("code") == 0: break
                        if data.get("code") == -1: await asyncio.sleep(2) 
                    except: await asyncio.sleep(1)

                if not data or data.get("code") != 0:
                    self.failed += 1
                else:
                    clip = data["data"]
                    video_id = clip.get("id", "unknown")
                    
                    if video_id in self.history:
                        self.done += 1 
                    else:
                        final_path = EXPORT_DIR / f"{video_id}.mp4"
                        with self.session.get(clip["play"], stream=True) as r:
                            with open(final_path, "wb") as f:
                                shutil.copyfileobj(r.raw, f)
                        
                        subprocess.run(["termux-media-scan", str(final_path)], capture_output=True)
                        self.add_to_history(video_id)
                        self.done += 1
                        # Notify the user
                        self.send_notification("Scoopz Downloaded", f"Saved {video_id} to Gallery")

            except Exception:
                self.failed += 1
            finally:
                self.active -= 1
                self.queue.task_done()
                self.update_ui()

    async def run(self):
        self.send_notification("Scoopz Active", "Watching for TikTok links...")
        workers = [asyncio.create_task(self.download_worker()) for _ in range(MAX_WORKERS)]
        watcher = asyncio.create_task(self.watch_inbox())
        await asyncio.gather(watcher, *workers)

if __name__ == "__main__":
    try:
        asyncio.run(ScoopzHybrid().run())
    except KeyboardInterrupt:
        print("\n\n👋 Stopped.")