#!/usr/bin/env python3
"""
Scoopz Caption Generator — v4
═══════════════════════════════════════════════════════════════════════════════
Stack  : ffmpeg → frame descriptions (text) + Whisper transcript → Gemini batch
Gemini : TEXT ONLY — zero images, zero audio bytes sent to API
Batch  : Exactly 8 videos per run. UI enforces this hard cap.
Target : ~5-8s total for 8 captions (one Gemini call, text payload only)
═══════════════════════════════════════════════════════════════════════════════
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
import math
import shutil
import tempfile
import requests
import secrets
from contextlib import contextmanager
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── Config ────────────────────────────────────────────────────────────────────
KEY_FILE            = os.path.expanduser("~/.config/gemini_key.txt")
GEMINI_MODEL        = "gemini-2.5-flash-lite"
GEMINI_BASE         = "https://generativelanguage.googleapis.com/v1beta/models"

POV_STATE_FILE       = os.path.expanduser("~/.cache/scoopz_caption_pov.json")
USER_EXAMPLES_FILE   = os.path.expanduser("~/.config/scoopz_caption_examples.txt")
STYLE_MEMORY_FILE    = os.path.expanduser("~/.cache/scoopz_caption_style_memory.json")
READABILITY_CACHE_FILE = os.path.expanduser("~/.cache/scoopz_video_readability.json")
UNREADABLE_REPORT_FILE = os.path.expanduser("~/.cache/scoopz_unreadable_videos.txt")
LAST_CAPTION_FILE   = "/sdcard/Download/scoopz_last_caption.txt"

SCOOPZ_DIR  = "/sdcard/Pictures/scoopz"
THUMB_DIR   = "/sdcard/Pictures/scoopz/.thumbs"
TEMP_DIR    = "/sdcard/Download"

# Hard batch cap — UI enforces exactly this many selections
BATCH_CAP = 8

PORT = 8765

# ── Temp file paths (per-video slots, reused across batches) ──────────────────
# Each slot: 2 frames + 1 audio + 1 trimmed audio
def _slot_paths(slot: int) -> dict:
    return {
        "f1":    f"{TEMP_DIR}/sz_{slot}_f1.jpg",
        "f2":    f"{TEMP_DIR}/sz_{slot}_f2.jpg",
        "audio": f"{TEMP_DIR}/sz_{slot}_audio.mp3",
        "trim":  f"{TEMP_DIR}/sz_{slot}_trim.mp3",
        "cands": [f"{TEMP_DIR}/sz_{slot}_c{j}.jpg" for j in range(6)],
    }

# ── Shared state ──────────────────────────────────────────────────────────────
processing_lock   = threading.Lock()
processing_stage  = "idle"
processing_detail = ""
batch_job         = None          # list of video indices when a job is queued
caption_ready     = threading.Event()


def set_stage(stage: str, detail: str = ""):
    global processing_stage, processing_detail
    with processing_lock:
        processing_stage  = stage
        processing_detail = detail


@contextmanager
def _timer(store: dict, key: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        store[key] = time.perf_counter() - t0


# ══════════════════════════════════════════════════════════════════════════════
# JSON HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_json_atomic(path: str, data: dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        try:
            os.replace(tmp, path)
        except Exception:
            os.rename(tmp, path)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# POV ROTATION
# ══════════════════════════════════════════════════════════════════════════════

def next_pov_mode() -> str:
    state = _read_json(POV_STATE_FILE)
    last  = (state.get("last") or "third").strip().lower()
    order = ["third", "second", "first_observer"]
    try:
        idx = order.index(last)
    except ValueError:
        idx = 0
    nxt = order[(idx + 1) % len(order)]
    state["last"] = nxt
    _write_json_atomic(POV_STATE_FILE, state)
    return {"second": "SECOND PERSON", "first_observer": "FIRST PERSON (OBSERVER)"}.get(nxt, "THIRD PERSON")


# ══════════════════════════════════════════════════════════════════════════════
# ANDROID UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def notify(title: str, msg: str, notif_id: str = "nb_status"):
    try:
        subprocess.run(
            ["termux-notification", "--id", notif_id, "--title", title, "--content", msg],
            timeout=3, capture_output=True, check=False,
        )
    except Exception:
        pass


def toast(msg: str):
    try:
        subprocess.run(["termux-toast", "-s", msg], timeout=2, capture_output=True, check=False)
    except Exception:
        pass


def open_browser(url: str):
    print(f"\n>>> Open in browser: {url}\n")
    for args in [
        ["am", "start", "-a", "android.intent.action.VIEW", "-d", url, "--activity-brought-to-front"],
        ["am", "start", "-a", "android.intent.action.VIEW", "-d", url],
    ]:
        try:
            r = subprocess.run(args, timeout=4, capture_output=True, check=False)
            if r.returncode == 0:
                return
        except Exception:
            pass
    try:
        subprocess.run(["termux-open-url", url], timeout=4, capture_output=True)
    except Exception:
        pass


def copy_to_clipboard(text: str) -> bool:
    """
    4-layer clipboard stack for Android.
    Layer 4 (file write) always runs first as guaranteed fallback.
    """
    expected = (text or "").strip()
    if not expected:
        return False

    def _norm(s: str) -> str:
        s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
        s = s.replace("\ufe0f", "").replace("\u200d", "")
        return re.sub(r"\s+", " ", s.strip())

    # Layer 4 — file fallback (always)
    try:
        with open(LAST_CAPTION_FILE, "w", encoding="utf-8") as f:
            f.write(expected)
    except Exception:
        pass

    # Layer 1 — termux-clipboard-set
    if shutil.which("termux-clipboard-set"):
        for attempt in range(5):
            try:
                r = subprocess.run(
                    ["termux-clipboard-set"],
                    input=expected.encode("utf-8"),
                    timeout=5, capture_output=True, check=False,
                )
                if r.returncode == 0:
                    for delay in (0.0, 0.15, 0.30):
                        if delay:
                            time.sleep(delay)
                        try:
                            g = subprocess.run(
                                ["termux-clipboard-get"],
                                timeout=5, capture_output=True, text=True, check=False,
                            )
                            if g.returncode == 0 and _norm(g.stdout) == _norm(expected):
                                return True
                        except Exception:
                            pass
                    # set succeeded even if read-back was flaky
                    return True
            except FileNotFoundError:
                break
            except Exception:
                pass
            time.sleep(0.3 + attempt * 0.2)

    # Layer 2 — am broadcast intent
    try:
        r = subprocess.run(
            ["am", "broadcast", "-a", "clipper.set", "--es", "text", expected[:512]],
            timeout=5, capture_output=True, check=False,
        )
        if r.returncode == 0:
            return True
    except Exception:
        pass

    # Layer 3 — xclip / xsel
    for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            try:
                r = subprocess.run(cmd, input=expected.encode("utf-8"),
                                   timeout=4, capture_output=True, check=False)
                if r.returncode == 0:
                    return True
            except Exception:
                pass

    print(f"⚠️  All clipboard layers failed. Saved to: {LAST_CAPTION_FILE}")
    notify("Caption saved", f"Open {LAST_CAPTION_FILE}", "nb_clip")
    return False


def load_api_key() -> str:
    with open(KEY_FILE) as f:
        return f.read().strip()


def kill_port(port: int):
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
            except Exception:
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
                except Exception:
                    pass
    except Exception:
        pass
    time.sleep(0.5)


def cleanup_slot(slot: int):
    paths = _slot_paths(slot)
    for key in ("f1", "f2", "audio", "trim"):
        try:
            p = paths[key]
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    for p in paths["cands"]:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def cleanup_all():
    for slot in range(BATCH_CAP):
        cleanup_slot(slot)


# ══════════════════════════════════════════════════════════════════════════════
# VIDEO UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def get_duration(vpath: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", vpath],
            capture_output=True, timeout=10, text=True,
        )
        return float(r.stdout.strip())
    except Exception:
        return 30.0


def is_video_readable(vpath: str) -> bool:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", vpath],
            capture_output=True, timeout=8, text=True, check=False,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def can_extract_frame(vpath: str) -> bool:
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", vpath,
             "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, timeout=10, text=True, check=False,
        )
        return r.returncode == 0
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SMART FRAME EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def _score_frame(path: str) -> float:
    """Sharpness + brightness via ffmpeg edgedetect+signalstats. No OpenCV."""
    if not os.path.exists(path) or os.path.getsize(path) < 500:
        return 0.0
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "quiet", "-i", path,
             "-vf", (
                 "split=2[a][b];"
                 "[a]edgedetect=low=0.05:high=0.2[ed];"
                 "[ed]signalstats[es];"
                 "[b]signalstats[bs]"
             ),
             "-map", "[es]", "-map", "[bs]",
             "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, timeout=8, text=True, check=False,
        )
        yavgs = re.findall(r"YAVG:([\d.]+)", r.stderr or "")
        if len(yavgs) >= 2:
            edge_energy = float(yavgs[0])
            brightness  = float(yavgs[1])
            penalty = 40.0 if (brightness < 20 or brightness > 235) else (15.0 if brightness < 50 else 0.0)
            return max(0.0, edge_energy - penalty)
    except Exception:
        pass
    try:
        return os.path.getsize(path) / 10_000.0
    except Exception:
        return 0.0


def _frames_similar(a: str, b: str, threshold: float = 0.92) -> bool:
    """PSNR similarity check via ffmpeg."""
    if not (os.path.exists(a) and os.path.exists(b)):
        return False
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "quiet",
             "-i", a, "-i", b, "-filter_complex", "psnr", "-f", "null", "-"],
            capture_output=True, timeout=8, text=True, check=False,
        )
        m = re.search(r"average:([\d.]+)", r.stderr or "")
        if m:
            return (1.0 - math.exp(-float(m.group(1)) / 40.0)) >= threshold
    except Exception:
        pass
    try:
        sa, sb = os.path.getsize(a), os.path.getsize(b)
        if sa > 0 and sb > 0:
            return min(sa, sb) / max(sa, sb) > 0.97
    except Exception:
        pass
    return False


def _dedup(paths: list) -> list:
    kept = []
    for p in paths:
        if not any(_frames_similar(p, k) for k in kept):
            kept.append(p)
    return kept


def _scene_timestamps(vpath: str, duration: float,
                      threshold: float = 0.28, limit: int = 5) -> list:
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "quiet", "-i", vpath,
             "-vf", f"select='gt(scene,{threshold})',showinfo",
             "-vsync", "vfr", "-frames:v", str(limit), "-f", "null", "-"],
            capture_output=True, timeout=20, text=True, check=False,
        )
        ts = sorted({
            float(m.group(1))
            for m in re.finditer(r"pts_time:([\d.]+)", r.stderr or "")
            if 0.0 < float(m.group(1)) < duration
        })
        return ts[:limit]
    except Exception:
        return []


def _extract_one_frame(vpath: str, seek: float, out: str) -> bool:
    try:
        if os.path.exists(out):
            os.remove(out)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-ss", f"{seek:.3f}", "-i", vpath,
             "-vframes", "1", "-q:v", "3", "-vf", "scale=480:-1", out],
            capture_output=True, timeout=12, check=False,
        )
    except Exception:
        pass
    return os.path.exists(out) and os.path.getsize(out) > 500


def _extract_first_frame(vpath: str, out: str) -> bool:
    try:
        if os.path.exists(out):
            os.remove(out)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-i", vpath,
             "-vframes", "1", "-q:v", "5", "-vf", "scale=480:-1", out],
            capture_output=True, timeout=12, check=False,
        )
    except Exception:
        pass
    return os.path.exists(out) and os.path.getsize(out) > 500


def extract_best_frames(vpath: str, slot: int) -> tuple:
    """
    Extract 2 best-quality, distinct frames. Writes to slot f1/f2 paths.
    Returns (f1_path_or_None, f2_path_or_None).
    """
    paths = _slot_paths(slot)
    cands = paths["cands"]
    f1, f2 = paths["f1"], paths["f2"]

    # Clean stale files
    for p in cands + [f1, f2]:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    duration = get_duration(vpath)

    scene_ts = _scene_timestamps(vpath, duration)
    fixed_ts = [max(0.5, duration * r) for r in (0.10, 0.25, 0.50, 0.75, 0.90)]
    all_ts   = list(scene_ts)
    for t in fixed_ts:
        if all(abs(t - e) > 1.0 for e in all_ts):
            all_ts.append(t)
    all_ts = sorted(all_ts)[:6]

    extracted = []
    for i, seek in enumerate(all_ts):
        if _extract_one_frame(vpath, seek, cands[i]):
            extracted.append((cands[i], seek))

    if not extracted:
        if _extract_first_frame(vpath, cands[0]):
            extracted.append((cands[0], 0.0))

    if not extracted:
        return None, None

    scored = sorted(
        [(_score_frame(p), seek, p) for p, seek in extracted],
        key=lambda x: x[0], reverse=True,
    )
    score_map = {p: s for s, _, p in scored}
    deduped   = _dedup([p for _, _, p in scored])
    final     = sorted(deduped, key=lambda p: score_map.get(p, 0.0), reverse=True)[:2]

    if len(final) == 1 and len(scored) >= 2:
        s2 = scored[1][2]
        if s2 != final[0]:
            final.append(s2)

    results = []
    for i, src in enumerate(final[:2]):
        dst = f1 if i == 0 else f2
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-y", "-i", src, "-q:v", "3", dst],
                capture_output=True, timeout=8, check=False,
            )
            if os.path.exists(dst) and os.path.getsize(dst) > 500:
                results.append(dst)
        except Exception:
            pass

    # Fallback for any missing target
    for dst, ratio in ((f1, 0.10), (f2, 0.50)):
        if dst not in results:
            seek = max(0.5, duration * ratio)
            if _extract_one_frame(vpath, seek, dst):
                results.append(dst)

    # Clean candidate temps
    for p in cands:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    r1 = results[0] if len(results) > 0 else None
    r2 = results[1] if len(results) > 1 else None
    return r1, r2


# ══════════════════════════════════════════════════════════════════════════════
# FRAME → TEXT DESCRIPTION
# (This replaces sending images to Gemini — text only hits the API)
# ══════════════════════════════════════════════════════════════════════════════

def _signalstats(frame_path: str) -> dict:
    """Extract brightness, contrast, saturation via ffmpeg signalstats."""
    if not os.path.exists(frame_path):
        return {}
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "quiet", "-i", frame_path,
             "-vf", "signalstats=stat=tout+vrep+brng", "-frames:v", "1",
             "-f", "null", "-"],
            capture_output=True, timeout=8, text=True, check=False,
        )
        stats = {}
        for key in ("YAVG", "YMIN", "YMAX", "SATAVG", "HUEMED"):
            m = re.search(rf"lavfi\.signalstats\.{key}=([0-9.]+)", r.stderr or "")
            if m:
                try:
                    stats[key.lower()] = float(m.group(1))
                except Exception:
                    pass
        return stats
    except Exception:
        return {}


def _ocr(frame_path: str) -> str:
    """Best-effort OCR via tesseract. Returns empty string if unavailable."""
    if not frame_path or not os.path.exists(frame_path):
        return ""
    if not shutil.which("tesseract"):
        return ""
    try:
        r = subprocess.run(
            ["tesseract", frame_path, "stdout", "-l", "eng", "--psm", "6"],
            capture_output=True, timeout=8, text=True, check=False,
        )
        if r.returncode != 0:
            return ""
        text = re.sub(r"[^\w\s\-.,:;!?()'\"/#]", " ", r.stdout or "", flags=re.UNICODE)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:200]
    except Exception:
        return ""


def _brightness_label(yavg: float) -> str:
    if yavg < 40:
        return "very dark scene"
    if yavg < 80:
        return "dimly lit"
    if yavg < 160:
        return "normally lit"
    if yavg < 210:
        return "bright scene"
    return "overexposed"


def _contrast_label(ymin: float, ymax: float) -> str:
    spread = ymax - ymin
    if spread < 40:
        return "low contrast (flat, foggy, or uniform)"
    if spread < 120:
        return "moderate contrast"
    return "high contrast (sharp shadows or hard lighting)"


def _saturation_label(satavg: float) -> str:
    if satavg < 15:
        return "desaturated / near greyscale"
    if satavg < 60:
        return "muted colours"
    if satavg < 120:
        return "vivid colours"
    return "highly saturated colours"


def describe_frame(frame_path: str, frame_num: int) -> str:
    """
    Build a compact text description of a frame for the Gemini prompt.
    Uses only ffmpeg signalstats + optional tesseract OCR — no vision model.

    Output example:
        FRAME 1 — normally lit, high contrast (sharp shadows), muted colours
        On-screen text: "BREAKING: Fire breaks out at warehouse"
    """
    if not frame_path or not os.path.exists(frame_path):
        return f"FRAME {frame_num} — not available"

    stats = _signalstats(frame_path)
    parts = []

    if stats:
        yavg   = stats.get("yavg",   128.0)
        ymin   = stats.get("ymin",   0.0)
        ymax   = stats.get("ymax",   255.0)
        satavg = stats.get("satavg", 50.0)
        parts.append(_brightness_label(yavg))
        parts.append(_contrast_label(ymin, ymax))
        parts.append(_saturation_label(satavg))
    else:
        parts.append("visual stats unavailable")

    description = f"FRAME {frame_num} — {', '.join(parts)}"

    ocr_text = _ocr(frame_path)
    if ocr_text:
        description += f'\n  On-screen text: "{ocr_text}"'

    return description


def build_frame_descriptions(f1: str, f2: str) -> str:
    """Return a 2-frame description block for one video."""
    lines = []
    if f1:
        lines.append(describe_frame(f1, 1))
    if f2:
        lines.append(describe_frame(f2, 2))
    return "\n".join(lines) if lines else "No frames available."


# ══════════════════════════════════════════════════════════════════════════════
# LOCAL WHISPER TRANSCRIPTION
# ══════════════════════════════════════════════════════════════════════════════

def _meaningful(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 10:
        return False
    # Reject whisper hallucination filler
    for filler in ("thank you for watching", "please subscribe", "subtitles by",
                   "[music]", "(music)", "[applause]", "www.", "transcribed by"):
        if t.lower().startswith(filler):
            return False
    return True


def transcribe(audio_path: str, trim_path: str) -> str:
    """
    Transcribe audio locally. Trims to 45s first for speed on mobile.
    Returns empty string gracefully if whisper is not installed.
    """
    if not audio_path or not os.path.exists(audio_path):
        return ""

    # Trim to 45s
    try:
        if os.path.exists(trim_path):
            os.remove(trim_path)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-i", audio_path, "-t", "45",
             "-ar", "16000", "-ac", "1", trim_path],
            capture_output=True, timeout=12, check=False,
        )
        src = trim_path if (os.path.exists(trim_path) and os.path.getsize(trim_path) > 500) else audio_path
    except Exception:
        src = audio_path

    # whisper.cpp binary
    model_path = os.path.expanduser("~/.local/share/whisper/ggml-base.en.bin")
    for binary in ("whisper-cli", "whisper_cpp", "main"):
        if shutil.which(binary) and os.path.exists(model_path):
            try:
                r = subprocess.run(
                    [binary, "-m", model_path, "-f", src,
                     "--output-txt", "--no-timestamps", "-"],
                    capture_output=True, timeout=60, text=True, check=False,
                )
                out = (r.stdout or "").strip()
                if _meaningful(out):
                    return out[:600]
            except Exception:
                pass

    # Python whisper package
    try:
        import whisper as _w  # type: ignore
        model = _w.load_model("base.en")
        result = model.transcribe(src, fp16=False, language="en")
        text   = (result.get("text") or "").strip()
        if _meaningful(text):
            return text[:600]
    except ImportError:
        pass
    except Exception as e:
        print(f"⚠️  Whisper: {e}")

    return ""


def extract_audio(vpath: str, audio_path: str):
    """Extract mono mp3 at 16kbps, capped at 45s (only what Whisper needs)."""
    try:
        if os.path.exists(audio_path):
            os.remove(audio_path)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-i", vpath,
             "-vn", "-t", "45", "-ar", "16000", "-ac", "1", "-b:a", "16k",
             audio_path],
            capture_output=True, timeout=25, check=False,
        )
    except Exception:
        pass


def _build_transcript_block(transcript: str) -> str:
    t = (transcript or "").strip()
    if not t:
        return "No speech detected — infer story from visual description only."
    return t


# ══════════════════════════════════════════════════════════════════════════════
# BATCH EXTRACTION  (all 8 videos in parallel threads)
# ══════════════════════════════════════════════════════════════════════════════

def extract_video(vpath: str, slot: int) -> dict:
    """
    Full extraction for one video slot: frames + audio + transcript.
    Returns dict with keys: vpath, frame_desc, transcript, ok, error.
    Retries once on failure.
    """
    def _attempt():
        paths = _slot_paths(slot)

        # Frames + audio in parallel
        f1_box, f2_box, audio_ok_box = [None], [None], [False]

        def _frames():
            r1, r2 = extract_best_frames(vpath, slot)
            f1_box[0], f2_box[0] = r1, r2

        def _audio():
            extract_audio(vpath, paths["audio"])
            audio_ok_box[0] = (
                os.path.exists(paths["audio"]) and
                os.path.getsize(paths["audio"]) > 500
            )

        tf = threading.Thread(target=_frames, daemon=True)
        ta = threading.Thread(target=_audio,  daemon=True)
        tf.start(); ta.start()
        tf.join();  ta.join()

        f1, f2 = f1_box[0], f2_box[0]
        if not f1:
            raise ValueError("frame extraction failed")

        # Build frame text description
        frame_desc = build_frame_descriptions(f1, f2)

        # Transcribe (sequential, after audio extraction)
        transcript = ""
        if audio_ok_box[0]:
            transcript = transcribe(paths["audio"], paths["trim"])

        return {
            "vpath":      vpath,
            "frame_desc": frame_desc,
            "transcript": transcript,
            "ok":         True,
            "error":      "",
        }

    for attempt in range(2):
        try:
            cleanup_slot(slot)
            return _attempt()
        except Exception as e:
            if attempt == 0:
                time.sleep(0.5)
            else:
                return {
                    "vpath": vpath, "frame_desc": "", "transcript": "",
                    "ok": False, "error": str(e)[:80],
                }

    # unreachable, satisfies type checker
    return {"vpath": vpath, "frame_desc": "", "transcript": "", "ok": False, "error": "unknown"}


# ══════════════════════════════════════════════════════════════════════════════
# PROMPTS
# ══════════════════════════════════════════════════════════════════════════════

BATCH_PROMPT_HEADER = """\
Write punchy scroll-stopping captions for {N} videos.
Each video block contains: frame visual descriptions + transcript (or "no speech").

