#!/usr/bin/env python3
"""
NewsBreak Caption & Hashtag Generator
- Tap widget → browser opens with video thumbnail grid
- Tap a thumbnail → script processes that video
- Caption + hashtags copied to clipboard
"""
import subprocess
import sys
import os
import time
import base64
import threading
import random
import socket
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── Config ──────────────────────────────────────────────
KEY_FILE        = os.path.expanduser("~/.config/gemini_key.txt")
MODEL           = "gemini-2.5-flash"
SCOOPZ_DIR      = "/sdcard/Pictures/scoopz"
THUMB_DIR       = "/sdcard/Pictures/scoopz/.thumbs"
TEMP_COMPRESSED = "/sdcard/Download/nb_caption_compressed.mp4"
GEMINI_BASE     = "https://generativelanguage.googleapis.com"
PORT            = 8765

# ────────────────────────────────────────────────────────
selected_video = None
caption_ready  = threading.Event()

# ── Utilities ─────────────────────────────────────────────
def notify(title, msg):
    """Notification with timeout to prevent hanging."""
    try:
        subprocess.run(
            ["termux-notification", "--title", title, "--content", msg],
            timeout=5,
            capture_output=True
        )
    except (subprocess.TimeoutExpired, Exception):
        pass

def toast(msg):
    """Toast with timeout to prevent hanging."""
    try:
        subprocess.run(
            ["termux-toast", "-s", msg], 
            timeout=3,
            capture_output=True
        )
    except (subprocess.TimeoutExpired, Exception):
        pass

def copy_to_clipboard(text):
    try:
        subprocess.run(["termux-clipboard-set"], input=text.encode(), timeout=3)
    except (subprocess.TimeoutExpired, Exception):
        pass

def load_api_key():
    with open(KEY_FILE, "r") as f:
        return f.read().strip()

def kill_port(port):
    """Kill any process occupying the given port."""
    import glob
    # Check if port is already free
    try:
        test = socket.socket()
        test.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        test.bind(("127.0.0.1", port))
        test.close()
        return  # already free
    except OSError:
        pass
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
                    pid = int(fd_path.split("/")[2])
                    os.kill(pid, 9)
                    break
            except Exception:
                pass
    time.sleep(0.8)

# ── Thumbnails ────────────────────────────────────────────
def generate_thumbnails(videos):
    """Extract first frame from each video using ffmpeg with progress and error handling."""
    os.makedirs(THUMB_DIR, exist_ok=True)
    total = len(videos)
    skipped = 0
    
    for idx, video_path in enumerate(videos):
        # Show progress every 5 videos
        if idx % 5 == 0 or idx == 0:
            toast(f"Loading thumbnails... {idx}/{total}")
        
        vid_name  = os.path.basename(video_path)
        thumb_path = os.path.join(THUMB_DIR, vid_name + ".jpg")
        
        if not os.path.exists(thumb_path):
            try:
                subprocess.run([
                    "ffmpeg", "-y", "-i", video_path,
                    "-vframes", "1", "-q:v", "3",
                    "-vf", "scale=320:-1",
                    thumb_path
                ], capture_output=True, timeout=10)  # 10 sec timeout per video
            except (subprocess.TimeoutExpired, Exception) as e:
                # Skip problematic videos, continue with others
                print(f"⚠ Skipped {vid_name}: {str(e)[:50]}")
                skipped += 1
                continue
    
    if skipped > 0:
        toast(f"✓ Loaded {total - skipped}/{total} videos ({skipped} skipped)")
    else:
        toast(f"✓ Loaded {total} videos")

