#!/data/data/com.termux/files/usr/bin/bash
# ══════════════════════════════════════════════════════
#  scoopz_env.sh  –  One-line commands for Scoopz workflow
#
#  HOW TO INSTALL (run once):
#    echo "source ~/scoopz/scoopz_env.sh" >> ~/.bashrc && source ~/.bashrc
# ══════════════════════════════════════════════════════
# App code location
export SCOOPZ_APP_DIR="${SCOOPZ_APP_DIR:-$HOME/scoopz}"

# Media location (used by collector/captions scripts)
export SCOOPZ_MEDIA_DIR="${SCOOPZ_MEDIA_DIR:-/sdcard/Pictures/scoopz}"

# Keep SCOOPZ_DIR for script compatibility (media path)
export SCOOPZ_DIR="$SCOOPZ_MEDIA_DIR"

# ── Config files (Termux) ───────────────────────────────────────────────────
SCOOPZ_CONFIG_DIR="$HOME/.config/scoopz"
SCOOPZ_REMOTE_URL_FILE="$SCOOPZ_CONFIG_DIR/remote_url.txt"

# ── Hybrid Evidence Mode (phone → laptop) ───────────────────────────────────
# Set this to your laptop server base URL (run `python3 scoopz_laptop_server.py` on laptop)
# Example: export SCOOPZ_REMOTE_URL="http://192.168.0.103:9000"
export SCOOPZ_REMOTE_URL="${SCOOPZ_REMOTE_URL:-}"

# If not set in the current shell, load it from config file.
if [ -z "$SCOOPZ_REMOTE_URL" ] && [ -f "$SCOOPZ_REMOTE_URL_FILE" ]; then
    _scoopz_ru=$(head -n 1 "$SCOOPZ_REMOTE_URL_FILE" 2>/dev/null | tr -d '\r\n' | sed 's/[[:space:]]*$//')
    if [ -n "$_scoopz_ru" ]; then
        export SCOOPZ_REMOTE_URL="$_scoopz_ru"
    fi
    unset _scoopz_ru
fi

# If 1: phone extracts short audio clips for laptop Whisper. If 0: frames only.
export SCOOPZ_TRANSCRIBE="${SCOOPZ_TRANSCRIBE:-1}"

# ── Remote URL helpers ──────────────────────────────────────────────────────

_scoopz__normalize_url() {
    # Accept: 192.168.0.10  OR  192.168.0.10:9000  OR  http://192.168.0.10:9000
    local v="$1"
    v="${v#http://}"; v="${v#https://}"
    if [[ "$v" != *:* ]]; then
        v="$v:9000"
    fi
    echo "http://$v"
}

scoopz-remote-show() {
    echo "SCOOPZ_REMOTE_URL=${SCOOPZ_REMOTE_URL:-<not set>}"
    if [ -f "$SCOOPZ_REMOTE_URL_FILE" ]; then
        echo "(saved) $(head -n 1 "$SCOOPZ_REMOTE_URL_FILE" 2>/dev/null | tr -d '\r\n')"
    else
        echo "(saved) <none>"
    fi
}

scoopz-remote-set() {
    # Usage: scoopz-remote-set 192.168.0.103[:9000]
    if [ -z "$1" ]; then
        echo "Usage: scoopz-remote-set <laptop_ip[:port] | full_url>"
        return 2
    fi
    local url
    url=$(_scoopz__normalize_url "$1")
    export SCOOPZ_REMOTE_URL="$url"
    mkdir -p "$SCOOPZ_CONFIG_DIR"
    printf '%s\n' "$url" > "$SCOOPZ_REMOTE_URL_FILE"
    chmod 600 "$SCOOPZ_REMOTE_URL_FILE" 2>/dev/null || true
    echo "✅ Set SCOOPZ_REMOTE_URL=$SCOOPZ_REMOTE_URL"
}

scoopz-remote-test() {
    # Usage: scoopz-remote-test   (tests current)
    local url="${1:-$SCOOPZ_REMOTE_URL}"
    if [ -z "$url" ]; then
        echo "SCOOPZ_REMOTE_URL not set. Use: scoopz-remote-set <ip>"
        return 2
    fi
    if ! command -v curl >/dev/null 2>&1; then
        echo "curl not found; install: pkg install -y curl"
        return 2
    fi
    echo "Testing: $url/health"
    curl -sS --max-time 2 "$url/health" || return 1
    echo ""
}

