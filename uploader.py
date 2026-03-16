"""NewsBreak uploader entrypoint with all helpers in one file."""
from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List

from logging.handlers import RotatingFileHandler

import adbutils
import uiautomator2 as u2

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DOWNLOAD_DIR = BASE_DIR / "downloads"
SHARED_EXPORT_DIR = Path.home() / "storage" / "shared" / "scoopz_downloads"

for path in (DATA_DIR, LOG_DIR, DOWNLOAD_DIR):
    path.mkdir(parents=True, exist_ok=True)

try:
    SHARED_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
except PermissionError:
    pass

UPLOADER_LOG_PATH = LOG_DIR / "uploader.log"
UPLOAD_STATE_PATH = DATA_DIR / "uploaded.json"

ADB_HOST = "127.0.0.1"
ADB_PORT = 5555
UI_WAIT_TIMEOUT = 15.0
CAPTION_PRESETS = [
    "Fresh picks from today's feed - what do you think?",
    "Would you try this trend? Tell me below!",
    "Top highlights going viral this week.",
    "Follow for the best scoopz scrolls in one place.",
    "Your quick hit of local scoopz creativity!",
]
DEFAULT_CAPTION = CAPTION_PRESETS[0]
HASHTAGS = ["#funny", "#interesting", "#viral", "#trending", "#mustwatch", "#scoopz"]
MAX_UPLOAD_RETRIES = 3
MAX_UPLOAD_BATCH = 40


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


def ensure_shared_copy(video_path: Path) -> Path:
    target = SHARED_EXPORT_DIR / video_path.name
    if not target.exists() or target.stat().st_mtime < video_path.stat().st_mtime:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(video_path, target)
    return target


@dataclass
class Selector:
    text: str | None = None
    resource_id: str | None = None
    description: str | None = None


class DeviceController:
    def __init__(self) -> None:
        self.logger = setup_logger("uploader", UPLOADER_LOG_PATH)
        self.client = adbutils.AdbClient(host=ADB_HOST, port=ADB_PORT)
        self.device = self.client.device()
        self.u2 = u2.connect(self.device.serial)

    def tap_selector(self, selector: Selector, timeout: float | None = None) -> bool:
        timeout = timeout or UI_WAIT_TIMEOUT
        ui_selector = self._build_selector(selector)
        if not ui_selector.wait(timeout=timeout):
            return False
        ui_selector.click()
        return True

    def set_text(self, selector: Selector, text: str, timeout: float | None = None) -> bool:
        timeout = timeout or UI_WAIT_TIMEOUT
        ui_selector = self._build_selector(selector)
        if not ui_selector.wait(timeout=timeout):
            return False
        ui_selector.set_text(text)
        return True

    def wait_for(self, selector: Selector, timeout: float | None = None) -> bool:
        timeout = timeout or UI_WAIT_TIMEOUT
        return self._build_selector(selector).wait(timeout=timeout)

    def back(self) -> None:
        self.u2.press("back")

    def _build_selector(self, selector: Selector):
        query = self.u2
        if selector.text:
            query = query(text=selector.text)
        if selector.resource_id:
            query = query(resourceId=selector.resource_id)
        if selector.description:
            query = query(description=selector.description)
        return query


class UploadError(RuntimeError):
    pass


class UploadManager:
    UPLOAD_BUTTON = Selector(text="Upload")
    PICKER_BUTTON = Selector(text="Select video")
    GALLERY_TAB = Selector(text="Gallery")
    CAPTION_FIELD = Selector(resource_id="com.particlenews.newsbreak:id/caption")
    PUBLISH_BUTTON = Selector(text="Publish")
    SUCCESS_INDICATOR = Selector(text="Submitted")

    def __init__(self) -> None:
        self.logger = setup_logger("uploader", UPLOADER_LOG_PATH)
        self.controller = DeviceController()
        self.state_path = UPLOAD_STATE_PATH
        self.uploaded = self._load_state()

    def _load_state(self) -> set[str]:
        if not self.state_path.exists():
            self.state_path.write_text("[]", encoding="utf-8")
        try:
            return set(json.loads(self.state_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            return set()

    def _persist_state(self) -> None:
        self.state_path.write_text(json.dumps(sorted(self.uploaded), indent=2), encoding="utf-8")

    def _candidates(self) -> List[Path]:
        files = sorted(DOWNLOAD_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        return [f for f in files if f.name not in self.uploaded]

    def upload_batch(self, caption: str | None = None, limit: int | None = None) -> None:
        limit = limit or MAX_UPLOAD_BATCH
        videos = self._candidates()[:limit]
        if not videos:
            print("[Uploader] No new videos available.")
            return
        print(f"[Uploader] Starting upload of {len(videos)} videos")
        for position, video in enumerate(videos):
            shared_path = ensure_shared_copy(video)
            caption_text = self._select_caption(position, caption)
            success = self._retry_upload(shared_path, caption_text)
            if success:
                self.uploaded.add(video.name)
                self._persist_state()
            else:
                self.logger.error("Giving up on %s", video.name)

    def _select_caption(self, position: int, override: str | None) -> str:
        if override:
            return self._with_hashtags(override)
        if not CAPTION_PRESETS:
            return self._with_hashtags(DEFAULT_CAPTION)
        index = (len(self.uploaded) + position) % len(CAPTION_PRESETS)
        return self._with_hashtags(CAPTION_PRESETS[index])

    def _with_hashtags(self, text: str) -> str:
        hashtag_block = " ".join(HASHTAGS)
        return f"{text} {hashtag_block}".strip()

    def _retry_upload(self, shared_path: Path, caption: str) -> bool:
        for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
            try:
                self._upload_once(shared_path, caption)
                self.logger.info("Uploaded %s", shared_path.name)
                return True
            except UploadError as exc:
                log_exception(self.logger, f"Upload attempt {attempt} failed", exc)
                time.sleep(3 * attempt)
        return False

    def _upload_once(self, shared_path: Path, caption: str) -> None:
        if not self.controller.tap_selector(self.UPLOAD_BUTTON):
            raise UploadError("Upload button not found")
        if not self.controller.tap_selector(self.PICKER_BUTTON):
            raise UploadError("Picker button not found")
        self.controller.tap_selector(self.GALLERY_TAB)
        video_selector = Selector(text=shared_path.name)
        if not self.controller.tap_selector(video_selector, timeout=30):
            raise UploadError(f"Video {shared_path.name} not visible")
        if not self.controller.set_text(self.CAPTION_FIELD, caption):
            raise UploadError("Caption field not found")
        if not self.controller.tap_selector(self.PUBLISH_BUTTON):
            raise UploadError("Publish button not found")
        if not self.controller.wait_for(self.SUCCESS_INDICATOR, timeout=60):
            raise UploadError("No success indicator")
        time.sleep(2)


def main() -> None:
    manager = UploadManager()
    manager.upload_batch()


if __name__ == "__main__":
    main()
