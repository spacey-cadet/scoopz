#!/usr/bin/env python3
"""
Scoopz Collector (Simple)
Downloader-first pipeline: ingest links, download mp4s, save optional API thumbnails.
"""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import requests

# Paths (overridable for Termux / multi-device consistency)
SCOOPZ_DIR = Path(os.environ.get("SCOOPZ_DIR", "/sdcard/Pictures/scoopz"))
DOWNLOAD_DIR = Path(os.environ.get("SCOOPZ_DOWNLOAD_DIR", "/sdcard/Download"))
INBOX_PATH = Path(os.environ.get("SCOOPZ_INBOX_PATH", str(DOWNLOAD_DIR / "links_inbox.txt")))
EXPORT_DIR = SCOOPZ_DIR
THUMB_DIR = Path(os.environ.get("SCOOPZ_THUMB_DIR", str(SCOOPZ_DIR / ".thumbs")))
HISTORY_FILE = Path(os.environ.get("SCOOPZ_HISTORY_FILE", str(DOWNLOAD_DIR / "scoopz_history.log")))
LOG_FILE = Path(os.environ.get("SCOOPZ_LOG_FILE", str(DOWNLOAD_DIR / "scoopz_errors.log")))

# Network / worker settings
TIKWM_API = "https://www.tikwm.com/api/"
TIKTOK_URL_PATTERN = re.compile(r"https?://(?:(?:www|vt|vm)\.)?tiktok\.com/\S+", re.IGNORECASE)
MAX_WORKERS = 5
POLL_INTERVAL = 0.5
API_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 45
API_RETRIES = 3
DOWNLOAD_RETRIES = 3
RETRY_BACKOFF = 2.0

# Optional behavior
USE_ANSI_STATUS = sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"
SAVE_API_THUMBNAILS = os.environ.get("SCOOPZ_SAVE_API_THUMBNAILS", "1") == "1"
THUMB_MAX_FILES = int(os.environ.get("SCOOPZ_THUMB_MAX_FILES", "500"))
THUMB_MAX_MB = int(os.environ.get("SCOOPZ_THUMB_MAX_MB", "120"))
THUMB_MAX_AGE_DAYS = int(os.environ.get("SCOOPZ_THUMB_MAX_AGE_DAYS", "30"))
THUMB_PRUNE_INTERVAL_SEC = int(os.environ.get("SCOOPZ_THUMB_PRUNE_INTERVAL_SEC", "300"))


@dataclass
class VideoMetadata:
    video_id: str
    author: str
    title: str
    play_url: str
    hd_play_url: Optional[str] = None
    thumb_url: Optional[str] = None

    @classmethod
    def from_api_response(cls, data: Dict) -> Optional["VideoMetadata"]:
        try:
            video_id = str(data.get("id", "")).strip()
            play_url = (data.get("play") or "").strip()
            if not video_id or not play_url:
                return None

            author_data = data.get("author", {})
            if isinstance(author_data, dict):
                author = (
                    author_data.get("unique_id")
                    or author_data.get("nickname")
                    or author_data.get("id")
                    or "unknown"
                )
            else:
                author = str(author_data) if author_data else "unknown"

            title = data.get("title") or data.get("desc") or data.get("region") or ""
            hd_play_url = data.get("hdplay")
            thumb_url = data.get("cover") or data.get("ai_dynamic_cover") or data.get("origin_cover")

            return cls(
                video_id=video_id,
                author=str(author),
                title=str(title),
                play_url=play_url,
                hd_play_url=hd_play_url,
                thumb_url=thumb_url,
            )
        except Exception:
            return None


@dataclass
class DownloadStats:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    active: int = 0
    queued: int = 0


class TikWMClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

    def get_video_info(self, url: str) -> Optional[VideoMetadata]:
        for attempt in range(API_RETRIES):
            try:
                resp = self.session.post(TIKWM_API, data={"url": url, "hd": 1}, timeout=API_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != 0:
                    if attempt < API_RETRIES - 1 and data.get("code") == -1:
                        time.sleep(RETRY_BACKOFF ** attempt)
                        continue
                    return None
                return VideoMetadata.from_api_response(data.get("data", {}))
            except Exception:
                if attempt < API_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF ** attempt)
                    continue
                return None
        return None


class VideoDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
            }
        )

    def download(self, metadata: VideoMetadata, output_path: Path) -> Tuple[bool, str]:
        url = metadata.hd_play_url or metadata.play_url
        temp_path = output_path.with_suffix(".mp4.part")

        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass

        for attempt in range(DOWNLOAD_RETRIES):
            try:
                ok, reason = self._download_file(url, temp_path)
                if not ok:
                    if attempt < DOWNLOAD_RETRIES - 1:
                        time.sleep(RETRY_BACKOFF ** attempt)
                        continue
                    return False, reason

                temp_path.rename(output_path)
                return True, ""
            except Exception as e:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except Exception:
                    pass
                if attempt < DOWNLOAD_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF ** attempt)
                    continue
                return False, f"Download error: {type(e).__name__}"

        return False, "Download failed after retries"

    def _download_file(self, url: str, output_path: Path) -> Tuple[bool, str]:
        try:
            with self.session.get(
                url,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
                headers={"Referer": "https://www.tiktok.com/"},
            ) as resp:
                resp.raise_for_status()
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if "text/html" in content_type or "application/json" in content_type:
                    return False, f"Invalid content-type: {content_type}"

                with open(output_path, "wb") as f:
                    shutil.copyfileobj(resp.raw, f)

                return True, ""
        except requests.exceptions.Timeout:
            return False, "Download timeout"
        except requests.exceptions.HTTPError as e:
            code = getattr(e.response, "status_code", "?")
            return False, f"HTTP {code}"
        except Exception as e:
            return False, f"{type(e).__name__}"


