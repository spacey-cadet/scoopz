#!/usr/bin/env python3
"""
Scoopz v2 Optimized: 4 frames + parallel API calls
Target: 50s for 8 videos (~6s per video)
"""
import subprocess
import sys
import os
import time
import base64
import threading
import socket
import re
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional
import requests

# ── Config ───────────────────────────────────────────────
OPENAI_KEY_FILE        = os.path.expanduser("~/.config/openai_key.txt")
OPENAI_MODEL           = "gpt-4o"
OPENAI_BASE            = "https://api.openai.com/v1/chat/completions"
OPENAI_MODELS_BASE     = "https://api.openai.com/v1/models"
OPENAI_TRANSCRIBE_BASE = "https://api.openai.com/v1/audio/transcriptions"
SCOOPZ_DIR             = os.environ.get("SCOOPZ_DIR", "/sdcard/Pictures/scoopz")
THUMB_DIR              = os.environ.get("SCOOPZ_THUMB_DIR", os.path.join(SCOOPZ_DIR, ".thumbs"))
TEMP_DIR               = os.environ.get("SCOOPZ_TEMP_DIR", "/sdcard/Download/scoopz_temp")

# Clipboard behavior: auto-copy captions one-by-one to build clipboard history.
# Note: Android only has ONE active clipboard item; this relies on your
# keyboard/clipboard manager keeping a history of recent copies.
CLIPBOARD_AUTO_CYCLE      = True
CLIPBOARD_CYCLE_DELAY_SEC = 3.0
CLIPBOARD_COPY_RETRIES    = 3
CLIPBOARD_RETRY_DELAY_SEC = 0.8

MAX_BATCH_SIZE     = 7
EXTRACTION_WORKERS = 3   # Parallel ffmpeg extractions
API_WORKERS        = 3   # Parallel OpenAI API calls (safe for GPT-4o rate limits)
NUM_FRAMES         = 4   # Extract 4 frames per video
PORT               = 8765
EXTRACT_AUDIO      = True

REQUIRE_VALID_THUMBNAIL    = True  # Hide videos that cannot produce a thumbnail.
REQUIRE_PICKER_FRAME_GATE  = os.environ.get("SCOOPZ_REQUIRE_PICKER_FRAME_GATE", "0") == "1"
THUMB_MAX_FILES            = int(os.environ.get("SCOOPZ_THUMB_MAX_FILES", "500"))
THUMB_MAX_MB               = int(os.environ.get("SCOOPZ_THUMB_MAX_MB", "120"))
THUMB_MAX_AGE_DAYS         = int(os.environ.get("SCOOPZ_THUMB_MAX_AGE_DAYS", "30"))

# Weighting: by default, prioritize spoken audio over visuals.
AUDIO_WEIGHT     = float(os.environ.get("SCOOPZ_AUDIO_WEIGHT", "0.65"))
FRAME_WEIGHT     = float(os.environ.get("SCOOPZ_FRAME_WEIGHT", "0.35"))
TRANSCRIBE_AUDIO = os.environ.get("SCOOPZ_TRANSCRIBE_AUDIO", "1") != "0"
POV_STATE_FILE   = os.path.expanduser("~/.cache/scoopz_pov.json")
# ─────────────────────────────────────────────────────────
 

@dataclass
class ExtractionResult:
    """Result from local extraction."""
    video_path: str
    video_name: str
    frames: List[str]       # Paths to extracted frames
    audio: Optional[str]    # Path to audio clip
    duration: float
    error: Optional[str] = None


@dataclass
class CaptionResult:
    """Final caption result."""
    video_name: str
    caption: str
    hashtags: str
    error: Optional[str] = None


processing_jobs: Dict[str, Dict] = {}
jobs_lock       = threading.Lock()
LAST_DISCOVERY  = {"existing": 0, "eligible": 0, "skipped": 0}


# ── Utilities ─────────────────────────────────────────────
def notify(title, msg, notif_id="scoopz"):
    try:
        subprocess.run([
            "termux-notification", "--id", notif_id,
            "--title", title, "--content", msg
        ], timeout=3, capture_output=True, check=False)
    except Exception:
        pass


def toast(msg):
    try:
        subprocess.run(["termux-toast", "-s", msg],
                       timeout=2, capture_output=True, check=False)
    except Exception:
        pass


def copy_to_clipboard(text):
    if not text:
        return False
    try:
        subprocess.run(
            ["termux-clipboard-set"],
            input=text.encode("utf-8"),
            timeout=4,
            check=True,
        )
        # Clipboard on Android can lag slightly; give it a beat.
        time.sleep(0.15)
        return True
    except Exception:
        return False


def load_api_key():
    # Prefer environment variable when provided.
    env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if env_key:
        return env_key.strip('"').strip("'")
    with open(OPENAI_KEY_FILE) as f:
        return f.read().strip().strip('"').strip("'")


def validate_api_key(api_key: str) -> Optional[str]:
    """Return None when key seems valid, else a readable error string."""
    if not api_key:
        return "API key is empty"
    if " " in api_key:
        return "API key contains spaces; check for accidental copy/paste formatting"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.get(OPENAI_MODELS_BASE, headers=headers, timeout=12)
        if resp.status_code == 401:
            return "Unauthorized (401): invalid API key, revoked key, or wrong project"
        if resp.status_code == 403:
            return "Forbidden (403): project/account does not have access"
        if resp.status_code >= 400:
            return f"OpenAI validation failed: HTTP {resp.status_code}"
        return None
    except requests.RequestException as e:
        return f"Could not validate OpenAI key: {e}"


def cleanup_temp_files(job_id: str):
    pattern = f"{job_id}_"
    try:
        for temp_file in os.listdir(TEMP_DIR):
            if temp_file.startswith(pattern):
                try:
                    os.remove(os.path.join(TEMP_DIR, temp_file))
                except Exception:
                    pass
    except Exception:
        pass


def print_batch_results(results: List[CaptionResult]):
    """Print raw captions in terminal for direct copy/paste."""
    print("\n" + "=" * 72)
    print("RAW CAPTIONS")
    print("=" * 72)
    for r in results:
        print("")
        if r.error:
            print(f"Error: {r.error}")
            continue
        print(r.caption)
        print(r.hashtags)
    print("\n" + "=" * 72 + "\n")


def build_raw_captions_text(results: List[CaptionResult]) -> str:
    """Build raw caption blocks for single clipboard copy."""
    blocks = []
    for r in results:
        if r.error:
            continue
        block = f"{r.caption}\n\n{r.hashtags}".strip()
        if block:
            blocks.append(block)
    return "\n\n\n".join(blocks)


