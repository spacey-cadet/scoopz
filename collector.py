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

# Validation settings
MIN_BYTES = int(os.environ.get("SCOOPZ_MIN_BYTES", "100000"))  # Lowered to 100KB (was 250KB)
DOWNLOAD_RETRIES = int(os.environ.get("SCOOPZ_DOWNLOAD_RETRIES", "2"))

# CRITICAL FIX: Make thumbnail validation optional (it's too strict)
VALIDATE_THUMBNAIL = os.environ.get("SCOOPZ_VALIDATE_THUMBNAIL", "0") == "1"

VERBOSE_EVENT_LOGS = os.environ.get("SCOOPZ_VERBOSE", "0") == "1"


class ScoopzHybrid:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        self.downloaded = 0
        self.skipped = 0
        self.failed = 0
        self.active = 0
        self.history = set(HISTORY_FILE.read_text().splitlines())
    
    def log_fail(self, reason: str):
        if VERBOSE_EVENT_LOGS:
            print(f"\n[fail] {reason}", flush=True)
    
    def update_ui(self):
        queued = self.queue.qsize()
        sys.stdout.write(
            f"\r\033[K ⬇️  Active: {self.active} | "
            f"✅ Downloaded: {self.downloaded} | "
            f"⏭️ Skipped: {self.skipped} | "
            f"❌ Fail: {self.failed} | "
            f"🕒 Queued: {queued}"
        )
        sys.stdout.flush()
    
    def send_notification(self, title, msg):
        try:
            subprocess.run([
                "termux-notification", 
                "-t", title, 
                "-c", msg, 
                "--id", "scoopz_notif",
                "--priority", "low"
            ], capture_output=True, timeout=3, check=False)
        except:
            pass
    
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
                            INBOX_PATH.write_text("")
                            for url in found_urls:
                                await self.queue.put(url)
                            self.update_ui()
                except Exception as e:
                    self.log_fail(f"inbox read: {type(e).__name__}")
            
            await asyncio.sleep(POLL_RATE)
    
    def _sync_download(self, play_url, final_path):
        """Runs blocking requests operations safely in a thread."""
        temp_path = final_path.with_suffix('.mp4.part')
        
        def _can_decode_video(path: Path) -> tuple[bool, str]:
            """
            FIXED: Check if ffmpeg can decode the video.
            This is the ONLY validation that matters for Scoopz.
            """
            try:
                if not path.exists():
                    return False, "temp file missing"
                
                size = path.stat().st_size
                if size < MIN_BYTES:
                    return False, f"file too small ({size} bytes < {MIN_BYTES})"
                
            except Exception as e:
                return False, f"stat error: {type(e).__name__}"
            
            # Test if ffmpeg can decode at least 1 frame
            try:
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v", "error",
                        "-i", str(path),
                        "-map", "0:v:0",
                        "-frames:v", "1",
                        "-f", "null",
                        "-"
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False
                )
                
                if result.returncode != 0:
                    # FIXED: Return actual ffmpeg error, not generic message
                    stderr = (result.stderr or "").strip()
                    return False, f"decode failed: {stderr[:100] if stderr else 'unknown error'}"
                
                # Optional: Check thumbnail extraction (disabled by default)
                if VALIDATE_THUMBNAIL:
                    thumb_ok = self._can_extract_thumbnail(path)
                    if not thumb_ok:
                        # Don't fail - just warn
                        print(f"\n⚠️  Warning: Thumbnail extraction failed for {path.name}", flush=True)
                
                return True, ""
            
            except FileNotFoundError:
                # ffmpeg not installed - accept file based on size only
                return True, ""
            except subprocess.TimeoutExpired:
                return False, "ffmpeg timeout"
            except Exception as e:
                return False, f"validation error: {type(e).__name__}"
        
        def _can_extract_thumbnail(path: Path) -> bool:
            """Test if thumbnail can be extracted (optional check)."""
            for seek in ["1", "2", "0.5"]:
                try:
                    result = subprocess.run(
                        [
                            "ffmpeg",
                            "-nostdin",
                            "-v", "error",
                            "-ss", seek,
                            "-i", str(path),
                            "-vframes", "1",
                            "-f", "null",
                            "-"
                        ],
                        capture_output=True,
                        timeout=10,
                        check=False
                    )
                    if result.returncode == 0:
                        return True
                except:
                    pass
            return False
        
        # Download with retries
        retries = max(1, DOWNLOAD_RETRIES)
        last_error = ""
        
        for attempt in range(retries):
            try:
                # Download file
                with self.session.get(
                    play_url,
                    stream=True,
                    timeout=30,
                    headers={
                        "Referer": "https://www.tiktok.com/",
                        "Accept": "video/*,*/*;q=0.8",
                    }
                ) as r:
                    r.raise_for_status()
                    
                    # Check content type
                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if "text/html" in ctype or "application/json" in ctype:
                        last_error = f"invalid content-type: {ctype}"
                        if attempt < retries - 1:
                            time.sleep(2)
                            continue
                        return False, last_error
                    
                    # Download to temp file
                    with open(temp_path, "wb") as f:
                        shutil.copyfileobj(r.raw, f)
                
                # Validate downloaded file
                is_valid, reason = _can_decode_video(temp_path)
                
                if not is_valid:
                    last_error = reason
                    
                    # Clean up bad file
                    try:
                        temp_path.unlink()
                    except:
                        pass
                    
                    # Retry on validation failure
                    if attempt < retries - 1:
                        time.sleep(2)
                        continue
                    
                    return False, last_error
                
                # Success - rename to final path
                temp_path.rename(final_path)
                return True, ""
            
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)[:100]}"
                
                # Clean up partial file
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except:
                    pass
                
                # Retry on exception
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
        
        return False, last_error
    
    async def download_worker(self):
        while True:
            url = await self.queue.get()
            self.active += 1
            self.update_ui()
            
            try:
                data = None
                last_api_err = ""
                
                # API retry loop
                for attempt in range(3):
                    try:
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
                    self.log_fail(last_api_err or "api returned no data")
                    continue
                
                clip = data.get("data", {})
                video_id = clip.get("id", "unknown")
                
                # Check history
                if video_id in self.history:
                    self.skipped += 1
                    continue
                
                # Get video URL
                play_url = clip.get("hdplay") or clip.get("play")
                if not play_url:
                    self.failed += 1
                    self.log_fail(f"no play url (id={video_id})")
                    continue
                
                # Build filename
                file_base = self._build_video_basename(clip, video_id)
                final_path = EXPORT_DIR / f"{file_base}.mp4"
                
                # Download
                success, reason = await asyncio.to_thread(
                    self._sync_download, 
                    play_url, 
                    final_path
                )
                
                if success:
                    # Add to media library
                    try:
                        subprocess.run(
                            ["termux-media-scan", str(final_path)], 
                            capture_output=True,
                            timeout=5,
                            check=False
                        )
                    except:
                        pass
                    
                    self.add_to_history(video_id)
                    self.downloaded += 1
                    self.send_notification(
                        "Scoopz Downloaded", 
                        f"Saved {final_path.name[:40]}"
                    )
                else:
                    self.failed += 1
                    self.log_fail(f"{video_id}: {reason}")
            
            except Exception as e:
                self.failed += 1
                self.log_fail(f"worker crash: {type(e).__name__}: {str(e)[:100]}")
            
            finally:
                self.active -= 1
                self.queue.task_done()
                self.update_ui()
    
    async def run(self):
        self.send_notification("Scoopz Active", "Watching for TikTok links...")
        print("📋 Starting collector — browse TikTok and Share → Copy Link\n")
        
        workers = [
            asyncio.create_task(self.download_worker()) 
            for _ in range(MAX_WORKERS)
        ]
        watcher = asyncio.create_task(self.watch_inbox())
        
        await asyncio.gather(watcher, *workers)


if __name__ == "__main__":
    try:
        asyncio.run(ScoopzHybrid().run())
    except KeyboardInterrupt:
        print("\n\n👋 Stopped.")