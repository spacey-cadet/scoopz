#!/usr/bin/env python3
"""
NewsBreak Caption Generator
Stack: ffmpeg (parallel) → 2 frames + audio clip → single Gemini call → caption
Target: 8-15s per video
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
import requests
import secrets
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── Config ───────────────────────────────────────────────
KEY_FILE     = os.path.expanduser("~/.config/gemini_key.txt")
GEMINI_MODEL = "gemini-2.5-flash-lite"  # FIXED: Use stable model
GEMINI_BASE  = "https://generativelanguage.googleapis.com/v1beta/models"

# Persist POV alternation across runs (2nd person ↔ 3rd person)
POV_STATE_FILE = os.path.expanduser("~/.cache/scoopz_caption_pov.json")
USER_EXAMPLES_FILE = os.path.expanduser("~/.config/scoopz_caption_examples.txt")

# Optional: persist micro-style memory to reduce repetition across runs.
STYLE_MEMORY_FILE = os.path.expanduser("~/.cache/scoopz_caption_style_memory.json")

SCOOPZ_DIR   = "/sdcard/Pictures/scoopz"
THUMB_DIR    = "/sdcard/Pictures/scoopz/.thumbs"

TEMP_FRAME_1 = "/sdcard/Download/nb_frame1.jpg"
TEMP_FRAME_2 = "/sdcard/Download/nb_frame2.jpg"
TEMP_AUDIO   = "/sdcard/Download/nb_audio.mp3"

PORT         = 8765
# ─────────────────────────────────────────────────────────

selected_video  = None
caption_ready   = threading.Event()
processing_lock = threading.Lock()

# Live processing state for UI/debugging
processing_stage  = "idle"   # idle | queued | extracting | generating | done | error
processing_detail = ""


def set_processing_stage(stage: str, detail: str = ""):
    global processing_stage, processing_detail
    with processing_lock:
        processing_stage = stage
        processing_detail = detail

# ── Prompt ───────────────────────────────────────────────
CAPTION_PROMPT_TEMPLATE = """Write a punchy, scroll-stopping caption based on the video frames and audio.

POV: {POV_MODE}

REQUIREMENTS:
- ~170–190 characters total
- Start with a specific visual detail (not generic)
- Build tension or curiosity
- End with a strong pull (question, judgment, or challenge)

STYLE:
- Raw, human, slightly uncomfortable
- Not formal, not news-like
- Avoid generic phrasing

CAPTION STRUCTURE (strict):
You must write exactly 2 sentences.

Sentence 1:
- Must contain exactly 1 emoji somewhere in the middle (not at the end)

Sentence 2:
- Must contain 1–2 emojis naturally within the sentence

Total emojis per caption: 2 or 3 (mandatory)

If the caption does not contain emojis in BOTH sentences, it is invalid and must be rewritten internally before returning.

HASHTAGS:
- 4–5 lowercase tags
- Include ONE of: #breakingnews or #localnews
- Avoid platform/app/tok/ig derivative tags

OUTPUT:
Return 3 different candidates in this format:

VALIDATION (internal, do not skip):
Before returning each candidate, verify:
1. There are exactly 2 sentences
2. Sentence 1 contains at least 1 emoji
3. Sentence 2 contains at least 1 emoji
4. Total emojis = 2 or 3

If ANY condition fails, rewrite the caption until all are satisfied.

CANDIDATE 1:
CAPTION:
...
HASHTAGS:
...

CANDIDATE 2:
CAPTION:
...
HASHTAGS:
...