GLOBAL REQUIREMENTS (apply to EVERY video):
- Exactly 2 sentences per caption, each 70-80 characters
- Total emojis: 2 or 3  (Sentence 1 → exactly 1 emoji not at end; Sentence 2 → 1-2 emojis)
- Start with a SPECIFIC visual detail from the description — not generic
- Build tension, curiosity, or discomfort
- End with a strong pull: question, judgment, or challenge

STYLE: Raw, human, slightly uncomfortable. Not formal. Not news-like.

AUDIO RULE:
- Speech present → anchor caption in what is being said
- No speech / music only → focus on body language, movement, implied story

HASHTAGS per video: 4-5 lowercase tags, include exactly ONE of #breakingnews or #localnews.
No platform/app/tok/ig/reels tags.

OUTPUT FORMAT — strictly one block per video, no extra text:
VIDEO 1:
CAPTION: ...
HASHTAGS: ...

VIDEO 2:
CAPTION: ...
HASHTAGS: ...

(continue for all {N} videos)

INTERNAL VALIDATION per video before returning:
1. Exactly 2 sentences  2. Sentence 1 has ≥1 emoji  3. Sentence 2 has ≥1 emoji  4. Total = 2-3
Rewrite until all pass. Never skip a video.
"""


def _load_user_examples() -> list:
    examples = []
    if not os.path.exists(USER_EXAMPLES_FILE):
        return examples
    try:
        with open(USER_EXAMPLES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                s = (line or "").strip()
                if not s or s.startswith("#"):
                    continue
                s = re.sub(r"#\w+", "", s).strip().replace('"', "'")
                if s:
                    examples.append(s)
                    if len(examples) >= 6:
                        break
    except Exception:
        pass
    return examples


def build_batch_prompt(items: list) -> str:
    """
    items: list of dicts with keys: pov_mode, frame_desc, transcript
    Returns full text-only prompt for one Gemini call.
    """
    n = len(items)
    lines = [BATCH_PROMPT_HEADER.format(N=n)]

    for i, item in enumerate(items, 1):
        pov  = item.get("pov_mode", "THIRD PERSON")
        fdesc = (item.get("frame_desc") or "No frame description available.").strip()
        tb   = _build_transcript_block(item.get("transcript", ""))
        lines.append(f"--- VIDEO {i} (POV: {pov}) ---")
        lines.append(fdesc)
        lines.append(f"TRANSCRIPT: {tb}")
        lines.append("")

    examples = _load_user_examples()
    if examples:
        lines.append("STYLE EXAMPLES — match pattern only, do NOT copy wording:")
        lines += [f'- "{e}"' for e in examples]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# GEMINI — TEXT ONLY
# ══════════════════════════════════════════════════════════════════════════════

def gemini_call(api_key: str, prompt: str, max_tokens: int = 7200) -> str:
    """
    Pure text-in, text-out Gemini call.
    No images. No audio bytes. Just the prompt string.
    Exponential backoff: 10s → 30s → 60s → 120s.
    """
    url     = f"{GEMINI_BASE}/{GEMINI_MODEL}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature":    1.0,
            "maxOutputTokens": max_tokens,
        },
    }
    delays = [10, 30, 60, 120]

    for attempt, delay in enumerate(delays + [None]):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=45)
            if resp.status_code == 429:
                wait = delay if delay is not None else 180
                print(f"⚠️  Gemini 429 — waiting {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = delay if delay is not None else 180
                print(f"⚠️  Gemini {resp.status_code} — waiting {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except requests.exceptions.Timeout:
            print(f"⚠️  Gemini timeout (attempt {attempt + 1})")
            if delay is not None:
                time.sleep(delay)
        except Exception as e:
            print(f"⚠️  Gemini error: {e}")
            if delay is not None:
                time.sleep(delay)

    raise ValueError("Gemini failed after all retries")


# ══════════════════════════════════════════════════════════════════════════════
# RESPONSE PARSER + FORMATTER
# ══════════════════════════════════════════════════════════════════════════════

_EMOJI_RE     = re.compile(
    r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u26FF\u2700-\u27BF]",
    flags=re.UNICODE,
)
_SKIN_RE      = re.compile(r"[\U0001F3FB-\U0001F3FF]", flags=re.UNICODE)


def _split_sents(text: str) -> list:
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if p.strip()]


def _trim(text: str, n: int) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    trimmed = text[:n].rsplit(" ", 1)[0].strip() or text[:n].strip()
    last = max(trimmed.rfind("."), trimmed.rfind("!"), trimmed.rfind("?"))
    if last >= int(len(trimmed) * 0.65):
        trimmed = trimmed[:last + 1].strip()
    return trimmed


def _limit_emojis(text: str, cap: int) -> str:
    kept, count = [], 0
    for ch in (text or ""):
        if ch in ("\u200d", "\ufe0f") or _SKIN_RE.match(ch):
            kept.append(ch)
            continue
        if _EMOJI_RE.match(ch):
            if count >= cap:
                continue
            count += 1
        kept.append(ch)
    return "".join(kept)


def _parse_video_blocks(raw: str, n: int) -> list:
    """
    Split raw Gemini response into per-video (caption, hashtags) pairs.
    Handles markdown bolding, missing colons, irregular spacing.
    Returns list of length n; missing entries are ("", "").
    """
    raw = (raw or "").replace("\r\n", "\n").strip()
    blocks = re.split(r"(?im)^\s*-*\s*VIDEO\s+(\d+)\s*[:\-]?\s*-*", raw)
    parsed = {}
    i = 1
    while i + 1 < len(blocks):
        try:
            num     = int(blocks[i])
            content = blocks[i + 1]
            m = re.search(
                r"(?ims)\bcaption\s*[:\-]?\s*(.*?)\n\s*(?:hashtags?|tags)\s*[:\-]?\s*(.*?)(?:\n|$)",
                content,
            )
            if m:
                parsed[num] = ((m.group(1) or "").strip(), (m.group(2) or "").strip())
            else:
                # Fallback: first line = caption, line with # = hashtags
                lines = [l.strip() for l in content.splitlines() if l.strip()]
                cap   = lines[0] if lines else ""
                tags  = next((l for l in reversed(lines) if "#" in l), "")
                if cap:
                    parsed[num] = (cap, tags)
        except Exception:
            pass
        i += 2

    return [parsed.get(n_idx, ("", "")) for n_idx in range(1, n + 1)]


def _sanitise(caption: str, hashtags: str, pov_mode: str) -> tuple:
    EMOJI_MAX   = 3
    CAPTION_MAX = 200

    # Caption cleanup
    c = (caption or "").strip()
    c = re.sub(r"#\w+", "", c)
    c = re.sub(r"\bnewsbreak\b", "", c, flags=re.IGNORECASE)
    c = re.sub(r"^\s*(?:nah\b|this is (?:crazy|wild)\b|can\s*'?t\s+believe\b)\s*[,:.!-]*\s*", "", c, flags=re.IGNORECASE)
    c = re.sub(r"^\s*[—\-–]+\s*", "", c).strip()
    c = re.sub(r"\s+([?.!,])", r"\1", c)
    c = re.sub(r"\s{2,}", " ", c).strip()
    if c:
        c = c[0].upper() + c[1:]

    # Keep first 2 sentences
    sents = _split_sents(c)
    if len(sents) >= 2:
        c = f"{sents[0]} {sents[1]}".strip()

    c = _limit_emojis(c, EMOJI_MAX)
    c = _trim(c, CAPTION_MAX)

    # Collective first-person → third-person
    if re.search(r"\b(we|we're|our|ours|us)\b", c, re.IGNORECASE):
        for pat, rep in [
            (r"\bwe['']re\b", "they're"), (r"\bwe\b", "they"),
            (r"\bour\b",      "their"),   (r"\bours\b", "theirs"),
            (r"\bus\b",       "them"),
        ]:
            c = re.sub(pat, rep, c, flags=re.IGNORECASE)
        c = re.sub(r"^\s*they\b", "They", c, flags=re.IGNORECASE)
        c = re.sub(r"\s{2,}", " ", c).strip()

    # Hashtag cleanup
    h = (hashtags or "").strip().strip("[](){}")
    raw_tags = re.findall(r"#\w+", h.lower())

    banned = {
        "#newsbreak", "#newsbreakapp",
        "#tiktok", "#instagram", "#youtube", "#facebook", "#snapchat",
        "#twitter", "#x", "#reddit", "#discord", "#telegram",
        "#fyp", "#viral", "#trending", "#foryou",
    }
    low_signal = {"#news", "#breaking", "#update", "#alert", "#live"}

    def _bad_tag(tag: str) -> bool:
        b = tag.lstrip("#")
        if "tiktok" in b or "instagram" in b:
            return True
        if b.endswith("tok") and b != "tok":
            return True
        if b.startswith("insta") and b not in {"instant", "instantly"}:
            return True
        if b in {"ig", "instareels", "reels"}:
            return True
        return False

    clean, seen = [], set()
    for tag in raw_tags:
        if tag in banned or tag in low_signal or _bad_tag(tag) or tag in seen:
            continue
        seen.add(tag)
        clean.append(tag)

    if "#breakingnews" not in seen and "#localnews" not in seen:
        clean.append("#localnews")
        seen.add("#localnews")

    return c, " ".join(clean[:5]).strip()


def _score(caption: str, hashtags: str, pov_mode: str) -> int:
    score = 0
    c, cl = (caption or "").strip(), (caption or "").strip().lower()

    if 170 <= len(c) <= 200:   score += 10
    elif 155 <= len(c) <= 215: score += 5
    else:                       score -= 6

    sents = _split_sents(c)
    if   len(sents) == 2: score += 12
    elif len(sents) == 1: score -= 6
    else:                  score -= 8

    if re.search(r"\b(?:don't|can't|won't|you're|isn't|aren't|didn't|doesn't|i'm|i've|that's|there's)\b", cl):
        score += 3

    if any(p in cl for p in ["got weird", "isn't adding up", "people are missing",
                               "could go left", "shift here", "doesn't sit right", "too quiet"]):
        score += 9

    if any(p in c for p in ["—", "...", ";"]):
        score += 2

    if re.match(r"^(his|her|this|that)\b", cl):     score -= 10
    if any(p in cl for p in ["this dude", "this guy", "some dude"]): score -= 14

    if "SECOND" in (pov_mode or "") and "?" in c:  score += 10

    tags = re.findall(r"#\w+", (hashtags or "").lower())
    if 4 <= len(tags) <= 5:                          score += 4
    if "#breakingnews" in tags or "#localnews" in tags: score += 4

    # Anti-repetition
    try:
        mem    = _read_json(STYLE_MEMORY_FILE)
        recent = set((mem.get("recent_captions") or [])[-20:])
        norm   = re.sub(r"\s+", " ", cl.replace("\ufe0f", "").replace("\u200d", "")).strip()
        if norm and norm in recent:
            score -= 25
    except Exception:
        pass

    return score


def _update_memory(caption: str):
    try:
        mem = _read_json(STYLE_MEMORY_FILE)
        mem.setdefault("recent_captions", [])
        mem.setdefault("recent_emojis",   [])
        norm = re.sub(r"\s+", " ", caption.lower().replace("\ufe0f","").replace("\u200d","")).strip()
        if norm:
            mem["recent_captions"].append(norm)
            mem["recent_captions"] = mem["recent_captions"][-30:]
        emo = _EMOJI_RE.findall(caption)[:6]
        if emo:
            mem["recent_emojis"].extend(emo)
            mem["recent_emojis"] = mem["recent_emojis"][-40:]
        _write_json_atomic(STYLE_MEMORY_FILE, mem)
    except Exception:
        pass


def format_batch_results(raw: str, pov_modes: list) -> list:
    """
    Parse and sanitise all VIDEO N blocks.
    Returns list of formatted caption strings (one per video, empty string on failure).
    """
    n     = len(pov_modes)
    pairs = _parse_video_blocks(raw, n)
    out   = []

    for (cap, tags), pov in zip(pairs, pov_modes):
        if not cap:
            out.append("")
            continue
        cap, tags = _sanitise(cap, tags, pov)
        if not cap:
            out.append("")
            continue
        _update_memory(cap)
        out.append(f"{cap}\n\n{tags}".strip() if tags else cap.strip())

    return out


# ══════════════════════════════════════════════════════════════════════════════
# VIDEO LOADER + THUMBNAILS
# ══════════════════════════════════════════════════════════════════════════════

def mediastore_paths() -> set:
    try:
        r = subprocess.run(
            ["content", "query", "--uri", "content://media/external/video/media",
             "--projection", "_data"],
            capture_output=True, timeout=10, text=True, check=False,
        )
        paths = set()
        for line in r.stdout.splitlines():
            if "_data=" in line:
                paths.add(line.split("_data=", 1)[1].strip())
        return paths
    except Exception:
        return set()


def load_videos() -> list:
    all_files = [
        os.path.join(SCOOPZ_DIR, f)
        for f in os.listdir(SCOOPZ_DIR)
        if f.lower().endswith(".mp4") and not f.startswith(".")
    ]
    existing = [v for v in all_files if os.path.exists(v)]

    hide = os.environ.get("SCOOPZ_HIDE_UNREADABLE", "0").strip() in ("1", "true", "True")
    if hide:
        cache   = _read_json(READABILITY_CACHE_FILE)
        entries = (cache.get("entries") or {}) if isinstance(cache, dict) else {}
        pruned  = []
        for p in existing:
            try:
                e = entries.get(p) or {}
                if e.get("ok") is False:
                    if e.get("mtime") == os.path.getmtime(p) and e.get("size") == os.path.getsize(p):
                        continue
            except Exception:
                pass
            pruned.append(p)
        existing = pruned

    known    = mediastore_paths()
    filtered = [v for v in existing if (not known) or (v in known)]
    filtered.sort(key=os.path.getmtime, reverse=True)
    return filtered


def generate_thumbnails(videos: list) -> list:
    os.makedirs(THUMB_DIR, exist_ok=True)
    valid_names = {os.path.basename(v) for v in videos}

    try:
        for thumb in os.listdir(THUMB_DIR):
            if thumb.endswith(".jpg") and thumb[:-4] not in valid_names:
                try:
                    os.remove(os.path.join(THUMB_DIR, thumb))
                except Exception:
                    pass
    except Exception:
        pass

    cache = _read_json(READABILITY_CACHE_FILE)
    if not isinstance(cache, dict):
        cache = {}
    entries = cache.get("entries")
    if not isinstance(entries, dict):
        entries = {}
        cache["entries"] = entries

    total    = len(videos)
    readable = []
    skipped  = []
    verbose  = os.environ.get("SCOOPZ_VERBOSE_SKIPS", "0").strip() in ("1", "true", "True")

    for idx, vpath in enumerate(videos):
        if idx % 5 == 0:
            toast(f"Loading {idx}/{total}...")

        name       = os.path.basename(vpath)
        thumb_path = os.path.join(THUMB_DIR, name + ".jpg")

        # Fast path — already validated
        try:
            mtime = os.path.getmtime(vpath)
            size  = os.path.getsize(vpath)
            e     = entries.get(vpath) or {}
            if e.get("ok") is True and e.get("mtime") == mtime and e.get("size") == size:
                if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
                    readable.append(vpath)
                    continue
        except Exception:
            pass

        if not is_video_readable(vpath) or not can_extract_frame(vpath):
            try:
                if os.path.exists(thumb_path):
                    os.remove(thumb_path)
            except Exception:
                pass
            skipped.append(vpath)
            if verbose:
                print(f"⚠️  Skipped: {name}")
            continue

        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
            readable.append(vpath)
            continue

        try:
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
        except Exception:
            pass

        success = False
        for seek_s, use_dur in [("1", False), (None, True), (None, False)]:
            if success:
                break
            try:
                cmd = ["ffmpeg", "-nostdin", "-y"]
                if seek_s:
                    cmd += ["-ss", seek_s]
                elif use_dur:
                    cmd += ["-ss", str(max(0.5, get_duration(vpath) * 0.1))]
                cmd += ["-i", vpath, "-vframes", "1", "-q:v", "3",
                        "-vf", "scale=320:-1", thumb_path]
                subprocess.run(cmd, capture_output=True, timeout=15, check=False)
                success = os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500
            except Exception:
                pass

        if success:
            readable.append(vpath)
        else:
            skipped.append(vpath)
            if verbose:
                print(f"⚠️  Skipped: {name}")

    # Update cache
    try:
        for p in readable:
            try:
                entries[p] = {"ok": True, "mtime": os.path.getmtime(p), "size": os.path.getsize(p)}
            except Exception:
                pass
        for p in skipped:
            try:
                entries[p] = {"ok": False, "mtime": os.path.getmtime(p), "size": os.path.getsize(p)}
            except Exception:
                pass
        _write_json_atomic(READABILITY_CACHE_FILE, cache)
    except Exception:
        pass

    if skipped and not verbose:
        print(f"⚠️  Skipped {len(skipped)} unreadable videos")
    if skipped:
        try:
            os.makedirs(os.path.dirname(UNREADABLE_REPORT_FILE), exist_ok=True)
            with open(UNREADABLE_REPORT_FILE, "w", encoding="utf-8") as f:
                for p in skipped:
                    f.write(f"{'EXISTS' if os.path.exists(p) else 'MISSING'}\t{p}\n")
        except Exception:
            pass

    toast("Ready")
    return readable


def get_thumb_b64(vpath: str) -> str:
    thumb = os.path.join(THUMB_DIR, os.path.basename(vpath) + ".jpg")
    try:
        if os.path.exists(thumb):
            with open(thumb, "rb") as f:
                return base64.b64encode(f.read()).decode()
    except Exception:
        pass
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# WEB UI  — hard cap of exactly BATCH_CAP (8) selections
# ══════════════════════════════════════════════════════════════════════════════

def build_html(videos: list) -> str:
    cards = ""
    for i, vpath in enumerate(videos):
        try:
            b64     = get_thumb_b64(vpath)
            src     = f"data:image/jpeg;base64,{b64}" if b64 else ""
            size_mb = os.path.getsize(vpath) / (1024 * 1024)
            name    = os.path.basename(vpath)
            img     = (f"<img src='{src}' loading='lazy'>"
                       if src else f"<div class='no-thumb'>{name[:18]}</div>")
            cards  += f"""
        <div class="card" id="card{i}" onclick="toggle({i})">
          {img}
          <div class="badge" id="badge{i}"></div>
          <div class="label">{size_mb:.1f}MB</div>
        </div>"""
        except Exception:
            pass

    return f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scoopz</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0a0a;font-family:sans-serif}}
header{{background:#111;padding:14px 16px;border-bottom:1px solid #222;
        position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:10px}}
header h1{{color:#fff;font-size:15px;font-weight:600;flex:1}}
#counter{{font-size:12px;color:#888;white-space:nowrap}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:3px;padding:3px;padding-bottom:80px}}
.card{{position:relative;aspect-ratio:9/16;background:#1a1a1a;
       cursor:pointer;overflow:hidden;border-radius:4px;transition:opacity .15s}}
.card img{{width:100%;height:100%;object-fit:cover;display:block}}
.no-thumb{{width:100%;height:100%;display:flex;align-items:center;justify-content:center;
           color:#666;font-size:10px;padding:6px;text-align:center}}
.badge{{display:none;position:absolute;top:5px;left:5px;width:22px;height:22px;
        background:#1a73e8;border-radius:50%;color:#fff;font-size:11px;font-weight:700;
        align-items:center;justify-content:center;z-index:2}}
.badge.on{{display:flex}}
.card.sel{{outline:3px solid #1a73e8;outline-offset:-3px}}
.card.full{{opacity:.35;pointer-events:none}}
.label{{position:absolute;bottom:0;left:0;right:0;
        background:linear-gradient(transparent,rgba(0,0,0,.8));
        color:#ccc;font-size:10px;padding:12px 5px 4px;text-align:right}}
.bar{{position:fixed;bottom:0;left:0;right:0;background:#111;
      border-top:1px solid #222;padding:12px 16px;z-index:20;
      display:flex;align-items:center;gap:12px}}
.bar-info{{flex:1;font-size:13px;color:#888}}
.bar-info strong{{color:#fff}}
.gen-btn{{background:#1a73e8;color:#fff;border:none;border-radius:8px;
          padding:10px 22px;font-size:14px;font-weight:600;cursor:pointer;
          opacity:.4;transition:opacity .2s}}
.gen-btn.ready{{opacity:1}}
.overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.95);
          z-index:100;align-items:center;justify-content:center;
          flex-direction:column;gap:16px}}
.overlay.show{{display:flex}}
.spinner{{width:44px;height:44px;border:3px solid #333;
          border-top-color:#fff;border-radius:50%;
          animation:spin .75s linear infinite}}
.msg{{color:#fff;font-size:15px;font-weight:500}}
.step{{color:#666;font-size:12px;text-align:center;max-width:300px;line-height:1.5}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
</style>
</head>
<body>
<header>
  <h1>Scoopz</h1>
  <span id="counter">0 / {BATCH_CAP} selected</span>
</header>
<div class="grid" id="grid">{cards}</div>

<div class="bar">
  <div class="bar-info" id="barInfo">Select exactly <strong>{BATCH_CAP}</strong> videos</div>
  <button class="gen-btn" id="genBtn" onclick="generate()" disabled>Generate {BATCH_CAP} captions</button>
</div>

<div class="overlay" id="ov">
  <div class="spinner" id="spin"></div>
  <p class="msg" id="ovMsg">Processing...</p>
  <p class="step" id="ovStep"></p>
</div>

<script>
const CAP = {BATCH_CAP};
const sel = new Map(); // index → order (1-based)
let order = 0;

function toggle(i) {{
  if (sel.has(i)) {{
    // Deselect
    sel.delete(i);
    document.getElementById('badge'+i).classList.remove('on');
    document.getElementById('badge'+i).textContent = '';
    document.getElementById('card'+i).classList.remove('sel');
    // Renumber remaining
    order = 0;
    sel.clear();
    // Re-iterate in DOM order to preserve visual numbering
    document.querySelectorAll('.card.sel').forEach(c => {{
      const idx = parseInt(c.id.replace('card',''));
      order++;
      sel.set(idx, order);
      document.getElementById('badge'+idx).textContent = order;
    }});
    // rebuild sel from scratch properly
    rebuildSel(i);
  }} else {{
    if (sel.size >= CAP) return; // hard cap
    order++;
    sel.set(i, order);
    document.getElementById('badge'+i).classList.add('on');
    document.getElementById('badge'+i).textContent = order;
    document.getElementById('card'+i).classList.add('sel');
  }}
  updateUI();
}}

function rebuildSel(removedIdx) {{
  // After removal, renumber sequentially
  const remaining = [];
  document.querySelectorAll('.card.sel').forEach(c => {{
    remaining.push(parseInt(c.id.replace('card','')));
  }});
  sel.clear();
  order = 0;
  remaining.forEach(idx => {{
    order++;
    sel.set(idx, order);
    const b = document.getElementById('badge'+idx);
    b.classList.add('on');
    b.textContent = order;
  }});
}}

function updateUI() {{
  const n   = sel.size;
  const ctr = document.getElementById('counter');
  const btn = document.getElementById('genBtn');
  const inf = document.getElementById('barInfo');
  ctr.textContent = n + ' / ' + CAP + ' selected';
  // Grey out unselected cards when cap reached
  document.querySelectorAll('.card').forEach(c => {{
    const idx = parseInt(c.id.replace('card',''));
    if (n >= CAP && !sel.has(idx)) {{
      c.classList.add('full');
    }} else {{
      c.classList.remove('full');
    }}
  }});
  if (n === CAP) {{
    btn.disabled = false;
    btn.classList.add('ready');
    inf.innerHTML = '<strong>' + CAP + '</strong> videos selected — ready';
  }} else {{
    btn.disabled = true;
    btn.classList.remove('ready');
    const left = CAP - n;
    inf.innerHTML = 'Select <strong>' + left + '</strong> more video' + (left !== 1 ? 's' : '');
  }}
}}

async function generate() {{
  if (sel.size !== CAP) return;
  const indices = Array.from(sel.keys());
  showOverlay('Generating {BATCH_CAP} captions...', 'Extracting frames & transcribing...');
  const ok = await fetch('/batch', {{
    method:  'POST',
    headers: {{'Content-Type': 'application/json'}},
    body:    JSON.stringify({{indices}}),
  }}).then(r => r.ok).catch(() => false);
  if (!ok) {{ hideOverlay(); return; }}
  await pollStatus(300);
}}

function showOverlay(msg, step) {{
  document.getElementById('ovMsg').textContent  = msg;
  document.getElementById('ovStep').textContent = step;
  document.getElementById('spin').style.display = 'block';
  document.getElementById('ov').classList.add('show');
}}
function hideOverlay() {{
  document.getElementById('ov').classList.remove('show');
}}

async function pollStatus(maxTries) {{
  for (let t = 0; t < maxTries; t++) {{
    const st = await fetch('/status').then(r => r.json()).catch(() => ({{}}));
    const stage  = st.stage  || 'unknown';
    const detail = st.detail || '';
    document.getElementById('ovStep').textContent =
      stage === 'extracting'  ? ('Extracting: ' + detail) :
      stage === 'generating'  ? 'Calling Gemini (text only)...' :
      stage === 'copying'     ? ('Copying captions: ' + detail) :
      detail || 'Processing...';
    if (stage === 'done') {{
      document.getElementById('spin').style.display = 'none';
      document.getElementById('ovMsg').textContent  = '✅ All captions copied!';
      document.getElementById('ovStep').textContent = detail || 'Pick next batch';
      // Reset selections
      sel.clear(); order = 0;
      document.querySelectorAll('.card').forEach(c => {{
        c.classList.remove('sel','full');
        const idx = parseInt(c.id.replace('card',''));
        const b = document.getElementById('badge'+idx);
        if (b) {{ b.classList.remove('on'); b.textContent = ''; }}
      }});
      updateUI();
      setTimeout(hideOverlay, 2500);
      return true;
    }}
    if (stage === 'error') {{
      document.getElementById('spin').style.display = 'none';
      document.getElementById('ovMsg').textContent  = '❌ Error';
      document.getElementById('ovStep').textContent = detail;
      setTimeout(hideOverlay, 3000);
      return false;
    }}
    await new Promise(r => setTimeout(r, 750));
  }}
  document.getElementById('ovMsg').textContent  = '⏱ Timed out';
  document.getElementById('ovStep').textContent = 'Try again';
  setTimeout(hideOverlay, 2500);
  return false;
}}
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# HTTP SERVER
# ══════════════════════════════════════════════════════════════════════════════

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
    videos         = []
    api_key        = ""
    error_occurred = False

    def log_message(self, *a):
        pass

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global batch_job
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                body = build_html(self.videos).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)

            elif parsed.path == "/status":
                with processing_lock:
                    stage  = processing_stage
                    detail = processing_detail
                    err    = Handler.error_occurred
                self._json({"stage": "error" if err else stage, "detail": detail})

            else:
                self.send_response(404)
                self.end_headers()
        except Exception:
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:
                pass

    def do_POST(self):
        global batch_job
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/batch":
                length  = int(self.headers.get("Content-Length", 0))
                data    = json.loads(self.rfile.read(length))
                indices = [int(i) for i in (data.get("indices") or [])
                           if 0 <= int(i) < len(self.videos)]

                if len(indices) != BATCH_CAP:
                    self._json({"error": f"exactly {BATCH_CAP} videos required, got {len(indices)}"}, 400)
                    return

                with processing_lock:
                    caption_ready.clear()
                    Handler.error_occurred = False
                    batch_job = indices
                set_stage("queued", f"{BATCH_CAP} videos")
                self._json({"ok": True})
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            try:
                self._json({"error": str(e)[:80]}, 500)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PROCESSING LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_batch(api_key: str, video_indices: list, all_videos: list, perf: dict):
    """
    Full pipeline for one batch of BATCH_CAP videos.
    1. Extract all videos in parallel threads (frames + audio + transcript)
    2. Build text descriptions for each
    3. One text-only Gemini call
    4. Parse + sanitise results
    5. Copy each caption sequentially with 1.5s gap
    """
    n = len(video_indices)
    results    = {}  # slot → extraction dict
    lock       = threading.Lock()
    completed  = [0]

    def _extract(slot: int, vpath: str):
        r = extract_video(vpath, slot)
        with lock:
            results[slot] = r
            completed[0] += 1
            set_stage("extracting", f"{completed[0]}/{n} done")

    # Parallel extraction
    with _timer(perf, "extraction"):
        threads = [
            threading.Thread(target=_extract, args=(slot, all_videos[idx]), daemon=True)
            for slot, idx in enumerate(video_indices)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # Separate successes from failures
    success_slots = [s for s in range(n) if results.get(s, {}).get("ok")]
    failed_slots  = [s for s in range(n) if not results.get(s, {}).get("ok")]

    if failed_slots:
        print(f"⚠️  {len(failed_slots)} video(s) failed extraction — skipping")

    if not success_slots:
        raise ValueError("All videos failed extraction")

    # Build pov modes + prompt items for successes
    pov_modes  = [next_pov_mode() for _ in success_slots]
    prompt_items = [
        {
            "pov_mode":   pov_modes[i],
            "frame_desc": results[slot]["frame_desc"],
            "transcript": results[slot]["transcript"],
        }
        for i, slot in enumerate(success_slots)
    ]

    set_stage("generating", f"{len(success_slots)} captions")
    toast("Calling Gemini...")

    with _timer(perf, "gemini"):
        prompt   = build_batch_prompt(prompt_items)
        raw      = gemini_call(api_key, prompt, max_tokens=900 * len(success_slots))

    with _timer(perf, "format"):
        captions = format_batch_results(raw, pov_modes)

    # Pair captions with video paths
    final = []  # (vpath, caption)
    for i, slot in enumerate(success_slots):
        final.append((results[slot]["vpath"], captions[i] if i < len(captions) else ""))
    for slot in failed_slots:
        final.append((results[slot]["vpath"], ""))  # empty = failed

    # Copy to clipboard sequentially
    set_stage("copying", f"0/{len(final)}")
    copied = 0
    for k, (vpath, caption) in enumerate(final):
        name = os.path.basename(vpath)
        if not caption:
            print(f"⚠️  No caption for {name}")
            continue
        print(f"\n[{k+1}/{len(final)}] {name}\n{caption}\n")
        set_stage("copying", f"{k+1}/{len(final)}: {name[:24]}")
        ok = copy_to_clipboard(caption)
        if ok:
            copied += 1
        if k < len(final) - 1:
            time.sleep(1.5)

    summary = f"{copied}/{len(final)} captions copied"
    toast(summary)
    notify("Scoopz done", summary, "nb_status")
    set_stage("done", summary)
    caption_ready.set()

    # Cleanup all slots
    cleanup_all()


def main():
    global batch_job

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

    print(f"📼 Found {len(videos)} videos")
    videos = generate_thumbnails(videos)
    if not videos:
        toast("No readable videos!")
        sys.exit(1)

    if len(videos) < BATCH_CAP:
        print(f"⚠️  Only {len(videos)} readable videos found (need {BATCH_CAP} to fill a batch)")

    Handler.videos  = videos
    Handler.api_key = api_key

    kill_port(PORT)
    srv = ReusableServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(1)

    url = f"http://127.0.0.1:{PORT}"
    open_browser(url)

    try:
        subprocess.run([
            "termux-notification", "--id", "sz_picker",
            "--title", "Scoopz", "--content", f"Select {BATCH_CAP} videos to generate",
            "--button1", "Open", "--button1-action", f"termux-open-url {url}",
            "--ongoing",
        ], timeout=3, capture_output=True, check=False)
    except Exception:
        pass

    perf_on = os.environ.get("SCOOPZ_PERF", "1").strip() not in ("0", "false", "False")

    while True:
        with processing_lock:
            batch_job = None
            caption_ready.clear()
            Handler.error_occurred = False

        while batch_job is None:
            time.sleep(0.2)

        job  = batch_job
        perf: dict = {}

        try:
            run_batch(api_key, job, videos, perf)
        except Exception as e:
            err = str(e)[:100]
            print(f"\nERROR: {err}\n")
            toast(f"Error: {err[:50]}")
            notify("Error", err, "nb_status")
            Handler.error_occurred = True
            set_stage("error", err)
            caption_ready.set()
            cleanup_all()

        if perf_on and perf:
            try:
                total = sum(perf.values())
                parts = [f"{k}={v:.2f}s" for k, v in perf.items()]
                print(f"[perf] total={total:.2f}s  |  {' | '.join(parts)}")
            except Exception:
                pass


if __name__ == "__main__":
    main()