def format_single_caption_for_clipboard(result: CaptionResult, index: int) -> str:
    if result.error:
        return f"❌ Error (Video {index}): {result.error}".strip()
    return f"{result.caption}\n\n{result.hashtags}".strip()


def _copy_with_retries(text: str) -> bool:
    for attempt in range(1, CLIPBOARD_COPY_RETRIES + 1):
        if copy_to_clipboard(text):
            return True
        if attempt < CLIPBOARD_COPY_RETRIES:
            time.sleep(CLIPBOARD_RETRY_DELAY_SEC)
    return False


def auto_cycle_clipboard(job_id: str, results: List[CaptionResult]) -> Dict[str, object]:
    """Copy captions one-by-one to clipboard with a delay.
    Intended to populate Android clipboard history.
    """
    total          = len(results)
    copied         = 0
    failed_indices: List[int] = []

    for i, r in enumerate(results, 1):
        text = format_single_caption_for_clipboard(r, i)
        ok   = _copy_with_retries(text)
        if not ok:
            failed_indices.append(i)
            toast(f"Copy failed {i}/{total}")
        else:
            copied += 1
            toast(f"Copied {i}/{total}")

        with jobs_lock:
            if job_id in processing_jobs:
                processing_jobs[job_id]["clipboard_copied"] = copied
                processing_jobs[job_id]["clipboard_failed"] = len(failed_indices)

        # Give clipboard history time to register each entry.
        time.sleep(CLIPBOARD_CYCLE_DELAY_SEC)

    if failed_indices:
        notify(
            "Batch Complete (Partial)",
            f"Copied {copied}/{total}. Failed: {','.join(str(x) for x in failed_indices)}",
            "scoopz_batch",
        )
    else:
        notify("Batch Complete", f"Copied {total}/{total} captions to clipboard", "scoopz_batch")

    return {
        "copied": copied,
        "failed": len(failed_indices),
        "failed_indices": failed_indices,
    }


# ── POV Rotation ──────────────────────────────────────────
def load_pov_state():
    try:
        with open(POV_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_pov_state(state):
    try:
        os.makedirs(os.path.dirname(POV_STATE_FILE), exist_ok=True)
        with open(POV_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


_pov_lock = threading.Lock()


def _mode_to_label(mode: str) -> str:
    mode = (mode or "third").lower()
    if mode == "second":
        return "SECOND PERSON"
    if mode == "first_observer":
        return "FIRST PERSON (OBSERVER)"
    return "THIRD PERSON"


def allocate_pov_modes(count: int) -> List[str]:
    """Allocate the next N POV modes atomically (safe with parallel captioning)."""
    if count <= 0:
        return []
    with _pov_lock:
        state = load_pov_state()
        last  = state.get("last", "third").lower()
        modes = ["third", "second", "first_observer"]
        try:
            idx = modes.index(last)
        except ValueError:
            idx = 0
        allocated = []
        for _ in range(count):
            idx = (idx + 1) % len(modes)
            allocated.append(modes[idx])
        state["last"] = allocated[-1]
        save_pov_state(state)
    return [_mode_to_label(m) for m in allocated]


def next_pov_mode() -> str:
    modes = allocate_pov_modes(1)
    return modes[0] if modes else "THIRD PERSON"


# ── Video Discovery ───────────────────────────────────────
def prune_thumbnail_cache(existing_videos: List[str]):
    """Prune thumbnail cache by source existence, age, count, and total size."""
    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
        now        = time.time()
        keep_names = {os.path.basename(v) + ".jpg" for v in existing_videos}
        entries    = [
            os.path.join(THUMB_DIR, name)
            for name in os.listdir(THUMB_DIR)
            if os.path.isfile(os.path.join(THUMB_DIR, name))
        ]
        max_age_sec = max(0, THUMB_MAX_AGE_DAYS) * 86400
        for path in list(entries):
            try:
                name    = os.path.basename(path)
                too_old = max_age_sec > 0 and (now - os.path.getmtime(path)) > max_age_sec
                if (name not in keep_names) or too_old:
                    os.remove(path)
            except Exception:
                pass

        entries = [
            os.path.join(THUMB_DIR, name)
            for name in os.listdir(THUMB_DIR)
            if os.path.isfile(os.path.join(THUMB_DIR, name))
        ]
        entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)

        max_bytes = max(1, THUMB_MAX_MB) * 1024 * 1024
        total     = 0
        for idx, path in enumerate(entries):
            try:
                size = os.path.getsize(path)
            except Exception:
                continue
            if idx >= max(1, THUMB_MAX_FILES) or (total + size) > max_bytes:
                try:
                    os.remove(path)
                except Exception:
                    pass
            else:
                total += size
    except Exception:
        pass


def load_videos() -> List[str]:
    try:
        files    = [
            os.path.join(SCOOPZ_DIR, f)
            for f in os.listdir(SCOOPZ_DIR)
            if f.lower().endswith(".mp4") and not f.startswith(".")
        ]
        existing = [f for f in files if os.path.exists(f)]
        existing.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        prune_thumbnail_cache(existing)

        # Filter out black/blank/corrupt videos: zero duration or very small size (<32KB)
        valid_videos = []
        skipped = 0
        for f in existing:
            try:
                size_ok = os.path.getsize(f) > 32 * 1024
                duration_ok = get_video_duration(f) > 0.5
                if size_ok and duration_ok:
                    valid_videos.append(f)
                else:
                    skipped += 1
            except Exception:
                skipped += 1

        LAST_DISCOVERY["existing"] = len(existing)
        LAST_DISCOVERY["eligible"] = 0
        LAST_DISCOVERY["skipped"]  = skipped

        if not REQUIRE_VALID_THUMBNAIL:
            LAST_DISCOVERY["eligible"] = len(valid_videos)
            return valid_videos

        filtered = []
        thumb_skipped = 0
        for video_path in valid_videos:
            thumb    = generate_thumbnail(video_path)
            frame_ok = True
            if REQUIRE_PICKER_FRAME_GATE:
                frame_ok = can_extract_picker_frames(video_path)
            if thumb and frame_ok:
                filtered.append(video_path)
            else:
                thumb_skipped += 1

        if skipped or thumb_skipped:
            label = "thumbnail/frame extraction" if REQUIRE_PICKER_FRAME_GATE else "thumbnail"
            print(f"⚠ Skipped {skipped} videos with bad file (size/duration) and {thumb_skipped} with no valid {label}")

        LAST_DISCOVERY["eligible"] = len(filtered)
        LAST_DISCOVERY["skipped"]  = skipped + thumb_skipped
        return filtered

    except Exception:
        LAST_DISCOVERY.update({"existing": 0, "eligible": 0, "skipped": 0})
        return []


def get_video_duration(video_path: str) -> float:
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ], capture_output=True, text=True, timeout=10)
        return float(result.stdout.strip())
    except Exception:
        return 0.0


