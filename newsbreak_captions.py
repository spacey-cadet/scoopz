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
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── Config ───────────────────────────────────────────────
KEY_FILE     = os.path.expanduser("~/.config/gemini_key.txt")
GEMINI_MODEL = "gemini-2.5-flash-lite"
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
CAPTION_PROMPT = """You are a viral caption writer for a hyperlocal news page.

I'm giving you:
1. Two frames from the video (at 10% and 50% of its duration)
2. The audio from the video — listen carefully to what is being said

Your job: write a caption that stops people mid-scroll.

CAPTION RULES:
- 1-2 sentences, punchy, present tense ("Watch as...", "Caught on camera...")
- 1-3 emojis placed naturally inline — not dumped at the end
- Use full location names if you can detect them (e.g. "Downtown Detroit" not "the area")
- NO hashtags inside the caption body
- NEVER use the words "NewsBreak", "TikTok", "App", or "Platform" in the caption text
- Caption itself: under 100 characters

HASHTAG RULES:
- 4-6 tags, all lowercase
- Always include one of: #breakingnews or #localnews
- Add a location tag if detectable (e.g. #chicago #miami #houston)
- Add 1-2 specific topic tags (e.g. #caraccident #flooding #protest #arrest #fire)
- NEVER use: #newsbreak #newsbreakapp #nb #tiktok #viral #fyp #foryou #trending or any vague filler tags

TOTAL length (caption + space + hashtags): 150 characters max.

OUTPUT FORMAT — output exactly this structure, nothing else, no extra commentary:
CAPTION:
[caption text here]

HASHTAGS:
[#tag1 #tag2 #tag3 #tag4]"""

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
    """Retries clipboard injection to bypass Android background limits."""
    for attempt in range(4):
        try:
            r = subprocess.run(
                ["termux-clipboard-set"],
                input=text.strip().encode("utf-8"),
                timeout=3, capture_output=True
            )
            if r.returncode == 0:
                return True
        except:
            pass
        time.sleep(1.0)
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
                        if len(parts) > 9 and parts.endswith(":" + port_hex):
                            inode = parts
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
                        os.kill(int(fd_path.split("/")), 9)
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

# ── Video loader (MediaStore-filtered) ───────────────────
def mediastore_filenames():
    """Returns set of valid Gallery filenames, or None if the query fails completely."""
    try:
        r = subprocess.run([
            "content", "query",
            "--uri", "content://media/external/video/media",
            "--projection", "_data",
        ], capture_output=True, timeout=10, text=True, check=False)
        
        if r.returncode != 0:
            return None
            
        names = set()
        for line in r.stdout.splitlines():
            if "_data=" in line:
                path = line.split("_data=", 1).strip()
                names.add(os.path.basename(path))
        return names
    except:
        return None

def load_videos():
    """Load valid videos, strictly enforcing Gallery state."""
    if not os.path.exists(SCOOPZ_DIR):
        return []

    all_files = [
        os.path.join(SCOOPZ_DIR, f)
        for f in os.listdir(SCOOPZ_DIR)
        if f.lower().endswith(".mp4") and not f.startswith(".")
    ]
    
    # Filter 1: Must physically exist AND be larger than 1KB (ignores ghost/trashed 0-byte files)
    existing = [v for v in all_files if os.path.exists(v) and os.path.getsize(v) > 1024]
    
    # Filter 2: Cross-reference with MediaStore
    known_names = mediastore_filenames()
    
    # If known_names is None, the query crashed. If it's [], the gallery is empty.
    if known_names is not None:
        # STRICT FILTER: Only keep files that are officially registered in the gallery.
        existing = [v for v in existing if os.path.basename(v) in known_names]
        
    existing.sort(key=os.path.getmtime, reverse=True)
    return existing

# ── Thumbnails (FIXED: only for valid videos) ────────────
def generate_thumbnails(videos):
    """
    Generate thumbnails ONLY for videos in the provided list.
    Delete orphaned thumbnails for videos no longer in the list.
    """
    os.makedirs(THUMB_DIR, exist_ok=True)
    
    # Get set of valid video basenames
    valid_names = {os.path.basename(v) for v in videos}
    
    # Delete orphaned thumbnails
    try:
        for thumb in os.listdir(THUMB_DIR):
            if thumb.endswith(".jpg"):
                source_name = thumb[:-4]  # strip .jpg
                if source_name not in valid_names:
                    try:
                        os.remove(os.path.join(THUMB_DIR, thumb))
                        print(f"🗑️  Deleted orphan thumbnail: {thumb}")
                    except:
                        pass
    except:
        pass
    
    total = len(videos)

    for idx, vpath in enumerate(videos):
        if idx % 5 == 0:
            toast(f"Loading {idx}/{total}...")

        name       = os.path.basename(vpath)
        thumb_path = os.path.join(THUMB_DIR, name + ".jpg")

        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
            continue

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

        # Fallback: colored placeholder
        if not success:
            try:
                color = f"0x{abs(hash(name)) % 0xFFFFFF:06x}"
                label = name[:12].replace("'", "").replace(":", "")
                subprocess.run([
                    "ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"color=c={color}:size=180x320:duration=1",
                    "-vframes", "1",
                    "-vf", (f"drawtext=text='{label}':fontcolor=white"
                            f":fontsize=14:x=(w-text_w)/2:y=(h-text_h)/2"),
                    thumb_path
                ], capture_output=True, timeout=10, check=False)
            except:
                pass

    toast("Ready")

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
    for seek, out in [
        (max(0.5, duration * 0.10), TEMP_FRAME_1),
        (max(1.0, duration * 0.50), TEMP_FRAME_2),
    ]:
        if os.path.exists(out):
            os.remove(out)
        try:
            subprocess.run([
                "ffmpeg", "-y", "-ss", str(seek), "-i", vpath,
                "-vframes", "1", "-q:v", "3", "-vf", "scale=640:-1",
                out
            ], capture_output=True, timeout=15, check=False)
        except:
            pass

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
            "temperature": 0.85,
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
        return resp.json()["candidates"]["content"]["parts"]["text"].strip()

    raise ValueError("Gemini rate limit")

def format_result(raw):
    """Parse CAPTION + HASHTAGS, enforce 150 char total."""
    try:
        if "CAPTION:" in raw and "HASHTAGS:" in raw:
            caption  = raw.split("HASHTAGS:").replace("CAPTION:", "").strip()
            hashtags = raw.split("HASHTAGS:").strip().split("\n").strip()
            combined = f"{caption} {hashtags}"
            if len(combined) > 150:
                combined = combined[:150].rsplit(" ", 1)
            return combined.strip()
    except:
        pass
    return raw[:150].rsplit(" ", 1) if len(raw) > 150 else raw

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
                # ANTI-CACHE HEADERS
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(body)

            elif parsed.path == "/pick":
                idx = int(parse_qs(parsed.query).get("i",))
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
        print("\n📂 0 videos found in gallery. Add some videos and run again.\n")
        sys.exit(1)

    print(f"📹 Found {len(videos)} valid videos")
    generate_thumbnails(videos)
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