class ScoopzCollector:
    def __init__(self):
        self.api_client = TikWMClient()
        self.downloader = VideoDownloader()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.stats = DownloadStats()
        self.history: Set[str] = set()
        self._last_status_len = 0
        self._last_thumb_prune_ts = 0.0

        self._ensure_directories()
        self._load_history()

    def _ensure_directories(self):
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        if not HISTORY_FILE.exists():
            HISTORY_FILE.touch()
        if not LOG_FILE.exists():
            LOG_FILE.touch()

    def _load_history(self):
        try:
            self.history = set(line.strip() for line in HISTORY_FILE.read_text().splitlines() if line.strip())
        except Exception:
            self.history = set()

    def _add_to_history(self, video_id: str):
        if video_id in self.history:
            return
        self.history.add(video_id)
        try:
            with open(HISTORY_FILE, "a") as f:
                f.write(f"{video_id}\n")
        except Exception:
            pass

    def _log_error(self, video_id: str, reason: str):
        try:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG_FILE, "a") as f:
                f.write(f"[{timestamp}] {video_id}: {reason}\n")
        except Exception:
            pass

        if USE_ANSI_STATUS:
            sys.stdout.write("\r\033[K")
        else:
            sys.stdout.write("\r" + (" " * self._last_status_len) + "\r")
        print(f"[fail] {video_id}: {reason}", flush=True)
        self._update_ui()

    def _update_ui(self):
        self.stats.queued = self.queue.qsize()
        status = (
            f"A:{self.stats.active} "
            f"D:{self.stats.downloaded} "
            f"S:{self.stats.skipped} "
            f"F:{self.stats.failed} "
            f"Q:{self.stats.queued}"
        )

        if USE_ANSI_STATUS:
            sys.stdout.write(f"\r\033[K{status}")
        else:
            pad = max(0, self._last_status_len - len(status))
            sys.stdout.write(f"\r{status}{' ' * pad}")
        self._last_status_len = len(status)
        sys.stdout.flush()

    def _safe_filename(self, text: str, max_len: int = 48) -> str:
        text = (text or "").strip().lower()
        text = re.sub(r"\s+", "_", text)
        text = re.sub(r"[^a-z0-9_\-]", "", text)
        text = re.sub(r"_+", "_", text).strip("_")
        return text[:max_len]

    def _build_filename(self, metadata: VideoMetadata) -> str:
        author = self._safe_filename(metadata.author, 20)
        title = self._safe_filename(metadata.title, 40)
        vid = self._safe_filename(metadata.video_id, 24)
        parts = [p for p in [author, title, vid] if p]
        return "__".join(parts) + ".mp4"

    def _sidecar_thumb_path(self, video_path: Path) -> Path:
        return THUMB_DIR / f"{video_path.name}.jpg"

    def _fetch_sidecar_thumbnail(self, metadata: VideoMetadata, video_path: Path):
        if not SAVE_API_THUMBNAILS:
            return
        thumb_url = (metadata.thumb_url or "").strip()
        if not thumb_url:
            return

        thumb_path = self._sidecar_thumb_path(video_path)
        try:
            with requests.get(thumb_url, stream=True, timeout=12) as resp:
                resp.raise_for_status()
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if ctype and "image" not in ctype:
                    return

                tmp = thumb_path.with_suffix(".jpg.part")
                with open(tmp, "wb") as f:
                    shutil.copyfileobj(resp.raw, f)
                if tmp.exists() and tmp.stat().st_size > 500:
                    tmp.replace(thumb_path)
                else:
                    tmp.unlink(missing_ok=True)
        except Exception:
            pass

    def _prune_thumb_cache(self):
        try:
            now = time.time()
            if (now - self._last_thumb_prune_ts) < THUMB_PRUNE_INTERVAL_SEC:
                return
            self._last_thumb_prune_ts = now

            if not THUMB_DIR.exists():
                return

            entries = [p for p in THUMB_DIR.iterdir() if p.is_file()]
            if not entries:
                return

            max_age_sec = max(0, THUMB_MAX_AGE_DAYS) * 86400
            for p in list(entries):
                try:
                    source = EXPORT_DIR / p.name.removesuffix(".jpg")
                    too_old = max_age_sec > 0 and (now - p.stat().st_mtime) > max_age_sec
                    if (not source.exists()) or too_old:
                        p.unlink(missing_ok=True)
                except Exception:
                    pass

            entries = [p for p in THUMB_DIR.iterdir() if p.is_file()]
            entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            max_bytes = max(1, THUMB_MAX_MB) * 1024 * 1024
            total = 0
            for idx, p in enumerate(entries):
                try:
                    size = p.stat().st_size
                except Exception:
                    continue
                if idx >= max(1, THUMB_MAX_FILES) or (total + size) > max_bytes:
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
                else:
                    total += size
        except Exception:
            pass

    async def _watch_inbox(self):
        while True:
            try:
                if INBOX_PATH.exists() and INBOX_PATH.stat().st_size > 0:
                    content = INBOX_PATH.read_text().strip()
                    if content:
                        urls = []
                        for line in content.splitlines():
                            m = TIKTOK_URL_PATTERN.search(line)
                            if m:
                                urls.append(m.group(0).strip())

                        if urls:
                            INBOX_PATH.write_text("")
                            for url in urls:
                                await self.queue.put(url)
                            self._update_ui()
            except Exception:
                pass

            await asyncio.sleep(POLL_INTERVAL)

    async def _download_worker(self):
        while True:
            url = await self.queue.get()
            try:
                self.stats.active += 1
                self._update_ui()

                metadata = await asyncio.to_thread(self.api_client.get_video_info, url)
                if not metadata:
                    self.stats.failed += 1
                    self._log_error("unknown", "API returned no metadata")
                    continue

                if metadata.video_id in self.history:
                    self.stats.skipped += 1
                    continue

                filename = self._build_filename(metadata)
                output_path = EXPORT_DIR / filename
                success, reason = await asyncio.to_thread(self.downloader.download, metadata, output_path)

                if success:
                    try:
                        subprocess.run(
                            ["termux-media-scan", str(output_path)],
                            capture_output=True,
                            timeout=5,
                            check=False,
                        )
                    except Exception:
                        pass

                    await asyncio.to_thread(self._fetch_sidecar_thumbnail, metadata, output_path)
                    await asyncio.to_thread(self._prune_thumb_cache)

                    self._add_to_history(metadata.video_id)
                    self.stats.downloaded += 1
                else:
                    self.stats.failed += 1
                    self._log_error(metadata.video_id, reason)
            except Exception as e:
                self.stats.failed += 1
                self._log_error("unknown", f"Worker error: {type(e).__name__}")
            finally:
                self.stats.active -= 1
                self.queue.task_done()
                self._update_ui()

    async def run(self):
        print("=" * 70)
        print("Scoopz Collector (Simple)")
        print("=" * 70)
        print(f"Export directory: {EXPORT_DIR}")
        print(f"Workers: {MAX_WORKERS}")
        print(f"Save API thumbnails: {SAVE_API_THUMBNAILS}")
        print("Mode: download-only (no validation, no transcoding)")
        print("=" * 70)
        print("\nWatching inbox: Share TikTok -> Copy Link\n")

        workers = [asyncio.create_task(self._download_worker()) for _ in range(MAX_WORKERS)]
        watcher = asyncio.create_task(self._watch_inbox())
        await asyncio.gather(watcher, *workers)


def main():
    try:
        collector = ScoopzCollector()
        asyncio.run(collector.run())
    except KeyboardInterrupt:
        print("\n\nStopped.\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