# ── Thumbnail Generation ──────────────────────────────────
def generate_thumbnail(video_path: str) -> Optional[str]:
    os.makedirs(THUMB_DIR, exist_ok=True)
    name       = os.path.basename(video_path)
    thumb_path = os.path.join(THUMB_DIR, name + ".jpg")

    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
        return thumb_path

    for seek in ["0.1", "0.5", "1", "2"]:
        try:
            subprocess.run([
                "ffmpeg", "-nostdin", "-y",
                "-ss", seek,
                "-i", video_path,
                "-vframes", "1",
                "-q:v", "3",
                "-vf", "scale=320:-1",
                thumb_path,
            ], capture_output=True, timeout=20, check=True)
            if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
                return thumb_path
        except Exception:
            pass
    return None


def can_extract_picker_frames(video_path: str) -> bool:
    """Quick ffmpeg frame extraction gate for picker eligibility."""
    duration = get_video_duration(video_path)
    if duration > 0:
        test_points = [
            max(0.1, duration * 0.15),
            max(0.5, duration * 0.35),
            max(1.0, duration * 0.65),
            max(1.5, duration * 0.85),
        ]
    else:
        test_points = [0.1, 0.5, 1.0, 2.0]

    for seek_time in test_points:
        try:
            result = subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error",
                "-ss", str(seek_time),
                "-i", video_path,
                "-vframes", "1",
                "-f", "null", "-",
            ], capture_output=True, timeout=12, check=False)
            if result.returncode == 0:
                return True
        except Exception:
            pass
    return False


def get_thumb_b64(video_path: str) -> str:
    thumb_path = generate_thumbnail(video_path)
    if thumb_path:
        try:
            with open(thumb_path, "rb") as f:
                return base64.b64encode(f.read()).decode()
        except Exception:
            pass
    return ""


# ── OPTIMIZED EXTRACTION (4 frames) ───────────────────────
def extract_video_assets(video_path: str, job_id: str, video_idx: int) -> ExtractionResult:
    """
    Extract 4 frames at key moments + optional audio clip.
    Frame positions: 15%, 35%, 65%, 85% for better coverage.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)
    name     = os.path.basename(video_path)
    duration = get_video_duration(video_path)

    frame_paths = [
        os.path.join(TEMP_DIR, f"{job_id}_{video_idx}_frame{i}.jpg")
        for i in range(NUM_FRAMES)
    ]
    audio_path       = os.path.join(TEMP_DIR, f"{job_id}_{video_idx}_audio.mp3")
    frame_positions  = [0.15, 0.35, 0.65, 0.85]

    def extract_frame(seek_time: float, output_path: str) -> bool:
        try:
            subprocess.run([
                "ffmpeg", "-nostdin", "-y",
                "-ss", str(seek_time),
                "-i", video_path,
                "-vframes", "1",
                "-q:v", "2",
                "-vf", "scale=800:-1",
                output_path,
            ], capture_output=True, timeout=15, check=True)
            return os.path.exists(output_path) and os.path.getsize(output_path) > 500
        except Exception:
            return False

    def extract_audio_clip(output_path: str) -> bool:
        try:
            subprocess.run([
                "ffmpeg", "-nostdin", "-y",
                "-ss", "2",
                "-i", video_path,
                "-t", "15",
                "-vn",
                "-ar", "16000",
                "-ac", "1",
                "-b:a", "32k",
                output_path,
            ], capture_output=True, timeout=30, check=True)
            return os.path.exists(output_path) and os.path.getsize(output_path) > 500
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=NUM_FRAMES + 1) as executor:
        futures: Dict = {}
        for i, pos in enumerate(frame_positions):
            seek_time = max(0.5, duration * pos)
            futures[executor.submit(extract_frame, seek_time, frame_paths[i])] = f"frame{i}"
        if EXTRACT_AUDIO:
            futures[executor.submit(extract_audio_clip, audio_path)] = "audio"

        results: Dict[str, bool] = {}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as e:
                print(f"Error extracting {key} for {name}: {e}")
                results[key] = False

    successful_frames = [
        frame_paths[i] for i in range(NUM_FRAMES)
        if results.get(f"frame{i}")
    ]
    audio_ok = audio_path if (EXTRACT_AUDIO and results.get("audio")) else None

    if not successful_frames:
        return ExtractionResult(
            video_path=video_path,
            video_name=name,
            frames=[],
            audio=None,
            duration=duration,
            error="Failed to extract frames",
        )

    print(f"✓ Extracted {len(successful_frames)}/{NUM_FRAMES} frames for {name}")
    return ExtractionResult(
        video_path=video_path,
        video_name=name,
        frames=successful_frames,
        audio=audio_ok,
        duration=duration,
    )


# ── Caption Prompt ─────────────────────────────────────────
# NOTE: Hashtag enforcement is handled entirely by normalize_hashtags().
# The prompt only asks the model for raw hashtag suggestions — the Python
# layer overrides the final mix, so duplicating the recipe here causes drift.
CAPTION_PROMPT = """Write a HIGH-CONVERSION caption based on these {num_frames} video frames.

POV: {pov_mode}

SIGNAL WEIGHTS:
- Audio/transcript weight: {audio_weight}
- Visual frames weight: {frame_weight}
- Default: prioritize spoken words, claims, arguments, admissions, and key quotes from transcript.
- If transcript is mostly music/noise/empty/unclear, prioritize visuals instead.
- If audio is background music, use it only for mood (tone/energy), not factual scene details.

STYLE MODE: {style_mode}

CONTEXT: You're seeing {num_frames} frames across the video (beginning, early-mid, late-mid, end).
Use progression only to infer tension/reveals; never mention frame order explicitly.

GOAL:
- Ground it in ONE specific detail from THIS video — an exact action, quote, object, or reaction visible or heard.
- Make it universally relatable — a judgment, question, or dare that anyone scrolling can instantly react to.
- It should feel like something a real person texted their friend, not a content writer.

REQUIREMENTS:
- No more than 130 characters total
- 1-2 sentences.
- 2-3 emojis total (distributed naturally, never clustered at the end)
- Must hook instantly (conflict, accusation, shock, stakes, or challenge)and  escalate tension and force a reaction (question, judgment, or dare)
- Include at least one specific anchor detail (exact phrase/object/action) that clearly ties caption to this video
- Strongly evoke at least one: outrage, suspicion, disbelief, urgency, or moral tension
- Use pattern interruption — reframe the obvious in an unexpected way (see examples below)
- Ensure any names mentioned in the transcipt or visible in frames are included in the caption for maximum specificity and searchability.
- Do NOT narrate calmly or describe scenery like a recap