CANDIDATE 3:
CAPTION:
...
HASHTAGS:
..."""


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def _write_json_atomic(path: str, data: dict) -> None:
    # Best-effort atomic write (works on Android/Linux).
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.replace(tmp, path)
    except:
        try:
            os.rename(tmp, path)
        except:
            pass


def next_pov_mode() -> str:
    """Rotate POV across captions: THIRD → SECOND → FIRST(OBSERVER) → ..."""
    state = _read_json(POV_STATE_FILE)
    last = (state.get("last") or "third").strip().lower()

    order = ["third", "second", "first_observer"]
    try:
        idx = order.index(last)
    except ValueError:
        idx = 0
    nxt = order[(idx + 1) % len(order)]

    state["last"] = nxt
    _write_json_atomic(POV_STATE_FILE, state)

    if nxt == "second":
        return "SECOND PERSON"
    if nxt == "first_observer":
        return "FIRST PERSON (OBSERVER)"
    return "THIRD PERSON"


def build_caption_prompt(pov_mode: str) -> str:
    prompt = CAPTION_PROMPT_TEMPLATE.format(POV_MODE=pov_mode)

    # Optional user-provided examples to steer style. One caption per line.
    # Lines starting with '#' are comments.
    try:
        examples = []
        if os.path.exists(USER_EXAMPLES_FILE):
            with open(USER_EXAMPLES_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    s = (line or "").strip()
                    if not s or s.startswith("#"):
                        continue
                    # Avoid injecting hashtags into the examples section.
                    s = re.sub(r"#\w+", "", s).strip()
                    s = s.replace('"', "'")
                    if not s:
                        continue
                    examples.append(s)
                    if len(examples) >= 6:
                        break

        if examples:
            prompt += "\n\nADDITIONAL STYLE EXAMPLES (do NOT copy wording; match the pattern):\n"
            prompt += "\n".join([f"- \"{e}\"" for e in examples])
    except:
        pass

    return prompt

# ── Utilities ─────────────────────────────────────────────
def notify(title, msg, notif_id="nb_status"):
    try:
        subprocess.run([
            "termux-notification", "--id", notif_id,
            "--title", title, "--content", msg
        ], timeout=3, capture_output=True, check=False)
    except:
        pass

def toast(msg):
    try:
        subprocess.run(["termux-toast", "-s", msg],
                       timeout=2, capture_output=True, check=False)
    except:
        pass

def open_browser(url):
    """Try multiple launch methods."""
    print(f"\n>>> Open in browser: {url}\n")

    for pkg in ["com.android.chrome", "org.mozilla.firefox",
                "com.brave.browser", "com.opera.browser"]:
        try:
            r = subprocess.run([
                "am", "start", "-a", "android.intent.action.VIEW", "-d", url,
                "--activity-brought-to-front",
            ], timeout=4, capture_output=True, check=False)
            if r.returncode == 0:
                return True
        except:
            pass

    try:
        r = subprocess.run([
            "am", "start", "-a", "android.intent.action.VIEW", "-d", url
        ], timeout=4, capture_output=True, check=False)
        if r.returncode == 0:
            return True
    except:
        pass

    try:
        subprocess.run(["termux-open-url", url], timeout=4, capture_output=True)
        return True
    except:
        pass

    return False

def copy_to_clipboard(text):
    expected = (text or "").strip()
    if not expected:
        return False

    def _normalize(s: str) -> str:
        s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
        # Termux clipboard can sometimes normalize emoji presentation (VS16/ZWJ).
        s = s.replace("\ufe0f", "").replace("\u200d", "")
        return re.sub(r"\s+", " ", s.strip())

    last_err = ""
    for attempt in range(5):
        try:
            r = subprocess.run(
                ["termux-clipboard-set"],
                input=expected.encode("utf-8"),
                timeout=4,
                capture_output=True,
                check=False,
            )
            if r.returncode != 0:
                last_err = (r.stderr or r.stdout or b"")[:120].decode("utf-8", "ignore")
            else:
                # Verify if possible (best-effort). Don't fail UI on flaky reads.
                try:
                    ok = False
                    for delay in (0.0, 0.12, 0.25):
                        if delay:
                            time.sleep(delay)
                        g = subprocess.run(
                            ["termux-clipboard-get"],
                            timeout=4,
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        if g.returncode != 0:
                            continue
                        if _normalize(g.stdout) == _normalize(expected):
                            ok = True
                            break

                    if not ok:
                        # Soft warning only: set succeeded, but read-back didn't match.
                        print("⚠️ Clipboard verify mismatch (set likely succeeded)")
                    return True
                except FileNotFoundError:
                    return True
                except Exception as e:
                    # Soft warning only.
                    print(f"⚠️ Clipboard verify failed (set likely succeeded): {str(e)[:80]}")
                    return True
        except FileNotFoundError:
            last_err = "termux-clipboard-set not found"
            break
        except Exception as e:
            last_err = str(e)[:80]

        time.sleep(0.25 + attempt * 0.15)

    print(f"⚠️ Clipboard copy failed: {last_err}")
    return False

def load_api_key():
    with open(KEY_FILE) as f:
        return f.read().strip()

def kill_port(port):
    import glob
    try:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.close()
        return
    except OSError:
        pass
    try:
        port_hex = format(port, "04X")
        inode = None
        for tcp_file in ["/proc/net/tcp", "/proc/net/tcp6"]:
            try:
                with open(tcp_file) as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) > 9 and parts[1].endswith(":" + port_hex):
                            inode = parts[9]
                            break
            except:
                pass
            if inode:
                break
        if inode:
            for fd_path in glob.glob("/proc/*/fd/*"):
                try:
                    link = os.readlink(fd_path)
                    if f"socket:[{inode}]" in link:
                        os.kill(int(fd_path.split("/")[2]), 9)
                        break
                except:
                    pass
    except:
        pass
    time.sleep(0.5)

def cleanup():
    for f in [TEMP_FRAME_1, TEMP_FRAME_2, TEMP_AUDIO]:
        try:
            if os.path.exists(f):
                os.remove(f)
        except:
            pass

def file_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

# ── Video utils ───────────────────────────────────────────
def get_duration(video_path):
    try:
        r = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ], capture_output=True, timeout=10, text=True)
        return float(r.stdout.strip())
    except:
        return 30.0

def is_video_readable(video_path):
    """Fast sanity check that ffmpeg/ffprobe can decode the file."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "csv=p=0",
                video_path,
            ],
            capture_output=True,
            timeout=8,
            text=True,
            check=False,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except:
        return False

def can_extract_frame(video_path):
    """Stricter check: ffmpeg can actually decode at least one video frame."""
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error",
                "-i", video_path,
                "-map", "0:v:0",
                "-frames:v", "1",
                "-f", "null", "-",
            ],
            capture_output=True,
            timeout=10,
            text=True,
            check=False,
        )
        return r.returncode == 0
    except:
        return False

# ── Video loader (MediaStore-filtered) ───────────────────
def mediastore_paths():
    """Get all video paths Android's MediaStore knows about."""
    try:
        r = subprocess.run([
            "content", "query",
            "--uri", "content://media/external/video/media",
            "--projection", "_data",
        ], capture_output=True, timeout=10, text=True, check=False)
        paths = set()
        for line in r.stdout.splitlines():
            if "_data=" in line:
                path = line.split("_data=", 1)[1].strip()
                paths.add(path)
        return paths
    except:
        return set()

def load_videos():
    """
    Return videos from SCOOPZ_DIR that:
    1. Exist as files
    2. Are in MediaStore (not deleted from Google Photos)
    """
    all_files = [
        os.path.join(SCOOPZ_DIR, f)
        for f in os.listdir(SCOOPZ_DIR)
        if f.lower().endswith(".mp4") and not f.startswith(".")
    ]
    
    # Filter: only files that actually exist
    existing = [v for v in all_files if os.path.exists(v)]
    
    # Filter: only files MediaStore knows about
    known = mediastore_paths()
    
    if not known:
        # MediaStore unavailable - show all existing files
        filtered = existing
    else:
        # Only show files that are in MediaStore (not deleted from gallery)
        filtered = [v for v in existing if v in known]
    
    # Sort by modification time, newest first
    filtered.sort(key=os.path.getmtime, reverse=True)
    
    return filtered

