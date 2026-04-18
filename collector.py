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

# Extra safety: always reject videos that cannot yield a thumbnail frame.
VALIDATE_THUMBNAIL = True

class ScoopzHybrid:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.session = requests.Session()
        # Disguise the script slightly to prevent API blocking
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.active = 0
        self.history = set(HISTORY_FILE.read_text().splitlines())

    def log_fail(self, reason: str):
        # Keep these logs short so they stay readable on mobile terminals.
        print(f"\n[fail] {reason}", flush=True)
    
    def update_ui(self):
        queued = self.queue.qsize()
        sys.stdout.write(
            f"\r\033[K ⬇️  Active: {self.active} | ✅ Downloaded: {self.downloaded} | ⏭️ Skipped: {self.skipped} | ❌ Fail: {self.failed} | 🕒 Queued: {queued}"
        )
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

    def _safe_slug(self, text: str, max_len: int = 48) -> str:
        text = (text or "").strip().lower()
        text = re.sub(r"\s+", "_", text)
        text = re.sub(r"[^a-z0-9_\-]", "", text)
        text = re.sub(r"_+", "_", text).strip("_")
        return text[:max_len]

    def _build_video_basename(self, clip: dict, video_id: str) -> str:
        """Create readable but stable filename with id suffix for uniqueness."""
        author = ""
        raw_author = clip.get("author")
        if isinstance(raw_author, dict):
            author = (
                raw_author.get("unique_id")
                or raw_author.get("nickname")
                or raw_author.get("id")
                or ""
            )
        elif isinstance(raw_author, str):
            author = raw_author

        title = (
            clip.get("title")
            or clip.get("desc")
            or clip.get("region")
            or ""
        )

        author_slug = self._safe_slug(author, max_len=20)
        title_slug = self._safe_slug(title, max_len=40)
        id_suffix = self._safe_slug(video_id, max_len=24) or "vid"

        parts = []
        if author_slug:
            parts.append(author_slug)
        if title_slug:
            parts.append(title_slug)
        parts.append(id_suffix)

        return "__".join(parts)
    
    async def watch_inbox(self):
        while True:
            if INBOX_PATH.exists() and INBOX_PATH.stat().st_size > 0:
                try:
                    raw_content = INBOX_PATH.read_text().strip()
                    if raw_content:
                        found_urls = []
                        for line in raw_content.splitlines():
                            m = TIKTOK_RE.search(line)
                            if m:
                                found_urls.append(m.group(0).strip())
                        if found_urls:
                            INBOX_PATH.write_text("") # Clear inbox immediately
                            for url in found_urls:
                                await self.queue.put(url)
                            self.update_ui()
                except Exception as e:
                    self.log_fail(f"inbox read: {type(e).__name__}")
            await asyncio.sleep(POLL_RATE)

    def _sync_download(self, play_url, final_path):
        """Runs blocking requests operations safely in a thread"""
        temp_path = final_path.with_suffix('.mp4.part')
        repaired_path = final_path.with_suffix('.mp4.repaired.part')

        def _repair_container_for_thumbnail(src: Path, dst: Path) -> bool:
            # Some phone/ffmpeg builds fail thumbs on fragmented mp4 containers.
            # Fast remux often fixes seekability without re-encoding.
            try:
                if dst.exists():
                    dst.unlink(missing_ok=True)
                r = subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(src),
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        str(dst),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                return r.returncode == 0 and dst.exists() and dst.stat().st_size > 0
            except FileNotFoundError:
                return False
            except Exception:
                return False

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

        def _looks_valid_mp4(path: Path) -> tuple[bool, str]:
            try:
                if not path.exists():
                    return False, "missing temp file"
                if MIN_BYTES > 0 and path.stat().st_size < MIN_BYTES:
                    return False, "file too small"
            except Exception:
                return False, "temp file stat failed"

            if VALIDATE_THUMBNAIL and not _can_extract_thumbnail(path):
                return False, "no extractable thumbnail"

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
                if r.returncode != 0:
                    return False, "ffmpeg decode check failed"
                return True, ""
            except FileNotFoundError:
                # ffmpeg not installed: fall back to size-only.
                return True, ""
            except Exception:
                return False, "ffmpeg validation error"
        
        retries = max(1, DOWNLOAD_RETRIES)

        # Retry loop for the actual video file
        for attempt in range(retries):
            try:
                with self.session.get(
                    play_url,
                    stream=True,
                    timeout=30,
                    headers={
                        "Referer": "https://www.tiktok.com/",
                        "Accept": "video/*,*/*;q=0.8",
                    },
                ) as r:
                    r.raise_for_status()

                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if ctype and ("text/html" in ctype or "application/json" in ctype):
                        return False, f"cdn non-video content-type: {ctype}"

                    with open(temp_path, "wb") as f:
                        shutil.copyfileobj(r.raw, f)

                # Validate before saving into the gallery folder.
                is_valid, reason = _looks_valid_mp4(temp_path)
                if not is_valid:
                    # One repair pass before giving up: helps with container quirks across devices.
                    if reason == "no extractable thumbnail" and _repair_container_for_thumbnail(temp_path, repaired_path):
                        is_valid, reason = _looks_valid_mp4(repaired_path)
                        if is_valid:
                            try:
                                temp_path.unlink(missing_ok=True)
                            except Exception:
                                pass
                            repaired_path.rename(temp_path)

                    if not is_valid:
                        try:
                            temp_path.unlink(missing_ok=True)
                            repaired_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        if attempt < retries - 1:
                            time.sleep(2)
                        continue

                    try:
                        repaired_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                
                # Success! Rename to final mp4
                temp_path.rename(final_path)
                return True, ""
                
            except Exception as e:
                # Clean up corrupted partial file if it failed
                if temp_path.exists():
                    temp_path.unlink()
                if repaired_path.exists():
                    repaired_path.unlink()
                
                if attempt < retries - 1:
                    time.sleep(2) # Wait before retry
        return False, reason if 'reason' in locals() and reason else f"download error: {type(e).__name__}" if 'e' in locals() else "download failed"

    async def download_worker(self):
        while True:
            url = await self.queue.get()
            self.active += 1
            self.update_ui()
            
            try:
                data = None
                last_api_err = ""
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
                        resp.raise_for_status()
                        data = resp.json()
                        if data.get("code") == 0:
                            break
                        last_api_err = f"api code {data.get('code')}: {data.get('msg', 'no message')}"
                        if data.get("code") == -1:
                            await asyncio.sleep(2)
                    except Exception as e:
                        last_api_err = f"api request: {type(e).__name__}"
                        await asyncio.sleep(2)
                
                if not data or data.get("code") != 0:
                    self.failed += 1
                    self.log_fail(last_api_err or "api returned no usable data")
                    continue
                
                clip = data.get("data", {})
                video_id = clip.get("id", "unknown")
                
                # Check history early to save bandwidth
                if video_id in self.history:
                    self.skipped += 1
                    continue
                
                # Prioritize HD, fallback to standard, fail gracefully if it's a photo post
                play_url = clip.get("hdplay") or clip.get("play")
                if not play_url:
                    self.failed += 1 # Likely an image carousel, not a video
                    self.log_fail(f"no play url (id={video_id})")
                    continue
                
                file_base = self._build_video_basename(clip, video_id)
                final_path = EXPORT_DIR / f"{file_base}.mp4"
                
                # Offload the heavy file downloading to a background thread
                success, reason = await asyncio.to_thread(self._sync_download, play_url, final_path)
                
                if success:
                    subprocess.run(["termux-media-scan", str(final_path)], capture_output=True)
                    self.add_to_history(video_id)
                    self.downloaded += 1
                    self.send_notification("Scoopz Downloaded", f"Saved {video_id} to Gallery")
                else:
                    self.failed += 1
                    self.log_fail(f"{video_id}: {reason}")

            except Exception as e:
                self.failed += 1
                self.log_fail(f"worker crash: {type(e).__name__}")
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