PATTERN INTERRUPTION EXAMPLES:
- Instead of "This dad is amazing" → "Nobody warned him about the tiny dictator 👑"
- Instead of "She caught him lying" → "He really thought the smile would save him 💀"
- Instead of "These dogs are funny" → "These dogs have a better commute than me 😭"
- Instead of "He's manifesting wealth" → "Manifesting a BMW while his knees file for divorce"
Use this energy — subvert the expected read, make the viewer feel slightly caught off guard.

STYLE:
- Raw, human, slightly uncomfortable
- Not formal, not news-like
- Avoid generic placeholders ("this guy", "some dude", "a woman")
- Use concrete high-stakes language, not neutral observation
- Warm confrontation for family/kids content — provocative but never mean
- Skeptical/sarcastic for finance or hustle content — not angry, just knowing
- Full chaos energy for animal or accident content — lean into absurdity
- Can include relatable/funny/sarcastic/chaotic edge when it increases shareability

MODE RULES:
- drama_conflict:
    DO: direct clash, accusation, betrayal, confrontation, power imbalance
    DON'T: vague tension ("something went wrong"), soft drama ("they disagreed")
    TONE: sharp, decisive, takes a side

- mystery_curiosity:
    DO: unanswered question, hidden detail, suspicious mismatch, "why is nobody talking about"
    DON'T: open-ended confusion that goes nowhere ("what even is this?")
    TONE: conspiratorial, slightly unsettled, pulls the viewer in

- outrage_opinion:
    DO: hot take, moral judgment, accountability, "pick a side" framing
    DON'T: both-sides neutrality, calm observation, fence-sitting
    TONE: confident, slightly provocative, demands a response

AVOID THESE PHRASES:
- "by frame 4", "in frame 1", "frame 2", "this shot"
- "starts calm, ends...", "by the end", "in this clip"
- Soft scenic lead-ins like "morning mist..." unless tied to immediate tension
- "this is wild", "you won't believe", "wait for it" — overused, skip them

