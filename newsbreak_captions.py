#!/usr/bin/env python3
"""
NewsBreak Caption Generator
Stack: ffmpeg (parallel) → 2 frames + audio clip → OpenAI (transcribe + vision) → caption
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
import math
import shutil
import tempfile
import requests
import secrets
from contextlib import contextmanager
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── Config ───────────────────────────────────────────────
# OpenAI auth:
# - Prefer env vars: SCOOPZ_OPENAI_API_KEY or OPENAI_API_KEY
# - Fallback to key file: ~/.config/openai_key.txt
KEY_FILE = os.path.expanduser(os.environ.get("SCOOPZ_OPENAI_KEY_FILE", "~/.config/openai_key.txt"))

# OpenAI API settings (override via env if desired)
OPENAI_BASE = os.environ.get("SCOOPZ_OPENAI_BASE", "https://api.openai.com/v1").rstrip("/")

# Persist POV alternation across runs (2nd person ↔ 3rd person)
POV_STATE_FILE = os.path.expanduser("~/.cache/scoopz_caption_pov.json")
USER_EXAMPLES_FILE = os.path.expanduser("~/.config/scoopz_caption_examples.txt")

# Optional: persist micro-style memory to reduce repetition across runs.
STYLE_MEMORY_FILE = os.path.expanduser("~/.cache/scoopz_caption_style_memory.json")

# Cache readability so we don't re-probe the same broken files every run.
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

def load_api_key():
    key = (os.environ.get("SCOOPZ_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    with open(KEY_FILE, "r", encoding="utf-8") as f:
        return (f.read() or "").strip()

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


def _file_b64(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return ""


def extract_evidence(vpath: str, slot: int) -> dict:
    """Extract evidence pack for remote processing: 2 frames + optional audio.

    This keeps the phone fast: no OCR, no Whisper, no LLM locally.
    """
    def _attempt():
        paths = _slot_paths(slot)

        transcribe_on = os.environ.get("SCOOPZ_TRANSCRIBE", "1").strip() not in ("0", "false", "False")

        f1_box, f2_box, audio_ok_box = [None], [None], [False]

        def _frames():
            r1, r2 = extract_best_frames(vpath, slot)
            f1_box[0], f2_box[0] = r1, r2

        def _audio():
            if not transcribe_on:
                audio_ok_box[0] = False
                return
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

        return {
            "vpath": vpath,
            "f1": f1,
            "f2": f2 or "",
            "audio": paths["audio"] if audio_ok_box[0] else "",
            "ok": True,
            "error": "",
        }

    for attempt in range(2):
        try:
            cleanup_slot(slot)
            return _attempt()
        except Exception as e:
            if attempt == 0:
                time.sleep(0.5)
            else:
                return {"vpath": vpath, "f1": "", "f2": "", "audio": "", "ok": False, "error": str(e)[:80]}

    return {"vpath": vpath, "f1": "", "f2": "", "audio": "", "ok": False, "error": "unknown"}


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
# (Signalstats + optional OCR; no online APIs)
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
    Build a compact text description of a frame for the LLM prompt.
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
    # IMPORTANT: do not auto-fall back to a generic binary name like `main`.
    # Both whisper.cpp and llama.cpp may build a `main` binary, so using `main`
    # here can accidentally execute the wrong program.
    #
    # If you built whisper.cpp from source, set:
    #   SCOOPZ_WHISPER_BIN=/path/to/whisper-cli
    # and optionally:
    #   SCOOPZ_WHISPER_MODEL=/path/to/ggml-*.bin
    model_path = os.environ.get(
        "SCOOPZ_WHISPER_MODEL",
        os.path.expanduser("~/.local/share/whisper/ggml-base.en.bin"),
    ).strip()
    forced_bin = os.environ.get("SCOOPZ_WHISPER_BIN", "").strip()
    bin_candidates = [forced_bin] if forced_bin else []
    bin_candidates += ["whisper-cli", "whisper_cpp"]

    for binary in bin_candidates:
        if not binary:
            continue
        bin_path = shutil.which(binary) if os.path.basename(binary) == binary else binary
        if bin_path and os.path.exists(model_path):
            try:
                r = subprocess.run(
                    [bin_path, "-m", model_path, "-f", src,
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

# ── OpenAI ────────────────────────────────────────────────
def _openai_error_text(resp) -> str:
    try:
        data = resp.json()
        err = data.get("error") or {}
        msg = (err.get("message") or "").strip()
        if msg:
            return msg
    except Exception:
        pass
    try:
        return (resp.text or "").strip()
    except Exception:
        return ""


def _openai_transcribe_audio(api_key: str, audio_path: str) -> str:
    """Best-effort audio → text using OpenAI transcription."""
    model = os.environ.get("SCOOPZ_OPENAI_TRANSCRIBE_MODEL", "whisper-1").strip() or "whisper-1"
    timeout_s = float(os.environ.get("SCOOPZ_OPENAI_TIMEOUT_S", "45").strip() or "45")
    url = OPENAI_BASE + "/audio/transcriptions"

    with open(audio_path, "rb") as f:
        files = {"file": ("audio.mp3", f, "audio/mpeg")}
        data = {
            "model": model,
            "response_format": "text",
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = requests.post(url, headers=headers, data=data, files=files, timeout=timeout_s)

    if not resp.ok:
        raise ValueError(f"OpenAI transcription error (HTTP {resp.status_code}): {_openai_error_text(resp)[:200]}")

    # response_format=text returns raw text; some setups return JSON.
    txt = (resp.text or "").strip()
    if txt and not txt.startswith("{"):
        return txt
    try:
        return (resp.json().get("text") or "").strip()
    except Exception:
        return txt


def generate_caption(api_key, frame_paths, audio_ok, prompt_text: str):
    """OpenAI call using frames (+ optional transcript from audio)."""
    chat_model = os.environ.get("SCOOPZ_OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"
    timeout_s = float(os.environ.get("SCOOPZ_OPENAI_TIMEOUT_S", "45").strip() or "45")
    do_transcribe = os.environ.get("SCOOPZ_TRANSCRIBE_AUDIO", "1").strip() not in ("0", "false", "False")

    transcript = ""
    if audio_ok and do_transcribe:
        transcript = _openai_transcribe_audio(api_key, TEMP_AUDIO)

    content = []
    text = prompt_text
    if transcript:
        text += "\n\nAUDIO TRANSCRIPT (best-effort):\n" + transcript
    content.append({"type": "text", "text": text})

    for fp in frame_paths:
        content.append({
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + file_b64(fp)},
        })

    payload = {
        "model": chat_model,
        "messages": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "temperature": 1.0,
        "max_tokens": 900,
    }

    url = OPENAI_BASE + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    for attempt in range(3):
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout_s)
        if resp.status_code == 429:
            time.sleep(10)
            continue
        if resp.status_code == 401:
            raise ValueError("OpenAI auth error (401). Check OPENAI_API_KEY / SCOOPZ_OPENAI_API_KEY.")
        if not resp.ok:
            msg = _openai_error_text(resp)
            print(f"OpenAI error {resp.status_code}: {msg[:300]}")
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()

    raise ValueError("OpenAI rate limit")

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
        """Return list of (caption, hashtags) pairs.

        The model sometimes varies formatting (markdown bolding, missing colons,
        headers on the same line, etc.). This parser is intentionally tolerant
        so we don't fall back to copying the entire raw multi-candidate blob.
        """
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            return []

        out: list[tuple[str, str]] = []

        # Pass 1: full candidate blocks.
        # Accept optional markdown emphasis around labels and optional colons.
        cand_pat = re.compile(
            r"(?ims)^\s*(?:\*\*?)?\s*candidate\s*(\d+)\s*(?:\*\*?)?\s*[:\-]?\s*(?:\n|\s)+"
            r"\s*(?:\*\*?)?\s*caption\s*[:\-]?\s*(?:\*\*?)?\s*(.*?)\n"
            r"\s*(?:\*\*?)?\s*(?:hashtags?|tags)\s*[:\-]?\s*(?:\*\*?)?\s*(.*?)"
            r"(?=\n\s*(?:\*\*?)?\s*candidate\s*\d+\b|\Z)",
        )
        for m in cand_pat.finditer(text):
            cap = (m.group(2) or "").strip()
            tags = (m.group(3) or "").strip()
            if cap and tags:
                out.append((cap, tags))

        if out:
            return out

        # Pass 2: caption/hashtags pairs without candidate headers.
        pair_pat = re.compile(
            r"(?ims)\bcaption\s*[:\-]?\s*(.*?)\n\s*(?:hashtags?|tags)\s*[:\-]?\s*(.*?)"
            r"(?=\n\s*\bcaption\s*[:\-]?|\Z)",
        )
        for m in pair_pat.finditer(text):
            cap = (m.group(1) or "").strip()
            tags = (m.group(2) or "").strip()
            if cap and tags:
                out.append((cap, tags))

        if out:
            return out

        # Pass 3: salvage Candidate 1 only (prevents copying all candidates).
        try:
            m1 = re.search(r"(?im)^\s*(?:\*\*?)?\s*candidate\s*1\b.*$", text)
            if m1:
                tail = text[m1.start():]
                m2 = re.search(r"(?im)^\s*(?:\*\*?)?\s*candidate\s*2\b.*$", tail)
                if m2:
                    tail = tail[:m2.start()]

                mcap = re.search(r"(?ims)\bcaption\s*[:\-]?\s*(.*?)\n\s*(?:hashtags?|tags)\s*[:\-]?\s*(.*)\Z", tail)
                if mcap:
                    cap = (mcap.group(1) or "").strip()
                    tags = (mcap.group(2) or "").strip()
                    if cap and tags:
                        return [(cap, tags)]
        except:
            pass

        return []

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
    showOverlay('Generating {BATCH_CAP} captions...', 'Extracting frames & audio...');
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
    stage === 'generating'  ? 'Generating on laptop...' :
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

# ── Main ──────────────────────────────────────────────────
def main():
    global selected_video, processing_stage, processing_detail

    try:
        api_key = load_api_key()
    except FileNotFoundError:
        notify("Error", "No API key at ~/.config/openai_key.txt", "nb_error")
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
            run_batch(job, videos, perf)
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