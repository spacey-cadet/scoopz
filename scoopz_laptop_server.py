#!/usr/bin/env python3
"""Scoopz Laptop Server — Hybrid Evidence Mode (OpenAI-only)

Receives evidence packs from the phone (2 frames + optional audio) and returns
captions generated using OpenAI from text-only evidence (OCR + Whisper output).

Endpoints:
  GET  /health  -> {"ok": true}
  POST /batch   -> {"captions": [..]}  body: {"items": [{name,f1_b64,f2_b64,audio_b64}, ...]}

Env vars:
  SCOOPZ_SERVER_PORT           (default: 9000)
  SCOOPZ_SERVER_BIND           (default: 0.0.0.0)

    # OpenAI (caption generation)
    SCOOPZ_OPENAI_API_KEY        (required) or OPENAI_API_KEY
    SCOOPZ_OPENAI_MODEL          (default: gpt-4o-mini)
    SCOOPZ_OPENAI_TIMEOUT_S      (default: 120)
    SCOOPZ_OPENAI_TEMP           (default: 0.8)
    SCOOPZ_LLM_MAX_TOKENS        (default 1200)

  # whisper.cpp
  SCOOPZ_TRANSCRIBE            (default 1)
  SCOOPZ_WHISPER_MODEL         (default ~/.local/share/whisper/ggml-base.en.bin)

  # OCR
  tesseract must be installed to enable OCR; otherwise it is skipped.

Run:
  python3 scoopz_laptop_server.py
"""

import base64
import json
import os
import tempfile
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Reuse your existing caption logic from the phone script.
# This keeps the prompt/parser/style memory consistent.
import newsbreak_captions as core  # type: ignore


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)).strip())
    except Exception:
        return default

def _openai_key() -> str:
    return (os.environ.get("SCOOPZ_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()


def openai_chat_call(prompt: str, *, max_tokens: int) -> str:
    """Call OpenAI Chat Completions API with a single user prompt (text-only)."""
    api_key = _openai_key()
    if not api_key:
        raise ValueError("Set SCOOPZ_OPENAI_API_KEY (or OPENAI_API_KEY) for OpenAI provider")

    model = (os.environ.get("SCOOPZ_OPENAI_MODEL") or "gpt-4o-mini").strip()
    temp = _env_float("SCOOPZ_OPENAI_TEMP", 0.8)
    timeout_s = _env_int("SCOOPZ_OPENAI_TIMEOUT_S", 120)

    # Keep it dependency-free (requests already used by the phone side; laptops usually have it too).
    import requests  # local import to avoid unused dependency warnings in some environments

    url = (os.environ.get("SCOOPZ_OPENAI_BASE_URL") or "https://api.openai.com").strip().rstrip("/") + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temp,
        "max_tokens": int(max(64, max_tokens)),
    }

    last_err = None
    for _ in range(2):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=timeout_s)
            if r.status_code >= 400:
                # Avoid dumping response body (may include account info). Keep errors short.
                raise ValueError(f"OpenAI API error: HTTP {r.status_code}")
            data = r.json() if r.content else {}
            content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
            if content:
                return content
            raise ValueError("OpenAI returned empty content")
        except Exception as e:
            last_err = e
    raise ValueError(str(last_err) if last_err else "OpenAI call failed")


def _b64_to_file(b64: str, out_path: str) -> bool:
    if not b64:
        return False
    try:
        raw = base64.b64decode(b64.encode("ascii"), validate=False)
        if not raw:
            return False
        with open(out_path, "wb") as f:
            f.write(raw)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception:
        return False


class ReusableServer(HTTPServer):
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # Keep terminal clean.
        pass

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.rstrip("/") != "/batch":
            return self._json({"error": "not found"}, 404)

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except Exception:
            length = 0

        try:
            data = json.loads((self.rfile.read(length) or b"{}").decode("utf-8", "ignore"))
        except Exception:
            return self._json({"error": "invalid json"}, 400)

        items = data.get("items") or []
        if not isinstance(items, list) or not items:
            return self._json({"error": "items required"}, 400)
        if len(items) > 8:
            return self._json({"error": "max 8 items per batch"}, 400)

        # Generate captions synchronously; the phone already shows progress.
        captions: list[str] = []

        with tempfile.TemporaryDirectory(prefix="scoopz_evidence_") as td:
            prompt_items = []
            pov_modes = []

            transcribe_on = os.environ.get("SCOOPZ_TRANSCRIBE", "1").strip() not in ("0", "false", "False")

            for idx, it in enumerate(items, 1):
                name = str(it.get("name") or f"video_{idx}")
                f1p = os.path.join(td, f"{idx}_f1.jpg")
                f2p = os.path.join(td, f"{idx}_f2.jpg")
                ap  = os.path.join(td, f"{idx}_a.mp3")
                tp  = os.path.join(td, f"{idx}_trim.mp3")

                ok1 = _b64_to_file(str(it.get("f1_b64") or ""), f1p)
                ok2 = _b64_to_file(str(it.get("f2_b64") or ""), f2p)
                oka = _b64_to_file(str(it.get("audio_b64") or ""), ap)

                if not ok1:
                    prompt_items.append({
                        "pov_mode": "THIRD PERSON",
                        "frame_desc": "No frames available.",
                        "transcript": "",
                    })
                    pov_modes.append(core.next_pov_mode())
                    continue

                frame_desc = core.build_frame_descriptions(f1p, f2p if ok2 else "")

                transcript = ""
                if transcribe_on and oka:
                    transcript = core.transcribe(ap, tp)

                pov = core.next_pov_mode()
                pov_modes.append(pov)
                prompt_items.append({
                    "pov_mode": pov,
                    "frame_desc": frame_desc,
                    "transcript": transcript,
                    "name": name,
                })

            prompt = core.build_batch_prompt(prompt_items)

            max_tokens = _env_int("SCOOPZ_LLM_MAX_TOKENS", 1200)
            raw = openai_chat_call(prompt, max_tokens=max_tokens)
            captions = core.format_batch_results(raw, pov_modes)

        # Always return same length as input.
        if len(captions) < len(items):
            captions += [""] * (len(items) - len(captions))
        captions = captions[: len(items)]

        return self._json({"captions": captions})


def main():
    bind = os.environ.get("SCOOPZ_SERVER_BIND", "0.0.0.0").strip() or "0.0.0.0"
    port = _env_int("SCOOPZ_SERVER_PORT", 9000)

    # Quick config sanity checks.
    if not _openai_key():
        raise SystemExit("Set SCOOPZ_OPENAI_API_KEY (or OPENAI_API_KEY)")

    httpd = ReusableServer((bind, port), Handler)
    print(f"[scoopz] laptop server listening on http://{bind}:{port}  (llm=openai)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