HASHTAGS:
- Generate 7 relevant hashtags for this video. Avoid platform tags (no #tiktok, #instagram, etc.).
- Mix reaction tags (emotion/curiosity/energy) with content-specific tags tied to this video's topic.

OUTPUT FORMAT:
CAPTION:
[your caption here]

HASHTAGS:
[hashtags here]"""

CAPTION_STYLE_MODES = [
    "drama_conflict",
    "mystery_curiosity",
    "outrage_opinion",
]


# ── Hashtag Normalization ──────────────────────────────────
def _extract_hashtags(text: str) -> List[str]:
    return re.findall(r"#([a-z0-9_]+)", (text or "").lower())


PLATFORM_TAGS = {
    "tiktok", "instagram", "reels", "youtube", "shorts", "facebook", "snapchat",
}

GENERAL_VIRAL_POOL = [
    "wtfmoment", "awkward", "watch", "reaction", "relatable", "breakingnews", "local",
    "funny" , "messy", "unreal", "mindblown", "chaosmode", "unexpected", "outofpocket", "wildenergy"
]

MANDATORY_GENERAL_TAGS = ["scoopz", "funny", "trending","scoopzfirst"]


GENERAL_HINT_TOKENS = {
    "wtf", "wild", "crazy", "awkward", "watch", "reaction", "chaos", "messy", "relatable",
}

# Keyword → video-specific tag mapping
_KEYWORD_TAG_MAP = [
    ("court",     "courtdrama"),
    ("judge",     "courtdrama"),
    ("police",    "policeinvestigation"),
    ("officer",   "policeinvestigation"),
    ("press",     "pressconference"),
    ("conference","pressconference"),
    ("public",    "publicreaction"),
    ("argument",  "heatedargument"),
    ("fight",     "heatedargument"),
    ("confront",  "confrontation"),
    ("accuse",    "accusation"),
    ("scam",      "scamalert"),
    ("fraud",     "scamalert"),
    ("arrest",    "arrestupdate"),
    ("lawyer",    "legaldrama"),
]

_STOPWORDS = {
    "the", "and", "for", "that", "with", "this", "from", "your", "you", "they",
    "have", "just", "what", "when", "were", "been", "about", "into", "over", "then",
    "them", "their", "there", "would", "could", "should", "video", "caption",
}

_TYPO_MAP = {
    "localanews": "localnews",
}


def _derive_video_specific_tags(caption: str, transcript: str) -> List[str]:
    text = f"{caption or ''} {transcript or ''}".lower()
    tags: List[str] = []

    for key, tag in _KEYWORD_TAG_MAP:
        if key in text and tag not in tags:
            tags.append(tag)

    # Fallback: synthesize topic tags from meaningful words in caption/transcript.
    if len(tags) < 3:
        words = re.findall(r"[a-z]{4,}", text)
        for w in words:
            if w in _STOPWORDS:
                continue
            if any(w in t for t in GENERAL_VIRAL_POOL):
                continue
            tag = re.sub(r"[^a-z0-9_]", "", w)
            if tag and tag not in tags:
                tags.append(tag)
            if len(tags) >= 3:
                break

    return tags[:3]


def _is_general_viral_tag(tag: str) -> bool:
    return tag in GENERAL_VIRAL_POOL or any(tok in tag for tok in GENERAL_HINT_TOKENS)


def _looks_like_breaking_story(caption: str, transcript: str) -> bool:
    """Heuristic: mark breaking only when explicit urgency signals are present."""
    combined = f"{caption or ''} {transcript or ''}".lower()
    triggers = [
        "breaking", "just in", "developing", "urgent",
        "right now", "live now", "confirmed", "official statement",
    ]
    return any(t in combined for t in triggers)


def normalize_hashtags(hashtags_text: str, caption: str, transcript: str) -> str:
    """Enforce strict mix: 4 mandatory general + 2 reaction + 1 video-specific (max 7).

    This function is the single source of truth for hashtag composition.
    The prompt intentionally asks for loose suggestions only; enforcement lives here.
    """
    extracted: List[str] = []
    for tag in _extract_hashtags(hashtags_text):
        tag = _TYPO_MAP.get(tag, tag)
        if tag not in PLATFORM_TAGS and tag and tag not in extracted:
            extracted.append(tag)

    video_specific = [t for t in extracted if not _is_general_viral_tag(t)]
    reaction_tags  = []

    # Derive video-specific tags if none came from the model.
    if not video_specific:
        for t in _derive_video_specific_tags(caption, transcript):
            if t not in video_specific:
                video_specific.append(t)
            if video_specific:
                break

    # Deterministic rotation to avoid repeating the exact same viral set.
    seed  = sum(ord(c) for c in f"{caption}|{transcript}")
    pool  = GENERAL_VIRAL_POOL[:]
    shift = seed % len(pool) if pool else 0
    pool  = pool[shift:] + pool[:shift]



    if _looks_like_breaking_story(caption, transcript) and not video_specific:
        video_specific.append("breakingnews")

    # Build exact recipe: 4 mandatory general + up to 3 video-specific (max 7).
    final = MANDATORY_GENERAL_TAGS[:] + video_specific[:3]
    deduped: List[str] = []
    for t in final:
        if t and t not in deduped and t not in PLATFORM_TAGS:
            deduped.append(t)



    # Guarantee at least one video-specific tag.
    if not any(not _is_general_viral_tag(t) and t not in MANDATORY_GENERAL_TAGS for t in deduped):
        fallback = video_specific[0] if video_specific else "localnews"
        if fallback not in deduped and fallback not in PLATFORM_TAGS:
            deduped.append(fallback)

    return " ".join(f"#{t}" for t in deduped[:7])


# ── Audio Utilities ────────────────────────────────────────
def _looks_like_music_or_noise(transcript: str) -> bool:
    t = (transcript or "").strip().lower()
    if not t:
        return True
    markers = ["[music]", "(music)", "instrumental", "beat drop", "la la", "humming"]
    if any(m in t for m in markers):
        return True
    words = re.findall(r"[a-zA-Z']+", t)
    if len(words) < 4:
        return True
    alpha_ratio = sum(ch.isalpha() for ch in t) / max(1, len(t))
    return alpha_ratio < 0.45


def transcribe_audio_openai(api_key: str, audio_path: Optional[str]) -> str:
    if not audio_path or not os.path.exists(audio_path):
        return ""
    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        with open(audio_path, "rb") as af:
            files = {"file": (os.path.basename(audio_path), af, "audio/mpeg")}
            data  = {"model": "gpt-4o-mini-transcribe"}
            resp  = requests.post(
                OPENAI_TRANSCRIBE_BASE,
                headers=headers,
                files=files,
                data=data,
                timeout=45,
            )
        if resp.status_code in (401, 403):
            print(f"Transcription auth error {resp.status_code}; falling back to visual-only")
            return ""
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()
    except Exception as e:
        print(f"Transcription failed: {e}")
        return ""


# ── Caption Generation ─────────────────────────────────────
def _clean_caption(raw: str) -> str:
    """Strip frame labels and chronology references that leaked from the model."""
    out = re.sub(r"\[(Beginning|Early-mid|Late-mid|End|Frame \d+)\]\s*", "", raw, flags=re.IGNORECASE)
    out = re.sub(r"\b(in|by)\s+(frame|shot)\s*\d+\b", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\b(frame|shot)\s*\d+\b", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\b(by the end|starts calm, ends|in this clip)\b", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,.-")
    return out


def generate_caption_openai(
    api_key: str,
    frames: List[str],
    audio: Optional[str],
    pov_mode: str,
    transcript: str = "",
    style_mode: str = "drama_conflict",
) -> Dict[str, str]:
    """Generate caption using OpenAI Vision API with multiple frames."""
    content = []

    position_labels = ["Beginning", "Early-mid", "Late-mid", "End"]
    for i, frame_path in enumerate(frames):
        with open(frame_path, "rb") as f:
            b64_image = base64.b64encode(f.read()).decode()
        label = position_labels[i] if i < len(position_labels) else f"Frame {i + 1}"
        content.append({"type": "text", "text": f"[{label}]"})
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{b64_image}",
                "detail": "high",
            },
        })

    # Adjust weights when audio is not usable speech.
    music_or_noise       = _looks_like_music_or_noise(transcript)
    effective_audio_w    = 0.20 if music_or_noise else AUDIO_WEIGHT
    effective_frame_w    = 0.80 if music_or_noise else FRAME_WEIGHT

    prompt = CAPTION_PROMPT.format(
        num_frames=len(frames),
        pov_mode=pov_mode,
        audio_weight=f"{effective_audio_w:.2f}",
        frame_weight=f"{effective_frame_w:.2f}",
        style_mode=style_mode,
    )

    if transcript and not music_or_noise:
        content.append({
            "type": "text",
            "text": f"Transcript (prioritize if speech is meaningful): {transcript}",
        })
    elif transcript and music_or_noise:
        content.append({
            "type": "text",
            "text": "Audio appears to be mostly background music/noise. Use it only as mood; do not infer scene facts from it.",
        })

    content.append({"type": "text", "text": prompt})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 500,
        "temperature": 0.9,
    }

    for attempt in range(3):
        try:
            response = requests.post(OPENAI_BASE, headers=headers, json=payload, timeout=35)

            if response.status_code in (401, 403):
                raise Exception(
                    f"Auth error {response.status_code}. Check OPENAI_API_KEY or {OPENAI_KEY_FILE}"
                )
            if response.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"Rate limit hit, waiting {wait}s...")
                time.sleep(wait)
                continue

            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"].strip()

            caption_match  = re.search(r"CAPTION:\s*(.+?)(?=HASHTAGS?:|$)", text, re.DOTALL | re.IGNORECASE)
            hashtags_match = re.search(r"HASHTAGS?:\s*(.+?)$", text, re.DOTALL | re.IGNORECASE)

            caption  = _clean_caption(caption_match.group(1).strip() if caption_match else "")
            hashtags = re.sub(r"\s+", " ", hashtags_match.group(1).strip() if hashtags_match else "").strip()

            return {"caption": caption, "hashtags": hashtags}

        except Exception as e:
            print(f"Caption generation attempt {attempt + 1} failed: {e}")
            if attempt == 2:
                raise
            time.sleep(2)

    raise Exception("Failed to generate caption after 3 attempts")


# ── Batch Processing ───────────────────────────────────────
def get_default_caption_and_hashtags():
    # Universal, catchy fallback caption and hashtags
    caption = "Somebody just changed the story 😳 What would you do next?"
    hashtags = "#scoopz #trending #scoopzfirst #funny #wtfmoment #unexpected #localnews"
    return caption, hashtags

def process_batch(job_id: str, video_paths: List[str], api_key: str):
    """Process batch with parallel extraction AND parallel API calls."""
    try:
        with jobs_lock:
            processing_jobs[job_id] = {
                "status": "extracting",
                "total": len(video_paths),
                "completed": 0,
                "results": [],
                "error": None,
            }

        # ── Phase 1: Extract assets ──────────────────────────
        print(f"\n📹 Phase 1: Extracting {len(video_paths)} videos (4 frames each)...")
        extraction_start = time.time()

        extraction_results: List[Optional[ExtractionResult]] = [None] * len(video_paths)

        with ThreadPoolExecutor(max_workers=EXTRACTION_WORKERS) as executor:
            futures = {
                executor.submit(extract_video_assets, vpath, job_id, i): i
                for i, vpath in enumerate(video_paths)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    extraction_results[idx] = future.result()
                except Exception as e:
                    print(f"Extraction failed for video {idx}: {e}")
                    extraction_results[idx] = ExtractionResult(
                        video_path=video_paths[idx],
                        video_name=os.path.basename(video_paths[idx]),
                        frames=[],
                        audio=None,
                        duration=0,
                        error=str(e),
                    )
                with jobs_lock:
                    processing_jobs[job_id]["completed"] = sum(
                        1 for r in extraction_results if r is not None
                    )

        extraction_time = time.time() - extraction_start
        print(f"✓ Extraction complete: {extraction_time:.1f}s")

        # ── Phase 2: Generate captions ───────────────────────
        print(f"\n🤖 Phase 2: Generating captions (parallel, {API_WORKERS} workers)...")
        generation_start = time.time()

        with jobs_lock:
            processing_jobs[job_id]["status"]    = "generating"
            processing_jobs[job_id]["completed"] = 0

        final_results: List[Optional[CaptionResult]] = [None] * len(extraction_results)
        pov_modes = allocate_pov_modes(len(extraction_results))


        def generate_single_caption(idx: int, extraction: Optional[ExtractionResult]):
            if extraction is None:
                # Use default caption for missing extraction
                caption, hashtags = get_default_caption_and_hashtags()
                return idx, CaptionResult(video_name="", caption=caption, hashtags=hashtags, error=None)
            if extraction.error:
                # Use default caption for extraction error
                caption, hashtags = get_default_caption_and_hashtags()
                return idx, CaptionResult(video_name=extraction.video_name, caption=caption, hashtags=hashtags, error=None)

            pov_mode   = pov_modes[idx] if idx < len(pov_modes) else "THIRD PERSON"
            style_mode = CAPTION_STYLE_MODES[idx % len(CAPTION_STYLE_MODES)]
            transcript = transcribe_audio_openai(api_key, extraction.audio) if TRANSCRIBE_AUDIO else ""

            try:
                caption_data = generate_caption_openai(
                    api_key=api_key,
                    frames=extraction.frames,
                    audio=extraction.audio,
                    pov_mode=pov_mode,
                    transcript=transcript,
                    style_mode=style_mode,
                )
                caption_data["hashtags"] = normalize_hashtags(
                    caption_data.get("hashtags", ""),
                    caption_data.get("caption", ""),
                    transcript,
                )
                print(f"  ✓ Caption generated for {extraction.video_name}")
                return idx, CaptionResult(
                    video_name=extraction.video_name,
                    caption=caption_data["caption"],
                    hashtags=caption_data["hashtags"],
                )
            except Exception as e:
                print(f"  ✗ Caption generation failed for {extraction.video_name}: {e}")
                # Use default caption for generation error
                caption, hashtags = get_default_caption_and_hashtags()
                return idx, CaptionResult(
                    video_name=extraction.video_name,
                    caption=caption,
                    hashtags=hashtags,
                    error=None,
                )

        with ThreadPoolExecutor(max_workers=API_WORKERS) as executor:
            futures = {
                executor.submit(generate_single_caption, i, ext): i
                for i, ext in enumerate(extraction_results)
            }
            for future in as_completed(futures):
                try:
                    idx, result = future.result()
                    final_results[idx] = result
                except Exception as e:
                    idx = futures.get(future)
                    print(f"Unexpected error in caption generation: {e}")
                    if idx is not None and final_results[idx] is None:
                        vname = ""
                        if idx < len(extraction_results) and extraction_results[idx]:
                            vname = extraction_results[idx].video_name
                        final_results[idx] = CaptionResult(
                            video_name=vname, caption="", hashtags="", error=str(e)
                        )
                with jobs_lock:
                    processing_jobs[job_id]["completed"] = sum(
                        1 for r in final_results if r is not None
                    )

        generation_time = time.time() - generation_start
        print(f"✓ Caption generation complete: {generation_time:.1f}s")

        total_time = extraction_time + generation_time
        print(f"\n🎯 Total time: {total_time:.1f}s ({total_time / len(video_paths):.1f}s per video)")

        # Ensure no Nones remain in final_results.
        safe_results: List[CaptionResult] = []
        for i, r in enumerate(final_results):
            if r is None:
                vname = ""
                if i < len(extraction_results) and extraction_results[i]:
                    vname = extraction_results[i].video_name
                r = CaptionResult(video_name=vname, caption="", hashtags="", error="Missing caption result")
            safe_results.append(r)

        with jobs_lock:
            processing_jobs[job_id]["status"]          = "done"
            processing_jobs[job_id]["results"]         = [asdict(r) for r in safe_results]
            processing_jobs[job_id]["clipboard_total"] = len(safe_results)

        print_batch_results(safe_results)

        if CLIPBOARD_AUTO_CYCLE:
            notify("Batch Complete", f"Copying {len(safe_results)} captions to clipboard...", "scoopz_batch")
            auto_cycle_clipboard(job_id, safe_results)
        else:
            raw_text = build_raw_captions_text(safe_results)
            copied   = copy_to_clipboard(raw_text) if raw_text else False
            if copied:
                notify("Batch Complete", f"{len(safe_results)} captions printed + copied", "scoopz_batch")
                toast("Captions copied to clipboard")
            else:
                notify("Batch Complete", f"{len(safe_results)} captions printed in terminal", "scoopz_batch")
                toast(f"{len(safe_results)} captions printed")

    except Exception as e:
        print(f"Batch processing error: {e}")
        with jobs_lock:
            processing_jobs[job_id]["status"] = "error"
            processing_jobs[job_id]["error"]  = str(e)

    finally:
        cleanup_temp_files(job_id)


def format_results_for_clipboard(results: List[CaptionResult]) -> str:
    if len(results) == 1:
        r = results[0]
        return f"Error: {r.error}" if r.error else f"{r.caption}\n\n{r.hashtags}"

    output = []
    for i, r in enumerate(results, 1):
        output.append(f"Video {i}: {r.video_name}")
        output.append(f"Error: {r.error}" if r.error else f"{r.caption}\n{r.hashtags}")
        output.append("")
    return "\n".join(output)


# ── Web UI ─────────────────────────────────────────────────
def build_html(videos: List[str]) -> str:
    cards = ""
    for i, vpath in enumerate(videos):
        try:
            b64 = get_thumb_b64(vpath)
            if not b64:
                continue
            size_mb = os.path.getsize(vpath) / (1024 * 1024)
            name    = os.path.basename(vpath)
            cards  += f"""
            <div class="card" data-index="{i}" onclick="toggleSelect({i})">
                <div class="check" id="check-{i}">✓</div>
                <img src='data:image/jpeg;base64,{b64}' loading='lazy' alt='{name}'>
                <div class="label">{size_mb:.1f}MB</div>
            </div>"""
        except Exception:
            pass

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scoopz Batch</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0a0a0a;font-family:sans-serif;padding-bottom:80px}}
  header{{background:#111;padding:16px 20px;border-bottom:1px solid #222;position:sticky;top:0;z-index:10}}
  header h1{{color:#fff;font-size:15px;font-weight:600}}
  header span{{color:#666;font-size:12px;display:block;margin-top:2px}}
  .stats{{color:#00ff88;font-size:11px;margin-top:4px}}
  .grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:3px;padding:3px}}
  .card{{position:relative;aspect-ratio:9/16;background:#1a1a1a;cursor:pointer;overflow:hidden;border-radius:4px;transition:all 0.2s}}
  .card.selected{{border:3px solid #00ff88}}
  .card:active{{transform:scale(0.95)}}
  .check{{position:absolute;top:8px;right:8px;width:28px;height:28px;border-radius:50%;background:rgba(0,0,0,0.7);color:#fff;display:none;align-items:center;justify-content:center;font-size:18px;font-weight:bold;z-index:5}}
  .card.selected .check{{display:flex;background:#00ff88;color:#000}}
  .card img{{width:100%;height:100%;object-fit:cover}}
  .label{{position:absolute;bottom:0;left:0;right:0;background:linear-gradient(transparent,rgba(0,0,0,0.8));color:#ccc;font-size:10px;padding:12px 6px 5px;text-align:right}}
  .fab{{position:fixed;bottom:20px;right:20px;width:64px;height:64px;border-radius:50%;background:#00ff88;color:#000;border:none;font-size:18px;font-weight:bold;box-shadow:0 4px 12px rgba(0,0,0,0.5);cursor:pointer;display:none;align-items:center;justify-content:center;z-index:20}}
  .fab.show{{display:flex}}
  .fab:active{{transform:scale(0.9)}}
  .counter{{position:absolute;top:-8px;right:-8px;width:24px;height:24px;border-radius:50%;background:#ff0066;color:#fff;font-size:12px;display:flex;align-items:center;justify-content:center}}
  .overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.95);z-index:100;align-items:center;justify-content:center;flex-direction:column;gap:16px}}
  .overlay.show{{display:flex}}
  .spinner{{width:48px;height:48px;border:4px solid #333;border-top-color:#00ff88;border-radius:50%;animation:spin 0.7s linear infinite}}
  .msg{{color:#fff;font-size:16px;font-weight:600}}
  .detail{{color:#aaa;font-size:13px}}
  .progress{{color:#00ff88;font-size:14px;margin-top:8px}}
  @keyframes spin{{to{{transform:rotate(360deg)}}}}
</style>
</head>
<body>
<header>
  <h1>Scoopz Batch</h1>
  <span>{len(videos)} videos · tap to select (max {MAX_BATCH_SIZE})</span>
  <div class="stats">4 frames per video · ~6s per video · parallel processing</div>
</header>
<div class="grid">{cards}</div>
<button class="fab" id="fab" onclick="processSelected()">
  <span class="counter" id="counter">0</span>
  GO
</button>
<div class="overlay" id="overlay">
  <div class="spinner"></div>
  <p class="msg" id="msg">Processing...</p>
  <p class="detail" id="detail"></p>
  <p class="progress" id="progress"></p>
</div>
<script>
const selected = new Set();
const fab      = document.getElementById('fab');
const counter  = document.getElementById('counter');
const overlay  = document.getElementById('overlay');
const msg      = document.getElementById('msg');
const detail   = document.getElementById('detail');
const progress = document.getElementById('progress');

function toggleSelect(i) {{
  const card = document.querySelector(`[data-index="${{i}}"]`);
  if (selected.has(i)) {{
    selected.delete(i);
    card.classList.remove('selected');
  }} else {{
    if (selected.size >= {MAX_BATCH_SIZE}) {{
      alert('Max {MAX_BATCH_SIZE} videos');
      return;
    }}
    selected.add(i);
    card.classList.add('selected');
  }}
  counter.textContent = selected.size;
  fab.classList.toggle('show', selected.size > 0);
}}

async function processSelected() {{
  if (selected.size === 0) return;
  const indices = Array.from(selected).sort((a, b) => a - b);
  overlay.classList.add('show');
  msg.textContent    = 'Starting...';
  detail.textContent = `Processing ${{indices.length}} videos`;
  progress.textContent = '';

  try {{
    const startResp = await fetch('/start_batch', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{indices}})
    }});
    if (!startResp.ok) throw new Error('Failed to start');
    const {{job_id}} = await startResp.json();

    for (let tries = 0; tries < 300; tries++) {{
      const statusResp = await fetch(`/job_status?job_id=${{job_id}}`);
      const status     = await statusResp.json();

      if (status.status === 'extracting') {{
        msg.textContent      = 'Extracting (4 frames each)...';
        progress.textContent = `${{status.completed}}/${{status.total}}`;
      }} else if (status.status === 'generating') {{
        msg.textContent      = 'Generating captions...';
        progress.textContent = `${{status.completed}}/${{status.total}}`;
      }} else if (status.status === 'done') {{
        msg.textContent      = 'Done!';
        const total          = status.clipboard_total ?? 0;
        detail.textContent   = total > 0 ? `${{total}} captions printed in terminal.` : 'No captions generated';
        progress.textContent = '';
        setTimeout(() => {{
          overlay.classList.remove('show');
          selected.clear();
          document.querySelectorAll('.card').forEach(c => c.classList.remove('selected'));
          counter.textContent = 0;
          fab.classList.remove('show');
        }}, 2000);
        return;
      }} else if (status.status === 'error') {{
        msg.textContent = 'Error';
        detail.textContent = status.error || 'Unknown error';
        setTimeout(() => overlay.classList.remove('show'), 3000);
        return;
      }}
      await new Promise(r => setTimeout(r, 1000));
    }}
    msg.textContent = 'Timeout';
    setTimeout(() => overlay.classList.remove('show'), 2500);
  }} catch (e) {{
    msg.textContent    = 'Error';
    detail.textContent = e.message;
    setTimeout(() => overlay.classList.remove('show'), 2500);
  }}
}}
</script>
</body>
</html>"""


# ── HTTP Server ────────────────────────────────────────────
class ReusableServer(HTTPServer):
    allow_reuse_address = True

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except Exception:
            pass
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    videos:   List[str] = []
    api_key: Optional[str] = None

    def log_message(self, *args):
        pass  # Suppress default HTTP access logs

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                body = build_html(self.videos).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)

            elif parsed.path == "/job_status":
                qs     = parse_qs(parsed.query)
                job_id = qs.get("job_id", [""])[0]
                with jobs_lock:
                    job = processing_jobs.get(job_id, {"status": "unknown"})
                self._send_json(200, job)

            else:
                self.send_response(404)
                self.end_headers()

        except Exception:
            self.send_response(500)
            self.end_headers()

    def do_POST(self):
        try:
            if self.path == "/start_batch":
                length      = int(self.headers["Content-Length"])
                data        = json.loads(self.rfile.read(length))
                indices     = data.get("indices", []) if isinstance(data, dict) else data

                if not indices or len(indices) > MAX_BATCH_SIZE:
                    self.send_response(400)
                    self.end_headers()
                    return

                video_paths = [self.videos[i] for i in indices if i < len(self.videos)]
                job_id      = f"job_{int(time.time())}_{len(video_paths)}"

                threading.Thread(
                    target=process_batch,
                    args=(job_id, video_paths, self.api_key),
                    daemon=True,
                ).start()

                self._send_json(200, {"job_id": job_id})

            else:
                self.send_response(404)
                self.end_headers()

        except Exception:
            self.send_response(500)
            self.end_headers()


