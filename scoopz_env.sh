#!/data/data/com.termux/files/usr/bin/bash
# ══════════════════════════════════════════════════════
#  scoopz_env.sh  –  One-line commands for Scoopz workflow
#
#  HOW TO INSTALL (run once):
#    echo "source ~/scoopz/scoopz_env.sh" >> ~/.bashrc && source ~/.bashrc
# ══════════════════════════════════════════════════════
export SCOOPZ_DIR="$HOME/scoopz"

# ── Hybrid Evidence Mode (phone → laptop) ───────────────────────────────────
# Set this to your laptop server base URL (run `python3 scoopz_laptop_server.py` on laptop)
# Example: export SCOOPZ_REMOTE_URL="http://192.168.0.103:9000"
export SCOOPZ_REMOTE_URL="${SCOOPZ_REMOTE_URL:-}"

# If 1: phone extracts short audio clips for laptop Whisper. If 0: frames only.
export SCOOPZ_TRANSCRIBE="${SCOOPZ_TRANSCRIBE:-1}"

# ── collect ───────────────────────────────────────────────────────────────────
collect() {
    echo "📋 Starting collector — browse TikTok and Share → Copy Link"
    cd "$SCOOPZ_DIR" && python collector.py
}

# ── NewsBreak Caption Generator ──────────────────────────────────────────────
caption() {
    echo "🎬 Starting NewsBreak caption generator..."
    cd "$SCOOPZ_DIR" || return 1
    
    if [ ! -f "newsbreak_captions.py" ]; then
        echo "⚠️  newsbreak_captions.py not found in $SCOOPZ_DIR"
        return 1
    fi
    
    python newsbreak_captions.py
}

caption-bg() {
    echo "🎬 Starting NewsBreak caption generator in background..."
    cd "$SCOOPZ_DIR" || return 1
    
    if [ ! -f "newsbreak_captions.py" ]; then
        echo "⚠️  newsbreak_captions.py not found in $SCOOPZ_DIR"
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
    tail -f "$SCOOPZ_DIR/logs/captions.log"
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
    total=$(ls "$SCOOPZ_DIR/"*.mp4 2>/dev/null | wc -l)
    echo "   Total videos: $total"
    
    echo ""
    echo "── Recent Logs ────────────────────────"
    if [ -f "$SCOOPZ_DIR/logs/captions.log" ]; then
        echo "   Caption log (last 3 lines):"
        tail -3 "$SCOOPZ_DIR/logs/captions.log" 2>/dev/null || echo "   (empty)"
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