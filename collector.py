"""TikTok collector entrypoint that bundles all helpers in one file."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import signal
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Deque, List

from logging.handlers import RotatingFileHandler

import requests
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DOWNLOAD_DIR = BASE_DIR / "downloads"

for path in (DATA_DIR, LOG_DIR, DOWNLOAD_DIR):
    path.mkdir(parents=True, exist_ok=True)

URL_LOG_PATH = DATA_DIR / "urls.txt"
QUEUE_PATH = DATA_DIR / "queue.json"
COLLECTOR_LOG_PATH = LOG_DIR / "collector.log"

URL_LOG_PATH.touch(exist_ok=True)

CLIPBOARD_POLL_INTERVAL = 1.0
MAX_DOWNLOAD_WORKERS = 3
DOWNLOAD_BATCH_SIZE = 20
MAX_DOWNLOAD_RETRIES = 3

TIKTOK_REGEX = re.compile(r"https?://(?:(?:www|vt|vm)\.)?tiktok\.com/\S+", re.IGNORECASE)


def setup_logger(name: str, log_path: Path, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def log_exception(logger: logging.Logger, message: str, exc: BaseException | None = None) -> None:
    logger.error("%s | %s", message, exc, exc_info=True)


class PersistentQueue:
    """Async-safe queue that persists to disk for crash recovery."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or QUEUE_PATH
        self.lock = asyncio.Lock()
        self._queue: Deque[str] = deque()
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for url in data:
                if isinstance(url, str):
                    self._queue.append(url)
                    self._seen.add(url)
        except json.JSONDecodeError:
            self._queue.clear()
            self._seen.clear()

    def _persist(self) -> None:
        self.path.write_text(json.dumps(list(self._queue), indent=2), encoding="utf-8")

    async def add(self, url: str) -> bool:
        async with self.lock:
            if url in self._seen:
                return False
            self._queue.append(url)
            self._seen.add(url)
            self._persist()
            return True

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

    async def peek_size(self) -> int:
        async with self.lock:
            return len(self._queue)


class ClipboardMonitor:
    """Polls the Termux clipboard and forwards TikTok URLs to a callback."""

    def __init__(self, callback: Callable[[str], Awaitable[None]], poll_interval: float | None = None) -> None:
        self.callback = callback
        self.poll_interval = poll_interval or CLIPBOARD_POLL_INTERVAL
        self._last_value: str | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        while self._running:
            await self._poll_once()
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False

    async def _poll_once(self) -> None:
        try:
            result = await asyncio.get_running_loop().run_in_executor(None, self._read_clipboard)
        except Exception:
            return
        if not result or result == self._last_value:
            return
        self._last_value = result
        match = TIKTOK_REGEX.search(result.strip())
        if match:
            await self.callback(match.group(0))

    @staticmethod
    def _read_clipboard() -> str:
        return subprocess.check_output(["termux-clipboard-get"], text=True).strip()


class _DownloadEventHandler(FileSystemEventHandler):
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def on_created(self, event):  # type: ignore[override]
        if not event.is_directory:
            self.logger.info("New file saved: %s", event.src_path)


@dataclass
class DownloadResult:
    url: str
    success: bool
    error: str | None = None


class Downloader:
    """Consumes the persistent queue and fetches videos in chunks."""

    def __init__(self, queue: PersistentQueue) -> None:
        self.queue = queue
        self.logger = setup_logger("collector", COLLECTOR_LOG_PATH)
        self.download_dir = DOWNLOAD_DIR
        self.sem = asyncio.Semaphore(MAX_DOWNLOAD_WORKERS)
        self._observer = Observer()

    async def __aenter__(self):
        handler = _DownloadEventHandler(self.logger)
        self._observer.schedule(handler, str(self.download_dir), recursive=False)
        self._observer.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._observer.stop()
        self._observer.join()

    async def run_forever(self) -> None:
        self.logger.info("Downloader loop started")
        while True:
            queue_size = await self.queue.peek_size()
            if queue_size == 0:
                await asyncio.sleep(1)
                continue
            batch = await self.queue.pop_batch(DOWNLOAD_BATCH_SIZE)
            if not batch:
                await asyncio.sleep(1)
                continue
            await self._process_batch(batch)

    async def _process_batch(self, batch: List[str]) -> None:
        self.logger.info("Processing batch of %s downloads", len(batch))
        tasks = [asyncio.create_task(self._download_with_semaphore(url)) for url in batch]
        for task in asyncio.as_completed(tasks):
            result = await task
            if result.success:
                self.logger.info("Downloaded %s", result.url)
            else:
                self.logger.error("Failed %s | %s", result.url, result.error)

    async def _download_with_semaphore(self, url: str) -> DownloadResult:
        async with self.sem:
            return await self._download_with_retries(url)

    async def _download_with_retries(self, url: str) -> DownloadResult:
        for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
            try:
                await asyncio.to_thread(self._run_tikwm, url)
                return DownloadResult(url=url, success=True)
            except Exception as exc:  # noqa: BLE001
                if attempt == MAX_DOWNLOAD_RETRIES:
                    return DownloadResult(url=url, success=False, error=str(exc))
                await asyncio.sleep(2 * attempt)
        return DownloadResult(url=url, success=False, error="Unknown error")

    def _run_tikwm(self, url: str) -> None:
        api = "https://tikwm.com/api/"
        resp = requests.get(api, params={"url": url}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"tikwm error: {data.get('msg')}")
        video_url = data["data"]["play"]
        video_id = data["data"]["id"]
        out_path = self.download_dir / f"{video_id}.mp4"
        with requests.get(video_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(out_path, "wb") as handle:
                for chunk in r.iter_content(chunk_size=8192):
                    handle.write(chunk)


class CollectorService:
    def __init__(self) -> None:
        self.queue = PersistentQueue()
        self.logger = setup_logger("collector", COLLECTOR_LOG_PATH)
        self.url_log_path = URL_LOG_PATH
        self.known_urls = self._load_known_urls()
        self.clipboard_monitor = ClipboardMonitor(self._handle_new_url)
        self._pending: list[asyncio.Task] = []

    def _load_known_urls(self) -> set[str]:
        lines = self.url_log_path.read_text(encoding="utf-8").splitlines()
        return {line.strip() for line in lines if line.strip()}

    async def _handle_new_url(self, url: str) -> None:
        if url in self.known_urls:
            self.logger.debug("Duplicate URL ignored: %s", url)
            return
        self.known_urls.add(url)
        with self.url_log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{url}\n")
        enqueued = await self.queue.add(url)
        if enqueued:
            self.logger.info("Enqueued %s", url)
        else:
            self.logger.info("URL already in queue: %s", url)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for signame in {signal.SIGINT, signal.SIGTERM}:
            loop.add_signal_handler(signame, self._graceful_shutdown)
        print("[Collector] Running. Copy TikTok links via Share -> Copy Link.")
        async with Downloader(self.queue) as downloader:
            download_task = asyncio.create_task(downloader.run_forever())
            clipboard_task = asyncio.create_task(self.clipboard_monitor.start())
            self._pending = [download_task, clipboard_task]
            await asyncio.wait(self._pending, return_when=asyncio.FIRST_COMPLETED)

    def _graceful_shutdown(self) -> None:
        print("[Collector] Shutting down...")
        for task in self._pending:
            task.cancel()


def main() -> None:
    service = CollectorService()
    try:
        asyncio.run(service.run())
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger = setup_logger("collector", COLLECTOR_LOG_PATH)
        log_exception(logger, "Collector crash", exc)
        raise


if __name__ == "__main__":
    main()