scoopz-remote-auto() {
    # Best-effort: checks current Wi‑Fi neighbors for an HTTP server on :9000/health.
    # This is fast if your laptop is already in the neighbor table.
    if ! command -v curl >/dev/null 2>&1; then
        echo "curl not found; install: pkg install -y curl"
        return 2
    fi
    if ! command -v ip >/dev/null 2>&1; then
        echo "ip command not found; install: pkg install -y iproute2"
        return 2
    fi

    local candidates=()
    local ip_list
    ip_list=$(ip neigh 2>/dev/null | awk '{print $1}' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | sort -u)

    # Prefer gateway if available
    local gw
    gw=$(ip route 2>/dev/null | awk '/default/ {print $3; exit}')
    if [ -n "$gw" ]; then
        candidates+=("$gw")
    fi

    while IFS= read -r line; do
        [ -n "$line" ] && candidates+=("$line")
    done <<< "$ip_list"

    local c url body
    for c in "${candidates[@]}"; do
        url="http://$c:9000"
        body=$(curl -sS --max-time 1 "$url/health" 2>/dev/null || true)
        if echo "$body" | grep -q '"ok"'; then
            scoopz-remote-set "$url"
            echo "✅ Found laptop server at $url"
            return 0
        fi
    done

    echo "❌ Could not auto-detect laptop server."
    echo "Tip: run 'hostname -I' on the laptop, then: scoopz-remote-set <ip>"
    return 1
}

# ── collect ───────────────────────────────────────────────────────────────────
collect() {
    echo "📋 Starting collector — browse TikTok and Share → Copy Link"
    cd "$SCOOPZ_APP_DIR" && python collector.py
}

# ── NewsBreak Caption Generator ──────────────────────────────────────────────
caption() {
    echo "🎬 Starting NewsBreak caption generator..."
    cd "$SCOOPZ_APP_DIR" || return 1
    
    if [ ! -f "newsbreak_captions.py" ]; then
        echo "⚠️  newsbreak_captions.py not found in $SCOOPZ_APP_DIR"
        return 1
    fi
    
    python newsbreak_captions.py
}

caption-bg() {
    echo "🎬 Starting NewsBreak caption generator in background..."
    cd "$SCOOPZ_APP_DIR" || return 1
    
    if [ ! -f "newsbreak_captions.py" ]; then
        echo "⚠️  newsbreak_captions.py not found in $SCOOPZ_APP_DIR"
        return 1
    fi
    
    mkdir -p logs
    
    nohup python newsbreak_captions.py > logs/captions.log 2>&1 &
    local pid=$!
    echo "✅ Caption generator running (PID: $pid)"
    echo "   Tap the notification to open video picker"
    echo "   Stop with: caption-stop or kill $pid"
}

caption-stop() {
    pkill -f "newsbreak_captions.py"
    echo "🛑 Stopped NewsBreak caption generator"
}

caption-logs() {
    tail -f "$SCOOPZ_APP_DIR/logs/captions.log"
}

# ── Status ────────────────────────────────────────────────────────────────────
scoopz-status() {
    echo ""
    echo "══════════════════════════════════════"
    echo "       Scoopz Workflow Status"
    echo "══════════════════════════════════════"
    
    echo "── Caption Generator ──────────────────"
    if pgrep -f "newsbreak_captions.py" > /dev/null; then
        local pid
        pid=$(pgrep -f "newsbreak_captions.py")
        echo "   Status: ✅ Running (PID: $pid)"
    else
        echo "   Status: ⚪ Not running"
    fi
    
    echo ""
    echo "── Videos ─────────────────────────────"
    local total
    total=$(ls "$SCOOPZ_MEDIA_DIR/"*.mp4 2>/dev/null | wc -l)
    echo "   Total videos: $total"
    echo "   Media dir: $SCOOPZ_MEDIA_DIR"
    
    echo ""
    echo "── Recent Logs ────────────────────────"
    if [ -f "$SCOOPZ_APP_DIR/logs/captions.log" ]; then
        echo "   Caption log (last 3 lines):"
        tail -3 "$SCOOPZ_APP_DIR/logs/captions.log" 2>/dev/null || echo "   (empty)"
    else
        echo "   No logs yet"
    fi
    echo "══════════════════════════════════════"
}

# ── Help ──────────────────────────────────────────────────────────────────────
scoopz-help() {
    echo ""
    echo "  ── Collection ───────────────────────────────"
    echo "  collect              TikTok link collector"
    echo ""
    echo "  ── NewsBreak Captions ───────────────────────"
    echo "  caption           Start caption generator (interactive)"
    echo "  caption-bg        Start in background"
    echo "  caption-stop      Stop background process"
    echo "  caption-logs      View logs"
    echo ""
    echo "  ── Status ───────────────────────────────────"
    echo "  scoopz-status        Check what's running"
    echo ""
    echo "  ── Workflow ─────────────────────────────────"
    echo "  1. collect           ← browse TikTok, share links"
    echo "  2. caption-bg     ← start caption generator"
    echo "  3. tap notification  ← open picker, select videos"
    echo "  4. caption-stop   ← when done"
    echo ""
}

echo "✅ Scoopz ready — 'scoopz-help' for commands"