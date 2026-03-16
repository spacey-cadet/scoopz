"""TikTok collector — clipboard → inbox buffer → parallel downloads."""
from __future__ import annotations
import asyncio
import json
import logging
import re
import signal
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Deque, List
from logging.handlers import RotatingFileHandler
import requests

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).resolve().parent
DATA_DIR     = BASE_DIR / "data"
LOG_DIR      = BASE_DIR / "logs"
DOWNLOAD_DIR = BASE_DIR / "downloads"
SHARED_EXPORT_DIR = Path.home() / "storage" / "shared" / "scoopz_downloads"

for path in (DATA_DIR, LOG_DIR, DOWNLOAD_DIR):
    path.mkdir(parents=True, exist_ok=True)

try:
    SHARED_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
except PermissionError:
    pass

INBOX_PATH        = DATA_DIR / "links_inbox.txt"   # clipboard buffer
URL_LOG_PATH      = DATA_DIR / "urls.txt"           # permanent record
QUEUE_PATH        = DATA_DIR / "queue.json"         # download queue
COLLECTOR_LOG_PATH = LOG_DIR / "collector.log"

INBOX_PATH.touch(exist_ok=True)
URL_LOG_PATH.touch(exist_ok=True)

# ── Config ────────────────────────────────────────────────────────────────────
CLIPBOARD_POLL_INTERVAL = 1.0    # seconds between clipboard checks
INBOX_POLL_INTERVAL     = 2.0    # seconds between inbox reads
MAX_DOWNLOAD_WORKERS    = 5      # parallel downloads
DOWNLOAD_BATCH_SIZE     = 5
MAX_DOWNLOAD_RETRIES    = 3
TIKWM_API               = "https://tikwm.com/api/"
CAPTION_TAGS            = "#viral #trending #funny #interesting #scoopz"
CAPTION_FALLBACK        = "What's your take on this?"

TIKTOK_REGEX = re.compile(
    r"https?://(?:(?:www|vt|vm)\.)?tiktok\.com/\S+", re.IGNORECASE
)

# ── Logging ───────────────────────────────────────────────────────────────────
def setup_logger(name: str, log_path: Path) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger

log = setup_logger("collector", COLLECTOR_LOG_PATH)

def log_exception(message: str, exc: BaseException | None = None) -> None:
    log.error("%s | %s", message, exc, exc_info=True)


def build_caption_text(metadata: dict) -> str:
    """Compose caption from tikwm metadata plus default tags."""
    title = metadata.get("title") or metadata.get("desc") or ""
    title = " ".join(title.split()).strip()
    if not title:
        title = CAPTION_FALLBACK
    return f"{title}\n\n{CAPTION_TAGS}"


def write_caption_file(video_path: Path, caption: str) -> None:
    caption_path = video_path.with_suffix(".txt")
    try:
        caption_path.write_text(caption, encoding="utf-8")
        log.info("Caption ready: %s", caption_path.name)
    except OSError as exc:
        log.warning("Could not save caption for %s: %s", video_path.name, exc)


def copy_to_shared(video_path: Path) -> None:
    """Mirror the downloaded file into shared storage for gallery apps."""
    if not video_path.exists():
        return
    try:
        destination = SHARED_EXPORT_DIR / video_path.name
        shutil.copy2(video_path, destination)
    except PermissionError:
        log.warning("Shared storage unavailable: %s", SHARED_EXPORT_DIR)
        return
    try:
        subprocess.run(["termux-media-scan", str(destination)], check=False, timeout=10)
    except FileNotFoundError:
        pass
    log.info("Copied to shared storage: %s", destination)

