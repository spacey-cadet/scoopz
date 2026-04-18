#!/usr/bin/env python3
"""
Scoopz Collector v2: Production-Ready TikTok Downloader
Validates videos for thumbnail extraction + AI caption generation workflow.
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
import json
from pathlib import Path
from typing import Optional, Tuple, Dict, Set
from dataclasses import dataclass
import requests

# ══════════════════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════════════════

INBOX_PATH   = Path("/sdcard/Download/links_inbox.txt")
EXPORT_DIR   = Path("/sdcard/Pictures/scoopz")
THUMB_DIR    = Path("/sdcard/Pictures/scoopz/.thumbs")
HISTORY_FILE = Path("/sdcard/Download/scoopz_history.log")
LOG_FILE     = Path("/sdcard/Download/scoopz_errors.log")

# API settings
TIKWM_API = "https://www.tikwm.com/api/"
TIKTOK_URL_PATTERN = re.compile(
    r"https?://(?:(?:www|vt|vm)\.)?tiktok\.com/\S+", 
    re.IGNORECASE
)

# Performance
MAX_WORKERS = 5
POLL_INTERVAL = 0.5
API_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 45

# Validation thresholds (critical for your workflow)
MIN_FILE_SIZE = 80_000        # 80KB - reject corrupted/partial downloads
MIN_DURATION = 0.0            # Disabled: do not reject by short duration
MAX_DURATION = 0.0            # Disabled: do not reject by long duration

# Retry configuration
DOWNLOAD_RETRIES = 3
API_RETRIES = 3
RETRY_BACKOFF = 2.0  # Exponential backoff multiplier

# Feature flags
REQUIRE_THUMBNAIL = True      # Must be able to extract thumbnail
REQUIRE_FRAME_EXTRACTION = True  # Must be able to extract frames for AI
# When false, thumbnail extraction failure is advisory and won't reject download.
STRICT_THUMBNAIL_VALIDATION = os.environ.get("SCOOPZ_STRICT_THUMB_VALIDATION", "0") == "1"
VERBOSE_LOGGING = os.environ.get("SCOOPZ_VERBOSE", "0") == "1"
USE_ANSI_STATUS = sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"

# Thumbnail cache controls (to avoid storage bloat)
THUMB_MAX_FILES = int(os.environ.get("SCOOPZ_THUMB_MAX_FILES", "500"))
THUMB_MAX_MB = int(os.environ.get("SCOOPZ_THUMB_MAX_MB", "120"))
THUMB_MAX_AGE_DAYS = int(os.environ.get("SCOOPZ_THUMB_MAX_AGE_DAYS", "30"))
THUMB_PRUNE_INTERVAL_SEC = int(os.environ.get("SCOOPZ_THUMB_PRUNE_INTERVAL_SEC", "300"))

# Multi-phone behavior:
# - SCOOPZ_TRANSCODE_MODE=auto (default): learn if this phone needs transcode
# - SCOOPZ_TRANSCODE_MODE=on: always transcode
# - SCOOPZ_TRANSCODE_MODE=off: never transcode
# Legacy support: SCOOPZ_FORCE_TRANSCODE=1/0
_legacy_force_transcode = os.environ.get("SCOOPZ_FORCE_TRANSCODE")
_mode_raw = os.environ.get("SCOOPZ_TRANSCODE_MODE")
if _mode_raw:
    TRANSCODE_MODE = _mode_raw.strip().lower()
elif _legacy_force_transcode is not None:
    TRANSCODE_MODE = "on" if _legacy_force_transcode == "1" else "off"
else:
    TRANSCODE_MODE = "auto"

if TRANSCODE_MODE not in {"auto", "on", "off"}:
    TRANSCODE_MODE = "auto"

# ══════════════════════════════════════════════════════════════════════════════
# Data Structures
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class VideoMetadata:
    """Metadata from TikWM API."""
    video_id: str
    author: str
    title: str
    duration: float
    play_url: str
    hd_play_url: Optional[str] = None
    thumb_url: Optional[str] = None
    
    @classmethod
    def from_api_response(cls, data: Dict) -> Optional['VideoMetadata']:
        """Parse TikWM API response."""
        try:
            video_id = str(data.get("id", ""))
            if not video_id:
                return None
            
            # Parse author
            author_data = data.get("author", {})
            if isinstance(author_data, dict):
                author = (
                    author_data.get("unique_id") or
                    author_data.get("nickname") or
                    author_data.get("id") or
                    "unknown"
                )
            else:
                author = str(author_data) if author_data else "unknown"
            
            # Parse title
            title = (
                data.get("title") or
                data.get("desc") or
                data.get("region") or
                ""
            )
            
            # Duration
            duration = float(data.get("duration", 0))
            
            # Video URLs
            play_url = data.get("play")
            hd_play_url = data.get("hdplay")
            thumb_url = data.get("cover") or data.get("ai_dynamic_cover") or data.get("origin_cover")
            
            if not play_url:
                return None
            
            return cls(
                video_id=video_id,
                author=author,
                title=title,
                duration=duration,
                play_url=play_url,
                hd_play_url=hd_play_url,
                thumb_url=thumb_url
            )
        except Exception:
            return None

@dataclass
class ValidationResult:
    """Result of video file validation."""
    valid: bool
    reason: str = ""
    can_extract_thumbnail: bool = False
    can_extract_frames: bool = False
    duration: float = 0.0
    file_size: int = 0

@dataclass
class DownloadStats:
    """Download statistics."""
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    active: int = 0
    queued: int = 0

# ══════════════════════════════════════════════════════════════════════════════
# Video Validation (Critical for Caption Workflow)
# ══════════════════════════════════════════════════════════════════════════════

class VideoValidator:
    """
    Validates downloaded videos for thumbnail extraction + AI captioning.
    
    This is the CORE of your workflow - if a video passes validation,
    it WILL work in both the picker UI and caption generation.
    """
    
    @staticmethod
    def validate_file(video_path: Path, expected_duration: float = 0.0) -> ValidationResult:
        """
        Comprehensive validation for caption workflow.
        
        Checks:
        1. File exists and has minimum size
        2. FFmpeg can read video metadata
        3. Duration is within acceptable range
        4. Can extract thumbnail (for picker UI)
        5. Can extract frames (for AI caption generation)
        """
        # Stage 1: Basic file checks
        if not video_path.exists():
            return ValidationResult(valid=False, reason="File does not exist")
        
        try:
            file_size = video_path.stat().st_size
        except Exception as e:
            return ValidationResult(valid=False, reason=f"Cannot stat file: {e}")
        
        if file_size < MIN_FILE_SIZE:
            return ValidationResult(
                valid=False,
                reason=f"File too small: {file_size} bytes < {MIN_FILE_SIZE}",
                file_size=file_size
            )
        
        # Stage 2: FFmpeg metadata check (with fallback)
        duration = VideoValidator._get_video_duration(video_path)
        if duration <= 0 and expected_duration > 0:
            duration = float(expected_duration)

        # Some builds fail duration probing for valid files; require decodability instead.
        if duration <= 0 and not VideoValidator._can_decode_video(video_path):
            return ValidationResult(
                valid=False,
                reason="Cannot decode video stream",
                file_size=file_size
            )
        
        # Stage 3: Duration validation
        if MIN_DURATION > 0 and duration < MIN_DURATION:
            return ValidationResult(
                valid=False,
                reason=f"Video too short: {duration:.1f}s < {MIN_DURATION}s",
                duration=duration,
                file_size=file_size
            )
        
        if MAX_DURATION > 0 and duration > MAX_DURATION:
            return ValidationResult(
                valid=False,
                reason=f"Video too long: {duration:.1f}s > {MAX_DURATION}s",
                duration=duration,
                file_size=file_size
            )
        
        # Stage 4: Thumbnail extraction test (for picker UI)
        can_thumbnail = VideoValidator._can_extract_thumbnail(video_path)
        if REQUIRE_THUMBNAIL and STRICT_THUMBNAIL_VALIDATION and not can_thumbnail:
            return ValidationResult(
                valid=False,
                reason="Cannot extract thumbnail (file will appear as no-thumb in picker)",
                duration=duration,
                file_size=file_size,
                can_extract_thumbnail=False
            )
        
        # Stage 5: Frame extraction test (for AI caption generation)
        can_frames = VideoValidator._can_extract_frames(video_path, duration_hint=duration)
        if REQUIRE_FRAME_EXTRACTION and not can_frames:
            return ValidationResult(
                valid=False,
                reason="Cannot extract frames (AI captioning will fail)",
                duration=duration,
                file_size=file_size,
                can_extract_thumbnail=can_thumbnail,
                can_extract_frames=False
            )
        
        # All checks passed!
        return ValidationResult(
            valid=True,
            reason="Valid for thumbnail + caption workflow",
            duration=duration,
            file_size=file_size,
            can_extract_thumbnail=can_thumbnail,
            can_extract_frames=can_frames
        )
    
    @staticmethod
    def _get_video_duration(video_path: Path) -> float:
        """Get video duration using ffprobe."""
        try:
            result = subprocess.run([
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path)
            ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False
            )
            
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
            
            return 0.0
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            return 0.0
    
    @staticmethod
    def _can_extract_thumbnail(video_path: Path) -> bool:
        """
        Test if thumbnail can be extracted.
        Mirrors the picker's thumbnail generation logic.
        """
        # Try multiple seek points (same as picker does)
        seek_points = ["1", "2", "0.5", "0.1"]
        
        for seek in seek_points:
            try:
                result = subprocess.run([
                    "ffmpeg",
                    "-nostdin",
                    "-v", "error",
                    "-ss", seek,
                    "-i", str(video_path),
                    "-vframes", "1",
                    "-f", "null",
                    "-"
                ],
                    capture_output=True,
                    timeout=12,
                    check=False
                )
                
                if result.returncode == 0:
                    return True
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return False

        # Fallback: remux container and retry thumbnail extraction.
        remux_path = video_path.with_suffix(".thumbfix.mp4")
        try:
            remux = subprocess.run([
                "ffmpeg",
                "-nostdin",
                "-v", "error",
                "-y",
                "-i", str(video_path),
                "-c", "copy",
                "-movflags", "+faststart",
                str(remux_path),
            ],
                capture_output=True,
                timeout=20,
                check=False
            )

            if remux.returncode != 0 or not remux_path.exists() or remux_path.stat().st_size <= 0:
                return False

            for seek in seek_points:
                result = subprocess.run([
                    "ffmpeg",
                    "-nostdin",
                    "-v", "error",
                    "-ss", seek,
                    "-i", str(remux_path),
                    "-vframes", "1",
                    "-f", "null",
                    "-"
                ],
                    capture_output=True,
                    timeout=12,
                    check=False
                )
                if result.returncode == 0:
                    return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        finally:
            try:
                if remux_path.exists():
                    remux_path.unlink()
            except Exception:
                pass
        
        return False
    
    @staticmethod
    def _can_extract_frames(video_path: Path, duration_hint: float = 0.0) -> bool:
        """
        Test if frames can be extracted for AI captioning.
        If duration is unknown on this device, use fixed seek points fallback.
        """
        duration = duration_hint if duration_hint > 0 else VideoValidator._get_video_duration(video_path)
        
        if duration > 0:
            # Test frame extraction at key points (same as your caption script)
            test_points = [
                duration * 0.25,
                duration * 0.75
            ]
        else:
            # Fallback for devices where duration probing is unreliable.
            test_points = [0.5, 1.5]
        
        for seek_time in test_points:
            try:
                result = subprocess.run([
                    "ffmpeg",
                    "-nostdin",
                    "-v", "error",
                    "-ss", str(seek_time),
                    "-i", str(video_path),
                    "-vframes", "1",
                    "-f", "null",
                    "-"
                ],
                    capture_output=True,
                    timeout=12,
                    check=False
                )
                
                if result.returncode != 0:
                    return False
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return False
        
        return True

    @staticmethod
    def _can_decode_video(video_path: Path) -> bool:
        """Quick smoke test: can ffmpeg decode at least one video frame?"""
        try:
            result = subprocess.run([
                "ffmpeg",
                "-nostdin",
                "-v", "error",
                "-i", str(video_path),
                "-map", "0:v:0",
                "-frames:v", "1",
                "-f", "null",
                "-"
            ],
                capture_output=True,
                timeout=12,
                check=False
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

# ══════════════════════════════════════════════════════════════════════════════
# Downloader
# ══════════════════════════════════════════════════════════════════════════════

class VideoDownloader:
    """Handles video download with validation and retries."""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8"
        })
        self.transcode_mode = TRANSCODE_MODE
        self._transcode_needed: Optional[bool] = None

    @staticmethod
    def _is_compatibility_issue(reason: str) -> bool:
        text = (reason or "").lower()
        return ("thumbnail" in text) or ("frame" in text) or ("decode" in text)
    
    def download(self, metadata: VideoMetadata, output_path: Path) -> Tuple[bool, str]:
        """
        Download video with validation.
        
        Returns:
            (success: bool, reason: str)
        """
        # Use HD URL if available
        url = metadata.hd_play_url or metadata.play_url
        
        temp_path = output_path.with_suffix('.mp4.part')
        fixed_path = output_path.with_suffix('.mp4.fixed.part')
        
        # Clean up any stale temp files
        try:
            if temp_path.exists():
                temp_path.unlink()
        except:
            pass
        
        # Download with retries
        for attempt in range(DOWNLOAD_RETRIES):
            try:
                # Download to temp file
                success, reason = self._download_file(url, temp_path)
                
                if not success:
                    if attempt < DOWNLOAD_RETRIES - 1:
                        time.sleep(RETRY_BACKOFF ** attempt)
                        continue
                    return False, reason

                # Decide strategy for this phone/runtime.
                if self.transcode_mode == "on":
                    if not self._normalize_video_for_picker(temp_path, fixed_path):
                        try:
                            temp_path.unlink(missing_ok=True)
                            fixed_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        return False, "Validation failed: normalize/transcode failed"
                    try:
                        temp_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    fixed_path.rename(temp_path)

                elif self.transcode_mode == "auto":
                    if self._transcode_needed is True:
                        if not self._normalize_video_for_picker(temp_path, fixed_path):
                            try:
                                temp_path.unlink(missing_ok=True)
                                fixed_path.unlink(missing_ok=True)
                            except Exception:
                                pass
                            return False, "Validation failed: normalize/transcode failed"
                        try:
                            temp_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        fixed_path.rename(temp_path)
                    elif self._transcode_needed is None:
                        probe_validation = VideoValidator.validate_file(
                            temp_path,
                            expected_duration=metadata.duration
                        )
                        if probe_validation.valid:
                            self._transcode_needed = False
                        elif self._is_compatibility_issue(probe_validation.reason):
                            if self._normalize_video_for_picker(temp_path, fixed_path):
                                repaired_validation = VideoValidator.validate_file(
                                    fixed_path,
                                    expected_duration=metadata.duration
                                )
                                if repaired_validation.valid:
                                    self._transcode_needed = True
                                    try:
                                        temp_path.unlink(missing_ok=True)
                                    except Exception:
                                        pass
                                    fixed_path.rename(temp_path)
                                else:
                                    try:
                                        temp_path.unlink(missing_ok=True)
                                        fixed_path.unlink(missing_ok=True)
                                    except Exception:
                                        pass
                                    return False, f"Validation failed after normalize: {repaired_validation.reason}"
                            else:
                                try:
                                    temp_path.unlink(missing_ok=True)
                                    fixed_path.unlink(missing_ok=True)
                                except Exception:
                                    pass
                                return False, "Validation failed: normalize/transcode failed"
                        else:
                            # Non-compatibility failure; no point in transcoding this file.
                            try:
                                temp_path.unlink(missing_ok=True)
                            except Exception:
                                pass
                            return False, f"Validation failed: {probe_validation.reason}"

                validation = VideoValidator.validate_file(
                    temp_path,
                    expected_duration=metadata.duration
                )
                
                if not validation.valid:
                    # Clean up invalid file
                    try:
                        temp_path.unlink()
                        fixed_path.unlink(missing_ok=True)
                    except:
                        pass

                    # Non-compatibility validation failures are unlikely to improve via redownload.
                    if attempt == 0:
                        return False, f"Validation failed: {validation.reason}"
                    
                    if attempt < DOWNLOAD_RETRIES - 1:
                        time.sleep(RETRY_BACKOFF ** attempt)
                        continue
                    
                    return False, f"Validation failed: {validation.reason}"
                
                # Success! Move to final location
                temp_path.rename(output_path)
                return True, ""
            
            except Exception as e:
                # Clean up on exception
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                    if fixed_path.exists():
                        fixed_path.unlink()
                except:
                    pass
                
                if attempt < DOWNLOAD_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF ** attempt)
                    continue
                
                return False, f"Download error: {type(e).__name__}"
        
        return False, "Download failed after retries"
    
    def _download_file(self, url: str, output_path: Path) -> Tuple[bool, str]:
        """Download single file from URL."""
        try:
            with self.session.get(
                url,
                stream=True,
                timeout=DOWNLOAD_TIMEOUT,
                headers={"Referer": "https://www.tiktok.com/"}
            ) as response:
                response.raise_for_status()
                
                # Check content type
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" in content_type or "application/json" in content_type:
                    return False, f"Invalid content-type: {content_type}"
                
                # Download
                with open(output_path, "wb") as f:
                    shutil.copyfileobj(response.raw, f)
                
                return True, ""
        
        except requests.exceptions.Timeout:
            return False, "Download timeout"
        except requests.exceptions.HTTPError as e:
            return False, f"HTTP {e.response.status_code}"
        except Exception as e:
            return False, f"{type(e).__name__}"

    def _normalize_video_for_picker(self, src_path: Path, dst_path: Path) -> bool:
        """Re-encode to a picker-friendly MP4 profile for cross-device decoding."""
        try:
            if dst_path.exists():
                dst_path.unlink()
        except Exception:
            pass

        try:
            result = subprocess.run([
                "ffmpeg",
                "-nostdin",
                "-v", "error",
                "-y",
                "-i", str(src_path),
                "-map", "0:v:0",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "24",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-c:a", "aac",
                "-b:a", "128k",
                str(dst_path),
            ],
                capture_output=True,
                timeout=120,
                check=False
            )

            return (
                result.returncode == 0
                and dst_path.exists()
                and dst_path.stat().st_size >= MIN_FILE_SIZE
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        except Exception:
            return False

# ══════════════════════════════════════════════════════════════════════════════
# TikWM API Client
# ══════════════════════════════════════════════════════════════════════════════

class TikWMClient:
    """TikWM API client with retry logic."""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
    
    def get_video_info(self, url: str) -> Optional[VideoMetadata]:
        """
        Get video metadata from TikWM API.
        
        Returns:
            VideoMetadata on success, None on failure
        """
        for attempt in range(API_RETRIES):
            try:
                response = self.session.post(
                    TIKWM_API,
                    data={"url": url, "hd": 1},
                    timeout=API_TIMEOUT
                )
                response.raise_for_status()
                
                data = response.json()
                
                if data.get("code") != 0:
                    error_msg = data.get("msg", "Unknown API error")
                    if attempt < API_RETRIES - 1 and data.get("code") == -1:
                        time.sleep(RETRY_BACKOFF ** attempt)
                        continue
                    return None
                
                clip_data = data.get("data", {})
                return VideoMetadata.from_api_response(clip_data)
            
            except requests.exceptions.Timeout:
                if attempt < API_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF ** attempt)
                    continue
                return None
            except Exception:
                if attempt < API_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF ** attempt)
                    continue
                return None
        
        return None

# ══════════════════════════════════════════════════════════════════════════════
# Main Collector
# ══════════════════════════════════════════════════════════════════════════════

class ScoopzCollector:
    """Main collector orchestrator."""
    
    def __init__(self):
        self.api_client = TikWMClient()
        self.downloader = VideoDownloader()
        
        self.queue: asyncio.Queue = asyncio.Queue()
        self.stats = DownloadStats()
        self.history: Set[str] = set()
        self._last_status_len = 0
        self._last_thumb_prune_ts = 0.0
        
        # Initialize
        self._ensure_directories()
        self._load_history()
    
    def _ensure_directories(self):
        """Create required directories."""
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        
        if not HISTORY_FILE.exists():
            HISTORY_FILE.touch()
        
        if not LOG_FILE.exists():
            LOG_FILE.touch()
    
    def _load_history(self):
        """Load download history."""
        try:
            content = HISTORY_FILE.read_text()
            self.history = set(line.strip() for line in content.splitlines() if line.strip())
        except:
            self.history = set()
    
    def _add_to_history(self, video_id: str):
        """Add video to history."""
        if video_id not in self.history:
            self.history.add(video_id)
            try:
                with open(HISTORY_FILE, "a") as f:
                    f.write(f"{video_id}\n")
            except:
                pass

    def _sidecar_thumb_path(self, video_path: Path) -> Path:
        return THUMB_DIR / f"{video_path.name}.jpg"

    def _fetch_sidecar_thumbnail(self, metadata: VideoMetadata, video_path: Path):
        """Save API thumbnail as sidecar so picker does not depend on ffmpeg thumbs."""
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
        """Keep thumbnail cache bounded by age, source existence, count, and size."""
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

            # Remove orphan and too-old thumbs first.
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
            if not entries:
                return

            # Then enforce max count and max total size.
            entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            max_bytes = max(1, THUMB_MAX_MB) * 1024 * 1024
            total = 0
            keep = []
            for idx, p in enumerate(entries):
                try:
                    size = p.stat().st_size
                except Exception:
                    continue

                over_count = idx >= max(1, THUMB_MAX_FILES)
                over_size = (total + size) > max_bytes
                if over_count or over_size:
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
                else:
                    keep.append(p)
                    total += size
        except Exception:
            pass
    
    def _log_error(self, video_id: str, reason: str):
        """Log error to file."""
        try:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG_FILE, "a") as f:
                f.write(f"[{timestamp}] {video_id}: {reason}\n")
        except:
            pass

        # Always print fail events, then redraw the live status line.
        if USE_ANSI_STATUS:
            sys.stdout.write("\r\033[K")
        else:
            sys.stdout.write("\r" + (" " * self._last_status_len) + "\r")
        print(f"[fail] {video_id}: {reason}", flush=True)
        self._update_ui()
    
    def _update_ui(self):
        """Update terminal UI."""
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
    
    def _send_notification(self, title: str, message: str):
        """Send Android notification."""
        try:
            subprocess.run([
                "termux-notification",
                "-t", title,
                "-c", message,
                "--id", "scoopz_collector",
                "--priority", "low"
            ],
                capture_output=True,
                timeout=3,
                check=False
            )
        except:
            pass
    
    def _safe_filename(self, text: str, max_len: int = 48) -> str:
        """Create safe filename component."""
        text = (text or "").strip().lower()
        text = re.sub(r"\s+", "_", text)
        text = re.sub(r"[^a-z0-9_\-]", "", text)
        text = re.sub(r"_+", "_", text).strip("_")
        return text[:max_len]
    
    def _build_filename(self, metadata: VideoMetadata) -> str:
        """Build readable filename."""
        author = self._safe_filename(metadata.author, max_len=20)
        title = self._safe_filename(metadata.title, max_len=40)
        video_id = self._safe_filename(metadata.video_id, max_len=24)
        
        parts = []
        if author:
            parts.append(author)
        if title:
            parts.append(title)
        parts.append(video_id)
        
        return "__".join(parts) + ".mp4"
    
    async def _watch_inbox(self):
        """Watch inbox file for new URLs."""
        while True:
            try:
                if INBOX_PATH.exists() and INBOX_PATH.stat().st_size > 0:
                    content = INBOX_PATH.read_text().strip()
                    
                    if content:
                        # Extract URLs
                        urls = []
                        for line in content.splitlines():
                            match = TIKTOK_URL_PATTERN.search(line)
                            if match:
                                urls.append(match.group(0).strip())
                        
                        # Clear inbox immediately
                        if urls:
                            INBOX_PATH.write_text("")
                            
                            # Add to queue
                            for url in urls:
                                await self.queue.put(url)
                            
                            self._update_ui()
            except:
                pass
            
            await asyncio.sleep(POLL_INTERVAL)
    
    async def _download_worker(self):
        """Download worker."""
        while True:
            url = await self.queue.get()
            
            try:
                self.stats.active += 1
                self._update_ui()
                
                # Get video metadata
                metadata = await asyncio.to_thread(
                    self.api_client.get_video_info,
                    url
                )
                
                if not metadata:
                    self.stats.failed += 1
                    self._log_error("unknown", "API returned no metadata")
                    continue
                
                # Check history
                if metadata.video_id in self.history:
                    self.stats.skipped += 1
                    continue
                
                # Build output path
                filename = self._build_filename(metadata)
                output_path = EXPORT_DIR / filename
                
                # Download and validate
                success, reason = await asyncio.to_thread(
                    self.downloader.download,
                    metadata,
                    output_path
                )
                
                if success:
                    # Add to media library
                    try:
                        subprocess.run([
                            "termux-media-scan",
                            str(output_path)
                        ],
                            capture_output=True,
                            timeout=5,
                            check=False
                        )
                    except:
                        pass

                    # Save sidecar thumbnail from API metadata when available.
                    await asyncio.to_thread(self._fetch_sidecar_thumbnail, metadata, output_path)
                    await asyncio.to_thread(self._prune_thumb_cache)
                    
                    # Update records
                    self._add_to_history(metadata.video_id)
                    self.stats.downloaded += 1
                    
                    # Notify
                    self._send_notification(
                        "Scoopz Downloaded",
                        f"{metadata.author}: {metadata.title[:30]}"
                    )
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
        """Run collector."""
        print("=" * 70)
        print("🎬 Scoopz Collector v2")
        print("=" * 70)
        print(f"Export directory: {EXPORT_DIR}")
        print(f"Workers: {MAX_WORKERS}")
        print(f"Min duration: {MIN_DURATION}s")
        print(f"Max duration: {MAX_DURATION}s" if MAX_DURATION > 0 else "Max duration: unlimited")
        print(f"Transcode mode: {TRANSCODE_MODE}")
        print(f"Strict thumbnail validation: {STRICT_THUMBNAIL_VALIDATION}")
        print(f"Require thumbnail: {REQUIRE_THUMBNAIL}")
        print(f"Require frame extraction: {REQUIRE_FRAME_EXTRACTION}")
        print("=" * 70)
        print("\n📋 Watching inbox — Share TikTok → Copy Link\n")
        
        # Send notification
        self._send_notification(
            "Scoopz Active",
            "Watching for TikTok links..."
        )
        
        # Start workers
        workers = [
            asyncio.create_task(self._download_worker())
            for _ in range(MAX_WORKERS)
        ]
        
        watcher = asyncio.create_task(self._watch_inbox())
        
        # Run forever
        await asyncio.gather(watcher, *workers)

# ══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    """Main entry point."""
    try:
        collector = ScoopzCollector()
        asyncio.run(collector.run())
    except KeyboardInterrupt:
        print("\n\n👋 Stopped.\n")
        sys.exit(0)

if __name__ == "__main__":
    main()