# ── Thumbnails (Strict Filtering) ─────────────────────────
def generate_thumbnails(videos):
    """
    Attempts to generate thumbnails.
    If a video fails (corrupted/trashed), it is skipped and removed from the list.
    """
    os.makedirs(THUMB_DIR, exist_ok=True)
    valid_names = {os.path.basename(v) for v in videos}

    try:
        for thumb in os.listdir(THUMB_DIR):
            if thumb.endswith(".jpg"):
                source_name = thumb[:-4]
                if source_name not in valid_names:
                    try:
                        os.remove(os.path.join(THUMB_DIR, thumb))
                    except:
                        pass
    except:
        pass

    total = len(videos)
    readable_videos = []

    for idx, vpath in enumerate(videos):
        if idx % 5 == 0:
            toast(f"Loading {idx}/{total}...")

        name       = os.path.basename(vpath)
        thumb_path = os.path.join(THUMB_DIR, name + ".jpg")

        # Strict Filter: if the file isn't decodable, drop it (and any stale thumbnail).
        if not is_video_readable(vpath):
            try:
                if os.path.exists(thumb_path):
                    os.remove(thumb_path)
            except:
                pass
            print(f"⚠️  Skipped corrupted or unreadable file: {name}")
            continue

        # Strict Filter: if ffmpeg can't extract even 1 frame, it's not usable.
        if not can_extract_frame(vpath):
            try:
                if os.path.exists(thumb_path):
                    os.remove(thumb_path)
            except:
                pass
            print(f"⚠️  Skipped corrupted or unreadable file: {name}")
            continue

        # Check if good thumbnail already exists
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
            readable_videos.append(vpath)
            continue

        # Avoid stale thumbnails causing false positives when ffmpeg fails
        try:
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
        except:
            pass

        success = False

        # Strategy 1: seek 1s
        if not success:
            try:
                subprocess.run([
                    "ffmpeg", "-nostdin", "-y", "-ss", "1", "-i", vpath,
                    "-vframes", "1", "-q:v", "3", "-vf", "scale=320:-1",
                    thumb_path
                ], capture_output=True, timeout=15, check=False)
                success = os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500
            except:
                pass

        # Strategy 2: seek to 10% duration
        if not success:
            try:
                seek = max(0.5, get_duration(vpath) * 0.1)
                subprocess.run([
                    "ffmpeg", "-nostdin", "-y", "-ss", str(seek), "-i", vpath,
                    "-vframes", "1", "-q:v", "3", "-vf", "scale=320:-1",
                    thumb_path
                ], capture_output=True, timeout=15, check=False)
                success = os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500
            except:
                pass

        # Strategy 3: first frame
        if not success:
            try:
                subprocess.run([
                    "ffmpeg", "-nostdin", "-y", "-i", vpath,
                    "-vframes", "1", "-q:v", "5", thumb_path
                ], capture_output=True, timeout=15, check=False)
                success = os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500
            except:
                pass

        # Strict Filter: If all 3 methods failed, skip the file.
        if success:
            readable_videos.append(vpath)
        else:
            print(f"⚠️  Skipped corrupted or unreadable file: {name}")

    toast("Ready")
    return readable_videos

def get_thumb_b64(vpath):
    thumb = os.path.join(THUMB_DIR, os.path.basename(vpath) + ".jpg")
    try:
        if os.path.exists(thumb):
            with open(thumb, "rb") as f:
                return base64.b64encode(f.read()).decode()
    except:
        pass
    return ""

# ── Extraction (parallel) ─────────────────────────────────
def _extract_frames(vpath, duration):
    """Extract 2 frames at 10% and 50% duration."""
    def _extract_one_frame(seek, out_path):
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except:
            pass
        try:
            subprocess.run([
                "ffmpeg", "-nostdin", "-y", "-ss", str(seek), "-i", vpath,
                "-vframes", "1", "-q:v", "3", "-vf", "scale=640:-1",
                out_path
            ], capture_output=True, timeout=15, check=False)
        except:
            pass
        return os.path.exists(out_path) and os.path.getsize(out_path) > 500

    def _extract_first_frame(out_path):
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except:
            pass
        try:
            subprocess.run([
                "ffmpeg", "-nostdin", "-y", "-i", vpath,
                "-vframes", "1", "-q:v", "5", "-vf", "scale=640:-1",
                out_path
            ], capture_output=True, timeout=15, check=False)
        except:
            pass
        return os.path.exists(out_path) and os.path.getsize(out_path) > 500

    for seek, out in [
        (max(0.5, duration * 0.10), TEMP_FRAME_1),
        (max(1.0, duration * 0.50), TEMP_FRAME_2),
    ]:
        ok = _extract_one_frame(seek, out)
        if not ok:
            _extract_first_frame(out)

def _extract_audio(vpath):
    """Extract mono mp3 at 32kbps."""
    if os.path.exists(TEMP_AUDIO):
        os.remove(TEMP_AUDIO)
    try:
        subprocess.run([
            "ffmpeg", "-nostdin", "-y", "-i", vpath,
            "-vn", "-ar", "16000", "-ac", "1", "-b:a", "32k",
            TEMP_AUDIO
        ], capture_output=True, timeout=30, check=False)
    except:
        pass

def extract_parallel(vpath):
    """Run frame + audio extraction simultaneously."""
    duration = get_duration(vpath)

    t_frames = threading.Thread(target=_extract_frames, args=(vpath, duration))
    t_audio  = threading.Thread(target=_extract_audio,  args=(vpath,))
    t_frames.start()
    t_audio.start()
    t_frames.join()
    t_audio.join()

    frames   = [p for p in [TEMP_FRAME_1, TEMP_FRAME_2]
                if os.path.exists(p) and os.path.getsize(p) > 500]
    audio_ok = os.path.exists(TEMP_AUDIO) and os.path.getsize(TEMP_AUDIO) > 500

    return frames, audio_ok