# ── Persistent queue ──────────────────────────────────────────────────────────
class PersistentQueue:
    """Async-safe queue that survives crashes."""

    def __init__(self) -> None:
        self.lock    = asyncio.Lock()
        self._queue: Deque[str] = deque()
        self._seen:  set[str]   = set()
        self._load()

    def _load(self) -> None:
        if not QUEUE_PATH.exists():
            QUEUE_PATH.write_text("[]", encoding="utf-8")
        try:
            for url in json.loads(QUEUE_PATH.read_text(encoding="utf-8")):
                if isinstance(url, str):
                    self._queue.append(url)
                    self._seen.add(url)
        except json.JSONDecodeError:
            self._queue.clear()
            self._seen.clear()

    def _persist(self) -> None:
        QUEUE_PATH.write_text(
            json.dumps(list(self._queue), indent=2), encoding="utf-8"
        )

    async def add(self, url: str) -> bool:
        async with self.lock:
            if url in self._seen:
                return False
            self._queue.append(url)
            self._seen.add(url)
            self._persist()
            return True

    async def add_many(self, urls: List[str]) -> int:
        added = 0
        async with self.lock:
            for url in urls:
                if url not in self._seen:
                    self._queue.append(url)
                    self._seen.add(url)
                    added += 1
            if added:
                self._persist()
        return added

    async def pop_batch(self, size: int) -> List[str]:
        async with self.lock:
            items: List[str] = []
            for _ in range(min(size, len(self._queue))):
                url = self._queue.popleft()
                self._seen.discard(url)
                items.append(url)
            if items:
                self._persist()
            return items

    async def size(self) -> int:
        async with self.lock:
            return len(self._queue)