def get_thumb_b64(video_path):
    vid_name   = os.path.basename(video_path)
    thumb_path = os.path.join(THUMB_DIR, vid_name + ".jpg")
    if os.path.exists(thumb_path):
        with open(thumb_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    return ""

# ── Web server ────────────────────────────────────────────
def build_html(videos):
    cards = ""
    for i, vpath in enumerate(videos):
        b64      = get_thumb_b64(vpath)
        img_src  = f"data:image/jpeg;base64,{b64}" if b64 else ""
        size_mb  = os.path.getsize(vpath) / (1024 * 1024)
        img_tag  = f"<img src='{img_src}' loading='lazy'>" if img_src else "<div class='no-thumb'>&#9654;</div>"
        cards   += f"""
        <div class="card" onclick="pick({i})">
            {img_tag}
            <div class="label">{size_mb:.1f}MB</div>
        </div>"""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pick a Video</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0a0a0a; font-family: 'Helvetica Neue', sans-serif; min-height: 100vh; padding-bottom: 40px; }}
  header {{ background: #111; padding: 16px 20px; border-bottom: 1px solid #222; position: sticky; top: 0; z-index: 10; }}
  header h1 {{ color: #fff; font-size: 15px; font-weight: 600; letter-spacing: 0.5px; }}
  header span {{ color: #666; font-size: 12px; display: block; margin-top: 2px; }}
  .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 3px; padding: 3px; }}
  .card {{ position: relative; aspect-ratio: 9/16; background: #1a1a1a; cursor: pointer; overflow: hidden; border-radius: 4px; transition: transform 0.1s, opacity 0.1s; -webkit-tap-highlight-color: transparent; }}
  .card:active {{ transform: scale(0.95); opacity: 0.8; }}
  .card img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .no-thumb {{ width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: #333; font-size: 24px; }}
  .label {{ position: absolute; bottom: 0; left: 0; right: 0; background: linear-gradient(transparent, rgba(0,0,0,0.8)); color: #ccc; font-size: 10px; padding: 12px 6px 5px; text-align: right; }}
  .overlay {{ display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.85); z-index: 100; align-items: center; justify-content: center; flex-direction: column; gap: 12px; }}
  .overlay.show {{ display: flex; }}
  .spinner {{ width: 40px; height: 40px; border: 3px solid #333; border-top-color: #fff; border-radius: 50%; animation: spin 0.8s linear infinite; }}
  .overlay p {{ color: #fff; font-size: 14px; letter-spacing: 0.3px; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body>
<header>
  <h1>Pick a Video</h1>
  <span>{len(videos)} videos &middot; tap to generate caption</span>
</header>
<div class="grid">{cards}</div>
<div class="overlay" id="overlay">
  <div class="spinner" id="spinner"></div>
  <p id="status">Processing video&hellip;</p>
</div>
<script>
  function pick(i) {{
    document.getElementById('overlay').classList.add('show');
    document.getElementById('status').textContent = 'Processing video\u2026';
    fetch('/pick?i=' + i).then(() => {{
      return fetch('/status');
    }}).then(r => r.text()).then(result => {{
      if (result === 'done') {{
        document.getElementById('spinner').style.display = 'none';
        document.getElementById('status').textContent = '\u2705 Caption copied! Pick next video.';
        setTimeout(() => {{
          document.getElementById('overlay').classList.remove('show');
          document.getElementById('spinner').style.display = 'block';
          document.getElementById('status').textContent = 'Processing video\u2026';
        }}, 2000);
      }} else {{
        document.getElementById('status').textContent = 'Timed out. Try again.';
        setTimeout(() => document.getElementById('overlay').classList.remove('show'), 2000);
      }}
    }}).catch(() => {{
      document.getElementById('status').textContent = 'Something went wrong.';
    }});
  }}
</script>
</body>
</html>"""

class ReusableServer(HTTPServer):
    allow_reuse_address = True
    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        super().server_bind()

class Handler(BaseHTTPRequestHandler):
    videos = []
    def log_message(self, format, *args):
        pass
    def do_GET(self):
        global selected_video, caption_ready
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html = build_html(self.videos)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
        elif parsed.path == "/pick":
            params = parse_qs(parsed.query)
            idx    = int(params.get("i", [0])[0])
            caption_ready.clear()
            time.sleep(0.1)
            selected_video = self.videos[idx]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        elif parsed.path == "/status":
            ready = caption_ready.wait(timeout=180)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"done" if ready else b"timeout")
        else:
            self.send_response(404)
            self.end_headers()

# ── Gemini pipeline ───────────────────────────────────────
def compress_video(input_path):
    toast("Compressing...")
    if os.path.exists(TEMP_COMPRESSED):
        os.remove(TEMP_COMPRESSED)
    original_mb = os.path.getsize(input_path) / (1024 * 1024)
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", "scale=-2:480",
        "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        TEMP_COMPRESSED
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0 or not os.path.exists(TEMP_COMPRESSED):
            toast("Compression failed, using original...")
            return input_path
        compressed_mb = os.path.getsize(TEMP_COMPRESSED) / (1024 * 1024)
        saved = ((original_mb - compressed_mb) / original_mb) * 100
        toast(f"{original_mb:.1f}MB -> {compressed_mb:.1f}MB ({saved:.0f}% smaller)")
        return TEMP_COMPRESSED
    except subprocess.TimeoutExpired:
        toast("Compression timeout, using original...")
        return input_path

def upload_video(api_key, video_path):
    toast("Uploading to Gemini...")
    file_size = os.path.getsize(video_path)
    init_resp = requests.post(
        f"{GEMINI_BASE}/upload/v1beta/files?uploadType=resumable&key={api_key}",
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(file_size),
            "X-Goog-Upload-Header-Content-Type": "video/mp4",
            "Content-Type": "application/json",
        },
        json={"file": {"display_name": "newsbreak_video"}},
        timeout=30
    )
    init_resp.raise_for_status()
    upload_url = init_resp.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise ValueError("Failed to get upload URL from Gemini.")
    with open(video_path, "rb") as f:
        video_bytes = f.read()
    for attempt in range(4):
        upload_resp = requests.post(
            upload_url,
            headers={
                "Content-Length": str(file_size),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
            },
            data=video_bytes,
            timeout=180
        )
        if upload_resp.status_code == 429:
            wait = (attempt + 1) * 15
            toast(f"Upload rate limit — retrying in {wait}s...")
            time.sleep(wait)
            continue
        upload_resp.raise_for_status()
        break
    file_info = upload_resp.json()
    return file_info["file"]["uri"], file_info["file"]["name"]

def wait_for_processing(api_key, file_name):
    toast("Analysing content...")
    url = f"{GEMINI_BASE}/v1beta/{file_name}?key={api_key}"
    for _ in range(30):
        resp  = requests.get(url, timeout=15)
        resp.raise_for_status()
        state = resp.json().get("state", "")
        if state == "ACTIVE":
            return
        if state == "FAILED":
            raise ValueError("Gemini failed to process the video.")
        time.sleep(3)
    raise TimeoutError("Gemini took too long.")

def generate_caption(api_key, file_uri):
    url   = f"{GEMINI_BASE}/v1beta/models/{MODEL}:generateContent?key={api_key}"
    style = random.choice([
        {
            "length":    "exactly 1 sentence, between 30 and 70characters",
            "structure": "One punchy hook that stands alone. Caption needed. No CTA needed."
        },
        {
            "length":    "2 sentences, between 50 and 100 characters",
            "structure": "Sentence 1: strong hook. Sentence 2: a call to action (e.g. 'Follow for more!', 'Share this!', 'Tag someone!').Caption Needed"
        },
    ])
    prompt = (
        "Watch this video carefully and generate:\n\n"
        "1. A casual, conversational caption for NewsBreak.\n\n"
        f"   Length: {style['length']}\n"
        f"   Structure: {style['structure']}\n\n"
        "   Style rules:\n"
        "   - Casual and conversational - no stiff or formal language\n"
        "   - Use 1 to 3 relevant emojis placed naturally within the caption\n"
        "   - Do NOT use hashtags inside the caption\n"
        "   - If the video contains any location hints (city, country, landmark, street sign, flag, accent, or text overlay), "
        "naturally include the FULL location name — never abbreviations (e.g. 'New York' not 'NYC', 'United States' not 'US')\n"
        "   - If NO location is detected, do not invent one\n\n"
        "2. Between 4 and 7 hashtags for NewsBreak platform (no spaces, all lowercase, prefixed with #)\n"
        "   - Focus on NewsBreak, news, and viral content audiences\n"
        "   - NO TikTok, YouTube, Instagram or other platform-specific tags\n"
        "   - Mix: 1-2 broad trending tags + 2-3 niche topic tags based on the video content\n"
        "   - If a location was detected, include 1 location hashtag using common short form (e.g. #nyc #uk #sa #la)\n"
        "   - Caption + hashtags combined must not exceed 150 characters total\n\n"
        "Format your response EXACTLY like this with no extra text:\n"
        "CAPTION:\n"
        "[your caption here]\n\n"
        "HASHTAGS:\n"
        "[hashtags separated by spaces]"
    )
    payload = {"contents": [{"parts": [
        {"file_data": {"mime_type": "video/mp4", "file_uri": file_uri}},
        {"text": prompt}
    ]}]}
    for attempt in range(6):
        resp = requests.post(url, json=payload, timeout=120)
        if resp.status_code == 429:
            wait = (attempt + 1) * 15
            toast(f"Rate limit — retrying in {wait}s...")
            notify("Rate limit hit", f"Waiting {wait}s before retry {attempt+1}/6")
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            raise ValueError(f"Gemini API error {resp.status_code}: {resp.text[:200]}")
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    raise ValueError("Rate limit exceeded after 6 retries. Wait a minute and try again.")

def delete_file(api_key, file_name):
    try:
        requests.delete(f"{GEMINI_BASE}/v1beta/{file_name}?key={api_key}", timeout=10)
    except Exception:
        pass

def cleanup():
    if os.path.exists(TEMP_COMPRESSED):
        os.remove(TEMP_COMPRESSED)

def format_result(result):
    """Extract caption + hashtags and enforce 150 char combined limit."""
    if "CAPTION:" in result and "HASHTAGS:" in result:
        parts        = result.split("HASHTAGS:")
        caption_text = parts[0].replace("CAPTION:", "").strip()
        hashtag_text = parts[1].strip()
        combined     = caption_text + " " + hashtag_text
        if len(combined) > 150:
            combined = combined[:150].rsplit(" ", 1)[0]
        return combined.strip()
    # Fallback
    return result[:150].rsplit(" ", 1)[0] if len(result) > 150 else result

# ── Main ──────────────────────────────────────────────────
def main():
    global selected_video
    file_name = None
    api_key   = None
    try:
        api_key = load_api_key()
        videos = sorted([
            os.path.join(SCOOPZ_DIR, f)
            for f in os.listdir(SCOOPZ_DIR)
            if f.endswith(".mp4") and not f.startswith(".")
        ], key=os.path.getmtime, reverse=True)
        if not videos:
            toast("No videos found in scoopz folder!")
            sys.exit(1)
        toast(f"Found {len(videos)} videos, generating thumbnails...")
        generate_thumbnails(videos)
        Handler.videos = videos
        kill_port(PORT)
        server = ReusableServer(("127.0.0.1", PORT), Handler)
        import atexit
        atexit.register(server.shutdown)
        atexit.register(server.server_close)
        server_thread        = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        url = f"http://127.0.0.1:{PORT}"
        time.sleep(2)
        try:
            subprocess.run([
                "termux-notification",
                "--title",          "NewsBreak Caption",
                "--content",        "Tap to open video picker",
                "--action",         f"termux-open-url {url}",
                "--button1",        "Open Picker",
                "--button1-action", f"termux-open-url {url}",
                "--id",             "nb_picker",
                "--ongoing"
            ], timeout=5)
        except subprocess.TimeoutExpired:
            pass
        
        toast("Tap notification to open picker")
        # Process videos one by one indefinitely
        while True:
            selected_video = None
            caption_ready.clear()
            file_name = None
            while selected_video is None:
                time.sleep(0.3)
            current_video = selected_video
            toast(f"Processing: {os.path.basename(current_video)}")
            try:
                compressed          = compress_video(current_video)
                file_uri, file_name = upload_video(api_key, compressed)
                wait_for_processing(api_key, file_name)
                result              = generate_caption(api_key, file_uri)
                result              = format_result(result)
                copy_to_clipboard(result)
                caption_ready.set()
                notify("Caption Ready!", "Copied to clipboard. Open NewsBreak and paste.")
                toast("Copied! Tap next video when ready.")
                print("\n" + "=" * 50)
                print(result)
                print("=" * 50)
            except Exception as e:
                caption_ready.set()
                notify("Error", str(e))
                toast(f"Error: {str(e)[:60]}")
            finally:
                if file_name and api_key:
                    delete_file(api_key, file_name)
                cleanup()
    except FileNotFoundError:
        notify("Error", "Gemini API key not found. Check ~/.config/gemini_key.txt")
        sys.exit(1)
    except Exception as e:
        notify("Error", str(e))
        toast(f"Error: {str(e)[:60]}")
        sys.exit(1)

if __name__ == "__main__":
    main()