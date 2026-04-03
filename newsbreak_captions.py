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
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── Config ───────────────────────────────────────────────
KEY_FILE     = os.path.expanduser("~/.config/gemini_key.txt")
GEMINI_MODEL = "gemini-2.5-flash-lite"  # FIXED: Use stable model
GEMINI_BASE  = "https://generativelanguage.googleapis.com/v1beta/models"

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

# ── Prompt ───────────────────────────────────────────────
CAPTION_PROMPT = """You are a real person on the scene reacting to something happening in your area.

You're NOT a news reporter. You're NOT a brand. You're just a local who pulled out their phone to record this.

I'm giving you:
1. Two frames from the video
2. The audio from the video

Your job: Write a caption that stops people mid-scroll by sounding raw, observant, and deeply local.

CAPTION RULES:
- 1–2 short sentences max. Under 100 characters.
- Voice: First-person ("I", "we", "my") or immediate observation ("nah this is crazy", "can't believe this").
- Be Hyper-Specific: Instead of "a fire in the city," use exact details if visible/audible. Mention specific neighborhoods, street names, subway lines, or intersections if you detect them (e.g., "Washington Square Park", "the FDR", "Downtown").
- Sensory Focus: React to the intensity. How loud is it? How massive is the crowd? How bad is the traffic? Focus on the immediate vibe.
- Examples of the exact tone to use:
    - "Traffic on the BQE is completely cooked right now 🚗"
    - "Can't even hear myself think over these sirens outside the bodega."
    - "Rain is coming down sideways right now, stay inside."
    - "Knicks game was an absolute movie tonight 🏀"
- Don't invent details you aren't 100% sure about from the media.
- NO news-anchor phrases ("watch as", "caught on camera", "breaking:").
- NO brand mentions (no app names, no platforms). Do NOT say "newsbreak" or "tiktok".
- 1–2 emojis max, placed naturally inline.

HASHTAGS RULES:
- 4–5 tags only, all lowercase.
- Include ONE of: #breakingnews or #localnews
- Include the specific location/neighborhood if obvious (e.g., #brooklyn, #harlem, #nyc).
- Include 1–2 highly specific topic tags (e.g., #gridlock, #subway, #flooding, #blockparty).
- Avoid generic filler (#news, #update, #live).
- NO platform tags (#viral, #fyp, #newsbreak).

TOTAL length (caption + hashtags): under 150 characters

OUTPUT EXACTLY:

CAPTION:
caption text

HASHTAGS:
#tag1 #tag2 #tag3 #tag4"""

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
    for attempt in range(2):
        try:
            r = subprocess.run(
                ["termux-clipboard-set"],
                input=text.strip().encode("utf-8"),
                timeout=2, capture_output=True
            )
            if r.returncode == 0:
                return True
        except:
            pass
        if attempt == 0:
            time.sleep(0.5)
    return True

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
                "ffmpeg", "-v", "error",
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
                    "ffmpeg", "-y", "-ss", "1", "-i", vpath,
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
                    "ffmpeg", "-y", "-ss", str(seek), "-i", vpath,
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
                    "ffmpeg", "-y", "-i", vpath,
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
                "ffmpeg", "-y", "-ss", str(seek), "-i", vpath,
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
                "ffmpeg", "-y", "-i", vpath,
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
            "ffmpeg", "-y", "-i", vpath,
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
def generate_caption(api_key, frame_paths, audio_ok):
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

    parts.append({"text": CAPTION_PROMPT})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 1.0,
            "maxOutputTokens": 220
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

def format_result(raw):
    """Parse CAPTION + HASHTAGS, enforce 150 char total."""
    try:
        if "CAPTION:" in raw and "HASHTAGS:" in raw:
            caption  = raw.split("HASHTAGS:")[0].replace("CAPTION:", "").strip()
            hashtags = raw.split("HASHTAGS:")[1].strip().split("\n")[0].strip()
            hashtags = hashtags.strip().strip("[](){}")

            # Hard guardrails: no hashtags or app-name leakage in CAPTION
            caption = re.sub(r"#\w+", "", caption)
            caption = re.sub(r"\bnewsbreak\b", "", caption, flags=re.IGNORECASE)
            caption = re.sub(r"\s+([?.!,])", r"\1", caption)
            caption = re.sub(r"\s{2,}", " ", caption).strip()
            if len(caption) > 100:
                caption = caption[:100].rsplit(" ", 1)[0].strip()

            # Hashtag sanitation: drop platform/app + low-signal filler, enforce required tags
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

            # If we got too strict and ended up with <4 tags, allow a couple generic (non-platform) tags.
            if len(clean_tags) < 4:
                for tag in non_banned:
                    if tag in seen:
                        continue
                    if tag not in low_signal:
                        continue
                    clean_tags.append(tag)
                    seen.add(tag)
                    if len(clean_tags) >= 4:
                        break

            # Cap to 5 tags
            clean_tags = clean_tags[:5]
            hashtags = " ".join(clean_tags)

            combined = f"{caption} {hashtags}"
            if len(combined) > 150:
                combined = combined[:150].rsplit(" ", 1)[0]
            return combined.strip()
    except:
        pass
    return raw[:150].rsplit(" ", 1)[0] if len(raw) > 150 else raw

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
    await fetch('/pick?i=' + i);
    const ticker = setInterval(() => {{ step.textContent = 'Generating caption...'; }}, 6000);
    const result = await fetch('/status').then(r => r.text());
    clearInterval(ticker);
    if (result === 'done') {{
      spin.style.display = 'none';
      msg.textContent  = 'Copied!';
      step.textContent = 'Pick next video';
      setTimeout(() => {{
        ov.classList.remove('show');
        spin.style.display = 'block';
      }}, 1600);
    }} else {{
      msg.textContent  = 'Error - try again';
      step.textContent = '';
      setTimeout(() => ov.classList.remove('show'), 2500);
    }}
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
        global selected_video, caption_ready
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
                with processing_lock:
                    caption_ready.clear()
                    Handler.error_occurred = False
                    selected_video = self.videos[idx]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")

            elif parsed.path == "/status":
                ready = caption_ready.wait(timeout=120)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                if Handler.error_occurred:
                    self.wfile.write(b"error")
                elif ready:
                    self.wfile.write(b"done")
                else:
                    self.wfile.write(b"timeout")

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
    global selected_video

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

        while selected_video is None:
            time.sleep(0.2)

        current = selected_video

        try:
            toast("Extracting...")
            frame_paths, audio_ok = extract_parallel(current)

            if not frame_paths:
                raise ValueError("Could not extract frames")

            toast("Generating caption...")
            raw    = generate_caption(api_key, frame_paths, audio_ok)
            result = format_result(raw)

            print(f"\n{result}\n")
            copy_to_clipboard(result)

            toast("Copied!")
            notify("Ready", "Caption copied", "nb_status")
            caption_ready.set()

        except Exception as e:
            err = str(e)[:80]
            print(f"\nERROR: {err}\n")
            toast(f"Error: {err[:50]}")
            notify("Error", err, "nb_status")
            Handler.error_occurred = True
            caption_ready.set()

        finally:
            cleanup()

if __name__ == "__main__":
    main()