# ── Port Management ────────────────────────────────────────
def _cmd_exists(cmd: str) -> bool:
    try:
        subprocess.run(["sh", "-lc", f"command -v {cmd}"],
                       capture_output=True, timeout=2, check=False)
        return True
    except Exception:
        return False


def _kill_pids(pids: List[int]):
    for pid in pids:
        try:
            os.kill(pid, 15)
        except Exception:
            pass
    time.sleep(0.3)
    for pid in pids:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def free_port(port: int) -> bool:
    """Best-effort: kill any process listening on localhost:port."""
    pids: List[int] = []

    if not pids and _cmd_exists("lsof"):
        try:
            out = subprocess.run(
                ["sh", "-lc", f"lsof -tiTCP:{port} -sTCP:LISTEN"],
                capture_output=True, text=True, timeout=3, check=False,
            ).stdout.strip()
            pids = [int(x) for x in out.split() if x.isdigit()]
        except Exception:
            pass

    if not pids and _cmd_exists("ss"):
        try:
            out = subprocess.run(
                ["sh", "-lc", f"ss -lptn 'sport = :{port}'"],
                capture_output=True, text=True, timeout=3, check=False,
            ).stdout
            for m in re.finditer(r"pid=(\d+)", out):
                pids.append(int(m.group(1)))
        except Exception:
            pass

    if not pids and _cmd_exists("netstat"):
        try:
            out = subprocess.run(
                ["sh", "-lc", f"netstat -ltnp 2>/dev/null | grep :{port}"],
                capture_output=True, text=True, timeout=3, check=False,
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if parts:
                    last = parts[-1]
                    if "/" in last:
                        pid_str = last.split("/", 1)[0]
                        if pid_str.isdigit():
                            pids.append(int(pid_str))
        except Exception:
            pass

    if pids:
        _kill_pids(sorted(set(pids)))
        return True

    # Last resort
    if _cmd_exists("fuser"):
        try:
            subprocess.run(
                ["sh", "-lc", f"fuser -k {port}/tcp"],
                capture_output=True, timeout=3, check=False,
            )
            return True
        except Exception:
            pass

    return False


# ── Main ──────────────────────────────────────────────────
def main():
    # Load & validate API key
    try:
        api_key = load_api_key()
    except FileNotFoundError:
        notify("Error", f"No API key at {OPENAI_KEY_FILE}", "scoopz_error")
        print(f"ERROR: Create {OPENAI_KEY_FILE} with your OpenAI API key")
        print("Tip: you can also set the OPENAI_API_KEY environment variable.")
        sys.exit(1)

    key_error = validate_api_key(api_key)
    if key_error:
        notify("Error", "OpenAI key validation failed", "scoopz_error")
        print(f"ERROR: {key_error}")
        print(f"Checked env OPENAI_API_KEY, then file: {OPENAI_KEY_FILE}")
        sys.exit(1)

    # Discover videos
    videos = load_videos()
    if not videos:
        toast("No videos found!")
        if LAST_DISCOVERY["existing"] > 0:
            print(
                f"Found {LAST_DISCOVERY['existing']} MP4 files in {SCOOPZ_DIR}, "
                f"but 0 passed the thumbnail/frame filter."
            )
        else:
            print(f"No MP4 files in {SCOOPZ_DIR}")
        sys.exit(1)

    print(f"📼 Found {len(videos)} videos")
    os.makedirs(TEMP_DIR, exist_ok=True)

    Handler.videos  = videos
    Handler.api_key = api_key

    # Bind server (retry once after freeing the port)
    srv = None
    for attempt in range(2):
        try:
            srv = ReusableServer(("127.0.0.1", PORT), Handler)
            break
        except OSError as e:
            if getattr(e, "errno", None) == 98 or "Address already in use" in str(e):
                print(f"Port {PORT} in use, attempting to free it...")
                free_port(PORT)
                time.sleep(0.3)
            else:
                raise

    if srv is None:
        print(f"ERROR: Could not bind to port {PORT}")
        sys.exit(1)

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.5)

    url = f"http://127.0.0.1:{PORT}"

    # Open in browser
    try:
        subprocess.run(
            ["am", "start", "-a", "android.intent.action.VIEW", "-d", url],
            timeout=4, check=False,
        )
    except Exception:
        pass

    # Persistent notification
    try:
        subprocess.run([
            "termux-notification",
            "--id", "scoopz_batch",
            "--title", "Scoopz Batch (4 Frames)",
            "--content", "Tap to open · ~6s per video",
            "--button1", "Open",
            "--button1-action", f"termux-open-url {url}",
            "--ongoing",
        ], timeout=3, check=False)
    except Exception:
        pass

    print(f"\n✓ Server running at {url}")
    print(f"✓ 4 frames per video · Parallel API calls")
    print(f"✓ Target: ~6s per video (~50s for 8 videos)")
    print("\nPress Ctrl+C to stop\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()