# ── Clipboard monitor → writes to inbox ──────────────────────────────────────
class ClipboardMonitor:
    """Poll the clipboard and push URLs straight into the queue."""

    def __init__(self, queue: PersistentQueue) -> None:
        self.queue = queue
        self._last: str | None = None
        self._running = False
        self._known: set[str] = set(
            line.strip()
            for line in URL_LOG_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    async def start(self) -> None:
        self._running = True
        print("[Collector] Clipboard monitor running — copy TikTok links freely")
        while self._running:
            await self._poll()
            await asyncio.sleep(CLIPBOARD_POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False

    async def _poll(self) -> None:
        try:
            text = await asyncio.get_running_loop().run_in_executor(
                None, self._read_clipboard
            )
        except Exception:
            return

        if not text or text == self._last:
            return
        self._last = text

        match = TIKTOK_REGEX.search(text.strip())
        if not match:
            return

        url = match.group(0)
        if url in self._known:
            return

        # Write to inbox buffer
        self._known.add(url)
        with INBOX_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{url}\n")

        # Also append to permanent log
        with URL_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{url}\n")

        print(f"[Clipboard] Captured: {url}")
        log.info("Captured to inbox: %s", url)
        asyncio.create_task(self._enqueue(url))

    async def _enqueue(self, url: str) -> None:
        added = await self.queue.add(url)
        if added:
            print(f"[Queue] Added: {url}")

    @staticmethod
    def _read_clipboard() -> str:
        return subprocess.check_output(
            ["termux-clipboard-get"], text=True
        ).strip()

# ── Inbox watcher → feeds queue ───────────────────────────────────────────────
class InboxWatcher:
    """
    Polls links_inbox.txt every 2 seconds.
    Drains it into the PersistentQueue so downloads start immediately.
    Clears the file after reading — acts as a consume-once buffer.
    """

    def __init__(self, queue: PersistentQueue) -> None:
        self.queue    = queue
        self._running = False

    async def start(self) -> None:
        self._running = True
        while self._running:
            await self._drain_inbox()
            await asyncio.sleep(INBOX_POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False

    async def _drain_inbox(self) -> None:
        if not INBOX_PATH.exists() or INBOX_PATH.stat().st_size == 0:
            return

        # Read and clear atomically
        try:
            content = INBOX_PATH.read_text(encoding="utf-8")
            INBOX_PATH.write_text("", encoding="utf-8")  # clear immediately
        except Exception as e:
            log_exception("Failed to read inbox", e)
            return

        urls = [
            line.strip() for line in content.splitlines()
            if line.strip() and TIKTOK_REGEX.search(line.strip())
        ]

        if not urls:
            return

        added = await self.queue.add_many(urls)
        if added:
            log.info("Inbox → queue: %d new URL(s)", added)
            print(f"[Inbox] Queued {added} URL(s) for download")

# ── Download result ───────────────────────────────────────────────────────────
@dataclass
class DownloadResult:
    url: str
    success: bool
    error: str | None = None

# ── Downloader ────────────────────────────────────────────────────────────────
class Downloader:
    """
    Continuously drains the queue and downloads videos in parallel.
    Uses tikwm.com API — no yt-dlp, no impersonation needed.
    MAX_DOWNLOAD_WORKERS videos download simultaneously.
    """

    def __init__(self, queue: PersistentQueue) -> None:
        self.queue = queue
        self.sem   = asyncio.Semaphore(MAX_DOWNLOAD_WORKERS)

    async def run_forever(self) -> None:
        log.info("Downloader started — %d parallel workers", MAX_DOWNLOAD_WORKERS)
        active: set[asyncio.Task[DownloadResult]] = set()
        while True:
            while len(active) < MAX_DOWNLOAD_WORKERS:
                batch = await self.queue.pop_batch(1)
                if not batch:
                    break
                url = batch[0]
                task = asyncio.create_task(self._download_with_semaphore(url))
                active.add(task)

            if not active:
                await asyncio.sleep(0.5)
                continue

            done, active = await asyncio.wait(
                active, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                result = await task
                if result.success:
                    log.info("✅ Downloaded: %s", result.url)
                    print(f"  ✅ Downloaded: {result.url}")
                else:
                    log.error("❌ Failed: %s | %s", result.url, result.error)
                    print(f"  ❌ Failed: {result.url} — {result.error}")

    async def _download_with_semaphore(self, url: str) -> DownloadResult:
        async with self.sem:
            return await self._download_with_retries(url)

    async def _download_with_retries(self, url: str) -> DownloadResult:
        for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
            try:
                await asyncio.to_thread(self._fetch_via_tikwm, url)
                return DownloadResult(url=url, success=True)
            except Exception as exc:
                if attempt == MAX_DOWNLOAD_RETRIES:
                    return DownloadResult(url=url, success=False, error=str(exc))
                await asyncio.sleep(2 * attempt)
        return DownloadResult(url=url, success=False, error="Unknown")

    def _fetch_via_tikwm(self, url: str) -> None:
        """Download video via tikwm.com API — no watermark, no yt-dlp."""
        resp = requests.get(TIKWM_API, params={"url": url}, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"tikwm error: {data.get('msg')}")

        clip_meta = data.get("data", {})
        video_url = clip_meta.get("play")
        video_id  = clip_meta.get("id")
        if not video_url or not video_id:
            raise RuntimeError("tikwm response missing play/id")
        out_path  = DOWNLOAD_DIR / f"{video_id}.mp4"
        caption_text = build_caption_text(clip_meta)

        if out_path.exists():
            write_caption_file(out_path, caption_text)
            log.info("Already exists: %s", out_path.name)
            return

        with requests.get(video_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        log.info("Saved: %s", out_path.name)
        write_caption_file(out_path, caption_text)
        copy_to_shared(out_path)

# ── Collector service ─────────────────────────────────────────────────────────
class CollectorService:
    """
    Runs three tasks concurrently:
    1. ClipboardMonitor  — captures URLs to inbox buffer
    2. InboxWatcher      — drains inbox into download queue
    3. Downloader        — downloads from queue in parallel

    Copying and downloading happen simultaneously.
    You can copy 10 links quickly — none will be missed.
    """

    def __init__(self) -> None:
        self.queue    = PersistentQueue()
        self.monitor  = ClipboardMonitor(self.queue)
        self.watcher  = InboxWatcher(self.queue)
        self.loader   = Downloader(self.queue)
        self._tasks: list[asyncio.Task] = []

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown)

        print("[Collector] Starting — copy TikTok links via Share → Copy Link")
        print(f"[Collector] Downloads save to: {DOWNLOAD_DIR}")
        print(f"[Collector] Inbox buffer: {INBOX_PATH}")

        # Drain any leftover links from a previous session
        await self.watcher._drain_inbox()
        queued = await self.queue.size()
        if queued:
            print(f"[Collector] Resuming — {queued} URL(s) already in queue")

        self._tasks = [
            asyncio.create_task(self.monitor.start(),  name="clipboard"),
            asyncio.create_task(self.watcher.start(),  name="inbox"),
            asyncio.create_task(self.loader.run_forever(), name="downloader"),
        ]

        await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)

    def _shutdown(self) -> None:
        print("\n[Collector] Shutting down...")
        self.monitor.stop()
        self.watcher.stop()
        for task in self._tasks:
            task.cancel()

# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    service = CollectorService()
    try:
        asyncio.run(service.run())
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log_exception("Collector crash", exc)
        raise

if __name__ == "__main__":
    main()