# ── Gemini ────────────────────────────────────────────────
def generate_caption(api_key, frame_paths, audio_ok, prompt_text: str):
    """Single Gemini multimodal call with frames + audio."""
    parts = []

    for fp in frame_paths:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": file_b64(fp)
            }
        })

    if audio_ok:
        parts.append({
            "inline_data": {
                "mime_type": "audio/mpeg",
                "data": file_b64(TEMP_AUDIO)
            }
        })

    parts.append({"text": prompt_text})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 1.0,
            "maxOutputTokens": 900
        }
    }

    url     = GEMINI_BASE + "/" + GEMINI_MODEL + ":generateContent"
    headers = {
        "Content-Type":    "application/json",
        "x-goog-api-key":  api_key,
    }

    for attempt in range(3):
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code == 429:
            time.sleep(10)
            continue
        if not resp.ok:
            print(f"Gemini error {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    raise ValueError("Gemini rate limit")

def format_result(raw, pov_mode: str = ""):
    """Parse CAPTION + HASHTAGS and sanitize output."""

    # Keep sanitation aligned with prompt target (~170–190 chars).
    _CAPTION_MAX_CHARS = 200

    # Compiled once per call to avoid duplicated patterns in helpers.
    _EMOJI_RE = re.compile(
        r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u26FF\u2700-\u27BF]",
        flags=re.UNICODE,
    )
    _SKIN_TONE_RE = re.compile(r"[\U0001F3FB-\U0001F3FF]", flags=re.UNICODE)

    def _extract_emojis(text: str, *, limit: int = 8) -> list[str]:
        """Best-effort emoji extraction for repetition tracking (single codepoints only)."""
        found = _EMOJI_RE.findall(text or "")
        out: list[str] = []
        seen: set[str] = set()
        for e in found:
            if e in seen:
                continue
            seen.add(e)
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def _split_sentences(text: str) -> list[str]:
        text = (text or "").strip()
        if not text:
            return []
        # Best-effort sentence splitter.
        parts = re.split(r"(?<=[.!?])\s+", text)
        return [p.strip() for p in parts if p.strip()]

    def _contains_collective_first_person(text: str) -> bool:
        fp = re.compile(r"\b(we|we're|were|our|ours|us)\b", flags=re.IGNORECASE)
        return bool(fp.search(text or ""))

    def _repair_collective_first_person(text: str) -> str:
        # Keep it simple: convert collective to third-person to avoid implying participation.
        replacements = [
            (r"\bwe['’]re\b", "they're"),
            (r"\bwe\b", "they"),
            (r"\bour\b", "their"),
            (r"\bours\b", "theirs"),
            (r"\bus\b", "them"),
        ]
        out = text or ""
        for pat, rep in replacements:
            out = re.sub(pat, rep, out, flags=re.IGNORECASE)
        out = re.sub(r"^\s*they\b", "They", out, flags=re.IGNORECASE)
        out = re.sub(r"\s{2,}", " ", out).strip()
        return out

    def _contains_participant_first_person(text: str) -> bool:
        # Heuristic: flags phrases that sound like the writer is involved, not observing.
        bad = re.compile(
            r"\b(i\s+(?:did|made|built|stabbed|hit|robbed|shot|ran|pulled\s+up|walked\s+up|came\s+out|was\s+there|we\s+(?:did|were|are)\b))",
            flags=re.IGNORECASE,
        )
        return bool(bad.search(text or ""))

    def _trim_to_max_chars(text: str, max_chars: int) -> str:
        text = (text or "").strip()
        if len(text) <= max_chars:
            return text
        trimmed = text[:max_chars].rsplit(" ", 1)[0].strip()
        if not trimmed:
            trimmed = text[:max_chars].strip()
        # Prefer ending on a sentence boundary if we have one late in the string.
        last_end = max(trimmed.rfind("."), trimmed.rfind("!"), trimmed.rfind("?"))
        if last_end >= 0 and last_end >= int(len(trimmed) * 0.65):
            trimmed = trimmed[: last_end + 1].strip()
        return trimmed

    def _count_emojis(text: str) -> int:
        # Best-effort emoji count (kept intentionally simple).
        return len(_EMOJI_RE.findall(text or ""))

    def _get_int_env(name: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(os.environ.get(name, str(default)).strip())
            return max(lo, min(hi, v))
        except:
            return default

    EMOJI_MAX = _get_int_env("SCOOPZ_EMOJI_MAX", 3, 1, 6)

    STYLE_MEMORY_ON = (os.environ.get("SCOOPZ_STYLE_MEMORY", "1").strip() not in ("0", "false", "False"))
    SCORING_DEBUG = (os.environ.get("SCOOPZ_SCORING_DEBUG", "0").strip() not in ("0", "false", "False"))

    def _limit_emojis(text: str, max_emojis: int) -> str:
        kept = []
        emoji_count = 0
        for ch in (text or ""):
            # Keep joiners/modifiers/variation selectors so we don't break emoji sequences.
            if ch in ("\u200d", "\ufe0f") or _SKIN_TONE_RE.match(ch):
                kept.append(ch)
                continue
            if _EMOJI_RE.match(ch):
                if emoji_count >= max_emojis:
                    continue
                emoji_count += 1
            kept.append(ch)
        return "".join(kept)

    def _enforce_emoji_quota(
        caption: str,
        *,
        min_emojis: int = 0,
        max_emojis: int = 3,
        max_len: int = 220,
    ) -> str:
        caption = caption.strip()

        # De-script: strip common "scripted" lead-ins if present.
        caption = re.sub(
            r"^\s*(?:nah\b|this is (?:crazy|wild)\b|can\s*'?t\s+believe\b)\s*[,:.!-]*\s*",
            "",
            caption,
            flags=re.IGNORECASE,
        )
        caption = re.sub(r"^\s*[—\-–]+\s*", "", caption).strip()
        if caption:
            caption = caption[0].upper() + caption[1:]

        caption = _trim_to_max_chars(caption, max_len)

        # If too many emojis, drop extras.
        caption = _limit_emojis(caption, max_emojis)

        # Re-apply emoji cap + length guard after edits.
        caption = _limit_emojis(caption, max_emojis)
        caption = _trim_to_max_chars(caption, max_len)
        return caption

    def _load_style_memory() -> dict:
        if not STYLE_MEMORY_ON:
            return {}
        return _read_json(STYLE_MEMORY_FILE) or {}

    def _save_style_memory(mem: dict) -> None:
        if not STYLE_MEMORY_ON:
            return
        _write_json_atomic(STYLE_MEMORY_FILE, mem or {})

    def _normalize_caption_for_memory(text: str) -> str:
        s = (text or "").strip().lower()
        s = s.replace("\ufe0f", "").replace("\u200d", "")
        s = re.sub(r"\s+", " ", s)
        return s

    def _extract_signature_phrases(text: str) -> list[str]:
        tl = (text or "").lower()
        phrases = []
        # Key implication/tension phrases that can feel repetitive.
        for p in [
            "and that’s when it got weird",
            "and that's when it got weird",
            "something about this isn’t adding up",
            "something about this isn't adding up",
            "the part people are missing is",
            "you can feel something shift here",
            "this doesn’t sit right",
            "this doesn't sit right",
            "that pause says everything",
            "it feels a little too quiet",
            "you staying or turning back",
        ]:
            if p in tl:
                phrases.append(p)
        # Emoji signatures (whatever emojis the model actually used).
        phrases.extend(_extract_emojis(text, limit=3))
        return phrases[:8]

    def _looks_flat(text: str) -> bool:
        text = (text or "").strip().lower()
        if not text:
            return True
        # Flat if it's too literal, no tension/implication, no punch.
        implication = [
            "got weird",
            "isn't adding up",
            "people are missing",
            "could go left",
            "something shift",
            "doesn't sit right",
            "quiet that just got",
            "seen this go wrong",
        ]
        has_implication = any(p in text for p in implication)
        has_punch = ("—" in text) or ("..." in text) or ("?" in text) or ("!" in text)
        return (not has_implication) and (not has_punch)

    def _inject_boost(caption: str, pov_mode: str) -> str:
        """Local, zero-API rewrite pass: add one implication + one tension phrase if caption feels flat."""
        caption = (caption or "").strip()
        if not caption:
            return caption

        if not _looks_flat(caption):
            return caption

        pattern_breakers = [
            "and nobody’s reacting like they should",
            "that’s not a normal reaction",
            "this part doesn’t make sense",
            "something feels off here",
        ]
        implication_phrases = [
            "and that’s when it got weird",
            "something about this isn’t adding up",
            "the part people are missing is…",
            "you can feel something shift here",
            "this could go left fast",
        ]
        tension_phrases = [
            "This doesn’t sit right.",
            "That pause says everything.",
            "It feels a little too quiet.",
            "That’s not normal.",
        ]

        sents = _split_sentences(caption)
        if not sents:
            return caption

        first = sents[0]
        second = sents[1] if len(sents) > 1 else ""

        # Add 1 pattern-breaker or implication to the first sentence if missing.
        if not re.search(r"got weird|isn['’]t adding up|people are missing|shift here|go left|feels off|doesn['’]t make sense|not a normal reaction|reacting", caption, flags=re.IGNORECASE):
            try:
                add = secrets.choice(pattern_breakers + implication_phrases)
            except Exception:
                add = pattern_breakers[0]
            first = (first.rstrip(".!?") + ", " + add + ".").strip()

        # Add 1 tension sentence if we still have room.
        rebuilt = (first + (" " + second if second else "")).strip()
        rebuilt = _trim_to_max_chars(rebuilt, _CAPTION_MAX_CHARS)

        if len(_split_sentences(rebuilt)) < 2:
            # Ensure 2 sentences by adding a short tension line.
            try:
                rebuilt = (rebuilt.rstrip() + " " + secrets.choice(tension_phrases)).strip()
            except Exception:
                rebuilt = (rebuilt.rstrip() + " " + tension_phrases[0]).strip()

        # POV-specific nudge: second-person questions should be slightly confrontational.
        if "SECOND" in (pov_mode or "") and "?" not in rebuilt:
            confront = [
                "You're staying for THIS?",
                "You're telling me you'd stay?",
                "Be honest—you're not walking away, right?",
            ]
            try:
                rebuilt = (rebuilt.rstrip(".! ") + " " + secrets.choice(confront)).strip()
            except Exception:
                rebuilt = (rebuilt.rstrip(".! ") + " " + confront[0]).strip()

        rebuilt = _trim_to_max_chars(rebuilt, _CAPTION_MAX_CHARS)
        return rebuilt

    def _parse_candidates(text: str) -> list[tuple[str, str]]:
        """Return list of (caption, hashtags) pairs."""
        text = (text or "").strip()
        if not text:
            return []

        # Split on explicit candidate headers if present.
        chunks = re.split(r"(?im)^\s*CANDIDATE\s+\d+\s*:\s*$", text)
        if len(chunks) <= 1:
            chunks = [text]

        out: list[tuple[str, str]] = []
        for chunk in chunks:
            if "CAPTION:" not in chunk or "HASHTAGS:" not in chunk:
                continue
            cap = chunk.split("HASHTAGS:")[0].replace("CAPTION:", "").strip()
            tag = chunk.split("HASHTAGS:", 1)[1].strip().split("\n")[0].strip()
            out.append((cap, tag))
        return out

    def _is_platform_derivative_hashtag(tag: str) -> bool:
        """Return True if tag is a TikTok/Instagram-derivative hashtag the user wants banned."""
        t = (tag or "").strip().lower()
        if not t.startswith("#"):
            return False
        body = t[1:]
        # Explicit TikTok anywhere (covers: #tiktok, #catsoftiktok, #cats_of_tiktok, etc.)
        if "tiktok" in body:
            return True
        # "tok" combined with something else (covers: #cattok, #wirdltok, etc.)
        # Keep conservative: only ban when it *ends* with tok (not substrings like #tokyo).
        if body.endswith("tok") and body != "tok":
            return True

        # Explicit Instagram anywhere (covers: #instagram, #instagramreels, etc.)
        if "instagram" in body:
            return True
        # Common Instagram derivatives: #insta*, #ig
        # Avoid false positives for common English words like #instant / #instantly.
        if body.startswith("insta") and body not in {"instant", "instantly"}:
            return True
        if body in {"ig", "instareels", "reels"}:
            # Note: #reels can be cross-platform; user requested Instagram derivatives,
            # and this often signals Instagram-specific posting.
            return True
        return False

    def _score_candidate(caption: str, hashtags: str, pov_mode: str) -> int:
        score = 0
        breakdown: dict[str, int] = {}
        c = (caption or "").strip()
        cl = c.lower()

        # Length/structure targets
        if 170 <= len(c) <= 200:
            score += 10; breakdown["len_good"] = 10
        elif 155 <= len(c) <= 215:
            score += 5; breakdown["len_ok"] = 5
        else:
            score -= 6; breakdown["len_off"] = -6

        sents = _split_sentences(c)
        if len(sents) == 2:
            score += 12; breakdown["two_sent"] = 12
        elif len(sents) == 1:
            score -= 6; breakdown["one_sent"] = -6
        else:
            score -= 8; breakdown["many_sent"] = -8

        # Reward casual human cadence (some contractions, not overly formulaic).
        contractions = len(re.findall(r"\b(?:don't|can't|won't|you're|isn't|aren't|didn't|doesn't|i'm|i’ve|i've|that's|there's)\b", cl))
        if contractions >= 1:
            score += 3; breakdown["contractions"] = 3

        # Penalize overly templated phrasing if multiple "prompt phrases" stack up.
        templated_hits = len(_extract_signature_phrases(c))
        if templated_hits >= 3:
            score -= 6; breakdown["templated"] = -6
        elif templated_hits == 2:
            score -= 2; breakdown["templated2"] = -2

        # Reward an "anchor detail": a proper noun or specific-looking token.
        # Heuristic: a capitalized word not at start OR a digit marker (train line, street #, etc.).
        anchor = False
        words = re.findall(r"\b[\w']+\b", c)
        for w in words[1:10]:
            if len(w) >= 3 and w[0].isupper() and w.lower() not in ("i", "im", "i'm"):
                anchor = True
                break
        if not anchor and re.search(r"\b\d{1,3}(?:st|nd|rd|th)?\b", c):
            anchor = True
        if anchor:
            score += 4; breakdown["anchor"] = 4

        # POV enforcement signals
        if "SECOND" in (pov_mode or ""):
            if "?" in c:
                score += 10; breakdown["second_q"] = 10
            # More aggressive second-person phrasing
            if any(p in cl for p in ["be honest", "you telling me", "you walking", "turning back"]):
                score += 4; breakdown["second_aggr"] = 4
            if any(p in cl for p in ["you're staying for this", "you’re staying for this", "for this?", "for this ?"]):
                score += 3; breakdown["second_confront"] = 3
        if "FIRST" in (pov_mode or ""):
            if any(p in cl for p in ["doesn't sit right", "dont like how quiet", "i've seen this go wrong", "i’m not gonna lie", "i'm not gonna lie"]):
                score += 4; breakdown["first_auth"] = 4

        # Curiosity/tension/implication
        if any(p in cl for p in ["got weird", "isn't adding up", "people are missing", "could go left", "shift here", "doesn't sit right", "too quiet"]):
            score += 9; breakdown["tension"] = 9
        if any(p in cl for p in ["until", "and then", "right after", "that pause", "the room", "vibe shifted", "went quiet"]):
            score += 3; breakdown["timing"] = 3

        # Pattern interrupt + rhythm
        if any(p in c for p in ["—", "...", ";"]):
            score += 2; breakdown["rhythm"] = 2
        if any(s.strip().startswith(("Until", "Until ", "Then", "Then ")) for s in sents):
            score += 1; breakdown["lead_word"] = 1

        # Penalize generic subject
        if any(p in cl for p in ["this dude", "this guy", "some dude"]):
            score -= 14; breakdown["generic"] = -14

        # Penalize generic openings harder (his/her/this/that at start reads like template).
        if re.match(r"^(his|her|this|that)\b", cl):
            score -= 10; breakdown["generic_open"] = -10

        # Novelty scoring: lightly penalize common overused phrases to force variety.
        common_phrases = [
            "would you",
            "this could go left",
            "got weird",
            "room got quiet",
        ]
        hit = 0
        for p in common_phrases:
            if p in cl:
                score -= 3
                hit += 1
        if hit:
            breakdown["common_phrase"] = -3 * hit

        # Safety: avoid implying participation
        if _contains_participant_first_person(c):
            score -= 12; breakdown["participation"] = -12
        if _contains_collective_first_person(c):
            score -= 8; breakdown["collective"] = -8

        # Hashtags sanity
        tags = re.findall(r"#\w+", (hashtags or "").lower())
        if 4 <= len(tags) <= 5:
            score += 4; breakdown["tags_count"] = 4
        if "#breakingnews" in tags or "#localnews" in tags:
            score += 4; breakdown["req_tag"] = 4

        # Anti-repetition across runs (optional). Prevents robotic sameness.
        try:
            mem = _load_style_memory()
            recent_caps = mem.get("recent_captions", []) or []
            recent_phr = mem.get("recent_phrases", []) or []
            recent_emo = mem.get("recent_emojis", []) or []

            norm = _normalize_caption_for_memory(c)
            if norm and norm in set(recent_caps[-20:]):
                score -= 25; breakdown["dup_caption"] = -25

            sig = _extract_signature_phrases(c)
            rep = 0
            for p in sig:
                if p in recent_phr:
                    rep += 1
            if rep:
                penalty = min(10, rep * 3)
                score -= penalty
                breakdown["repeat_phrase"] = -penalty

            # Slightly penalize reusing the same filler emojis over and over.
            emo_in = _extract_emojis(c, limit=4)
            if emo_in and any(e in set(recent_emo[-20:]) for e in emo_in):
                score -= 2; breakdown["repeat_emoji"] = -2
        except:
            pass

        if SCORING_DEBUG:
            try:
                keys = sorted(breakdown.keys())
                dbg = ", ".join([f"{k}:{breakdown[k]}" for k in keys])
                print(f"[score] {score:>4} | {dbg} | {c[:70]}")
            except:
                pass

        return score

    try:
        candidates = _parse_candidates(raw)
        if candidates:
            # Use the same sanitation logic on each candidate, then pick best.
            best_text = ""
            best_score = -10**9

            for cand_caption, cand_hashtags in candidates[:3]:
                caption = (cand_caption or "").strip()
                hashtags = (cand_hashtags or "").strip().strip("[](){}")

                # Hard guardrails: no hashtags or app-name leakage in CAPTION
                caption = re.sub(r"#\w+", "", caption)
                caption = re.sub(r"\bnewsbreak\b", "", caption, flags=re.IGNORECASE)
                caption = re.sub(r"\s+([?.!,])", r"\1", caption)
                caption = re.sub(r"\s{2,}", " ", caption).strip()

                # Prefer 2 sentences; if more, keep the first two.
                sents = _split_sentences(caption)
                if len(sents) >= 2:
                    caption = (sents[0] + " " + sents[1]).strip()
                else:
                    caption = caption.strip()

                # De-script + emoji cap, but don't force any minimum emojis.
                caption = _enforce_emoji_quota(caption, min_emojis=0, max_emojis=EMOJI_MAX, max_len=_CAPTION_MAX_CHARS)

                # Avoid implying participation: eliminate collective "we/our/us" and obvious first-person actions.
                if _contains_collective_first_person(caption):
                    caption = _repair_collective_first_person(caption)
                if _contains_participant_first_person(caption):
                    # Last-resort soften: remove the clause after the first-person trigger.
                    caption = re.sub(r"\bI\s+(?:did|made|built|stabbed|hit|robbed|shot|ran|pulled\s+up|walked\s+up|came\s+out)\b[^.!?]*", "", caption, flags=re.IGNORECASE).strip()
                    caption = re.sub(r"\s{2,}", " ", caption).strip()

                # Optional boost if the caption is flat (no extra API calls)
                caption = _inject_boost(caption, pov_mode=pov_mode)

                # Length guard: keep caption tight.
                caption = _trim_to_max_chars(caption, _CAPTION_MAX_CHARS)

                # Hashtag sanitation: drop platform/app + low-signal filler, enforce required tag
                raw_tags = re.findall(r"#\w+", hashtags.lower())
                banned_platform = {
                    "#newsbreak",
                    "#tiktok", "#instagram", "#youtube", "#facebook", "#snapchat",
                    "#twitter", "#x", "#reddit", "#discord", "#telegram",
                    "#newsbreakapp", "#fyp", "#viral", "#trending", "#foryou",
                }
                low_signal = {"#news", "#breaking", "#update", "#alert", "#live"}

                non_banned = []
                seen_non_banned = set()
                for tag in raw_tags:
                    if _is_platform_derivative_hashtag(tag):
                        continue
                    if tag in banned_platform:
                        continue
                    if tag not in seen_non_banned:
                        seen_non_banned.add(tag)
                        non_banned.append(tag)

                clean_tags = []
                seen = set()
                for tag in non_banned:
                    if tag in low_signal:
                        continue
                    if tag not in seen:
                        seen.add(tag)
                        clean_tags.append(tag)

                if "#breakingnews" not in seen and "#localnews" not in seen:
                    clean_tags.append("#localnews")
                    seen.add("#localnews")

                clean_tags = clean_tags[:5]
                hashtags = " ".join(clean_tags).strip()

                # Score and pick.
                s = _score_candidate(caption, hashtags, pov_mode=pov_mode)
                if s > best_score:
                    best_score = s
                    best_text = (f"{caption}\n\n{hashtags}".strip() if hashtags else caption.strip())

            # Update style memory with the selected caption to reduce repetition next run.
            if best_text:
                try:
                    mem = _load_style_memory()
                    mem.setdefault("recent_captions", [])
                    mem.setdefault("recent_phrases", [])
                    mem.setdefault("recent_emojis", [])

                    chosen_caption = best_text.split("\n\n", 1)[0].strip()
                    norm = _normalize_caption_for_memory(chosen_caption)
                    if norm:
                        mem["recent_captions"].append(norm)
                        mem["recent_captions"] = mem["recent_captions"][-30:]

                    sig = _extract_signature_phrases(chosen_caption)
                    if sig:
                        mem["recent_phrases"].extend(sig)
                        mem["recent_phrases"] = mem["recent_phrases"][-80:]

                    emo = _extract_emojis(chosen_caption, limit=6)
                    if emo:
                        mem["recent_emojis"].extend(emo)
                        mem["recent_emojis"] = mem["recent_emojis"][-40:]

                    _save_style_memory(mem)
                except:
                    pass

            return best_text.strip() if best_text else (raw or "").strip()
    except:
        pass

    # Fallback: best-effort cleanup without strict truncation.
    raw = (raw or "").strip()
    return raw

# ── Web UI ────────────────────────────────────────────────
def build_html(videos):
    cards = ""
    for i, vpath in enumerate(videos):
        try:
            b64     = get_thumb_b64(vpath)
            src     = f"data:image/jpeg;base64,{b64}" if b64 else ""
            size_mb = os.path.getsize(vpath) / (1024 * 1024)
            name    = os.path.basename(vpath)
            img     = (f"<img src='{src}' loading='lazy'>"
                       if src else
                       f"<div class='no-thumb'>{name[:20]}</div>")
            cards += f"""
            <div class="card" onclick="pick({i})">
                {img}
                <div class="label">{size_mb:.1f}MB</div>
            </div>"""
        except:
            pass

    return f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pick Video</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0a0a0a;font-family:sans-serif;padding-bottom:40px}}
  header{{background:#111;padding:16px 20px;border-bottom:1px solid #222;
          position:sticky;top:0;z-index:10}}
  header h1{{color:#fff;font-size:15px;font-weight:600}}
  header span{{color:#666;font-size:12px;display:block;margin-top:2px}}
  .grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:3px;padding:3px}}
  .card{{position:relative;aspect-ratio:9/16;background:#1a1a1a;
         cursor:pointer;overflow:hidden;border-radius:4px}}
  .card:active{{transform:scale(0.95);opacity:0.8}}
  .card img{{width:100%;height:100%;object-fit:cover}}
  .no-thumb{{width:100%;height:100%;display:flex;align-items:center;
             justify-content:center;color:#666;font-size:11px;
             padding:8px;text-align:center}}
  .label{{position:absolute;bottom:0;left:0;right:0;
          background:linear-gradient(transparent,rgba(0,0,0,0.8));
          color:#ccc;font-size:10px;padding:12px 6px 5px;text-align:right}}
  .overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.93);
            z-index:100;align-items:center;justify-content:center;
            flex-direction:column;gap:14px}}
  .overlay.show{{display:flex}}
  .spinner{{width:44px;height:44px;border:3px solid #333;
            border-top-color:#fff;border-radius:50%;
            animation:spin 0.75s linear infinite}}
  .msg{{color:#fff;font-size:15px;font-weight:500}}
  .step{{color:#666;font-size:12px}}
  @keyframes spin{{to{{transform:rotate(360deg)}}}}
</style>
</head>
<body>
<header>
  <h1>Pick Video</h1>
  <span>{len(videos)} videos &middot; tap to generate caption</span>
</header>
<div class="grid">{cards}</div>
<div class="overlay" id="o">
  <div class="spinner" id="spin"></div>
  <p class="msg"  id="msg">Processing...</p>
  <p class="step" id="step"></p>
</div>
<script>
async function pick(i) {{
  const ov   = document.getElementById('o');
  const msg  = document.getElementById('msg');
  const step = document.getElementById('step');
  const spin = document.getElementById('spin');
  ov.classList.add('show');
  spin.style.display = 'block';
  msg.textContent  = 'Processing...';
  step.textContent = 'Extracting frames & audio...';
  try {{
        const pickResp = await fetch('/pick?i=' + i);
        if (!pickResp.ok) throw new Error('Pick failed');

        let tries = 0;
        const maxTries = 160; // ~2 minutes at 750ms
        while (tries++ < maxTries) {{
            const st = await fetch('/status').then(r => r.json());
            const stage = (st && st.stage) ? st.stage : 'unknown';
            const detail = (st && st.detail) ? st.detail : '';

            if (stage === 'done') {{
                spin.style.display = 'none';
                msg.textContent  = 'Copied!';
                step.textContent = 'Pick next video';
                setTimeout(() => {{
                    ov.classList.remove('show');
                    spin.style.display = 'block';
                }}, 1600);
                return;
            }}

            if (stage === 'error') {{
                msg.textContent  = 'Error - try again';
                step.textContent = detail ? detail : '';
                setTimeout(() => ov.classList.remove('show'), 3000);
                return;
            }}

            if (stage === 'queued') step.textContent = 'Queued...';
            else if (stage === 'extracting') step.textContent = 'Extracting frames & audio...';
            else if (stage === 'generating') step.textContent = 'Generating caption...';
            else if (stage === 'idle') step.textContent = 'Waiting...';
            else step.textContent = (detail || 'Processing...');

            await new Promise(r => setTimeout(r, 750));
        }}

        msg.textContent  = 'Timed out - try again';
        step.textContent = '';
        setTimeout(() => ov.classList.remove('show'), 2500);
  }} catch(e) {{
    msg.textContent  = 'Error - try again';
    step.textContent = '';
    setTimeout(() => ov.classList.remove('show'), 2500);
  }}
}}
</script>
</body>
</html>"""

# ── HTTP server ───────────────────────────────────────────
class ReusableServer(HTTPServer):
    allow_reuse_address = True
    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except:
            pass
        super().server_bind()

class Handler(BaseHTTPRequestHandler):
    videos         = []
    error_occurred = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        global selected_video, caption_ready, processing_stage, processing_detail
        try:
            parsed = urlparse(self.path)

            if parsed.path == "/":
                body = build_html(self.videos).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)

            elif parsed.path == "/pick":
                idx = int(parse_qs(parsed.query).get("i", [0])[0])
                if idx < 0 or idx >= len(self.videos):
                    set_processing_stage("error", "Bad selection index")
                    Handler.error_occurred = True
                    caption_ready.set()
                    self.send_response(400)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"bad index")
                else:
                    with processing_lock:
                        caption_ready.clear()
                        Handler.error_occurred = False
                        selected_video = self.videos[idx]
                        processing_stage = "queued"
                        processing_detail = ""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"ok")

            elif parsed.path == "/status":
                # Non-blocking status endpoint for better debugging.
                with processing_lock:
                    stage = processing_stage
                    detail = processing_detail
                    err = Handler.error_occurred

                payload = {
                    "stage": "error" if err else stage,
                    "detail": detail,
                }

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode("utf-8"))

            else:
                self.send_response(404)
                self.end_headers()
        except:
            try:
                self.send_response(500)
                self.end_headers()
            except:
                pass

# ── Main ──────────────────────────────────────────────────
def main():
    global selected_video, processing_stage, processing_detail

    try:
        api_key = load_api_key()
    except FileNotFoundError:
        notify("Error", "No API key at ~/.config/gemini_key.txt", "nb_error")
        sys.exit(1)

    try:
        videos = load_videos()
    except Exception as e:
        notify("Error", str(e)[:60], "nb_error")
        sys.exit(1)

    if not videos:
        toast("No videos found!")
        sys.exit(1)

    print(f"� Found {len(videos)} videos in gallery")
    videos = generate_thumbnails(videos)
    if not videos:
        toast("No readable videos!")
        sys.exit(1)
    Handler.videos = videos

    kill_port(PORT)
    srv = ReusableServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    time.sleep(1)
    url = f"http://127.0.0.1:{PORT}"
    open_browser(url)

    try:
        subprocess.run([
            "termux-notification", "--id", "nb_picker",
            "--title", "NewsBreak Caption",
            "--content", "Tap to open picker",
            "--button1", "Open",
            "--button1-action", f"termux-open-url {url}",
            "--ongoing"
        ], timeout=3, capture_output=True, check=False)
    except:
        pass

    # ── Processing loop ───────────────────────────────────
    while True:
        with processing_lock:
            selected_video = None
            caption_ready.clear()
            Handler.error_occurred = False
            # Don't reset stage/detail here.
            # If we immediately reset from "done" → "idle", the web UI can miss
            # the "done" state and keep showing the processing overlay.

        while selected_video is None:
            time.sleep(0.2)

        current = selected_video

        try:
            set_processing_stage("extracting")
            toast("Extracting...")
            frame_paths, audio_ok = extract_parallel(current)

            if not frame_paths:
                raise ValueError("Could not extract frames")

            set_processing_stage("generating")
            toast("Generating caption...")
            pov_mode = next_pov_mode()
            prompt = build_caption_prompt(pov_mode)
            raw    = generate_caption(api_key, frame_paths, audio_ok, prompt)
            result = format_result(raw, pov_mode=pov_mode)

            print(f"\n{result}\n")
            if copy_to_clipboard(result):
                toast("Copied!")
                notify("Ready", "Caption copied", "nb_status")
                set_processing_stage("done")
            else:
                toast("Clipboard failed")
                notify("Error", "Clipboard failed (caption printed in terminal)", "nb_status")
                Handler.error_occurred = True
                set_processing_stage("error", "Clipboard failed")
            caption_ready.set()

        except Exception as e:
            err = str(e)[:80]
            print(f"\nERROR: {err}\n")
            toast(f"Error: {err[:50]}")
            notify("Error", err, "nb_status")
            Handler.error_occurred = True
            set_processing_stage("error", err)
            caption_ready.set()

        finally:
            cleanup()

if __name__ == "__main__":
    main()