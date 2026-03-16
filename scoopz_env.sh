#!/data/data/com.termux/files/usr/bin/bash
# ══════════════════════════════════════════════════════
#  scoopz_env.sh  –  One-line commands for Scoopz workflow
#
#  HOW TO INSTALL (run once):
#    echo "source ~/scoopz/scoopz_env.sh" >> ~/.bashrc && source ~/.bashrc
# ══════════════════════════════════════════════════════

export SCOOPZ_DIR="$HOME/scoopz"
export SCOOPZ_SHARED="$HOME/storage/shared/scoopz_downloads"

# ── collect ───────────────────────────────────────────────────────────────────
collect() {
    echo "📋 Starting collector — browse TikTok and Share → Copy Link"
    cd "$SCOOPZ_DIR" && python collector.py
}

# ── upload ────────────────────────────────────────────────────────────────────
upload() {
    echo "🔍 Checking for connected device..."
    if ! adb devices | grep -q "device$"; then
        echo "⚠️  No device found. Connect phone via USB then try again."
        return 1
    fi

    local count
    count=$(ls "$SCOOPZ_DIR/downloads/"*.mp4 2>/dev/null | wc -l)
    if [ "$count" -eq 0 ]; then
        echo "⚠️  Nothing to upload. Run 'collect' while browsing TikTok first."
        return 1
    fi

    echo "📦 $count MP4(s) ready — starting uploader"
    cd "$SCOOPZ_DIR" && python uploader.py
}

# ── Captions ─────────────────────────────────────────────────────────────────
next-caption() {
    cd "$SCOOPZ_DIR" || return 1
    local txt
    txt=$(ls downloads/*.txt 2>/dev/null | head -n 1)
    if [ -z "$txt" ]; then
        echo "⚠️  No captions ready"
        command -v termux-toast >/dev/null 2>&1 && termux-toast "No captions ready"
        return 1
    fi

    local caption
    caption=$(cat "$txt")

    if command -v termux-clipboard-set >/dev/null 2>&1; then
        printf "%s" "$caption" | termux-clipboard-set
        if command -v termux-toast >/dev/null 2>&1; then
            termux-toast "📋 Copied: ${caption:0:40}..."
        fi
    elif command -v pbcopy >/dev/null 2>&1; then
        printf "%s" "$caption" | pbcopy
    elif command -v xclip >/dev/null 2>&1; then
        printf "%s" "$caption" | xclip -selection clipboard
    else
        echo "⚠️  Clipboard utility not found; caption printed below:"
        echo "----------------------------------------"
        printf "%s\n" "$caption"
        echo "----------------------------------------"
    fi

    mkdir -p downloads/done
    local base
    base=${txt%.txt}
    mv "$base.mp4" downloads/done/ 2>/dev/null
    mv "$txt" downloads/done/
    echo "✅ Caption archived to downloads/done/"
}

install-next-caption-widget() {
    mkdir -p "$HOME/.shortcuts"
    cat <<'EOF' > "$HOME/.shortcuts/next-caption.sh"
#!/data/data/com.termux/files/usr/bin/bash
cd "$HOME/scoopz" || exit 1
txt=$(ls downloads/*.txt 2>/dev/null | head -1)
if [ -z "$txt" ]; then
    termux-toast "No captions ready"
    exit 0
fi
caption=$(cat "$txt")
printf "%s" "$caption" | termux-clipboard-set
termux-toast "📋 Copied: ${caption:0:40}..."
mkdir -p downloads/done
base=${txt%.txt}
mv "$base.mp4" downloads/done/ 2>/dev/null
mv "$txt" downloads/done/
EOF
    chmod +x "$HOME/.shortcuts/next-caption.sh"
    echo "✅ Termux widget script ready — add it via Termux:Widget"
}

# ── Cleanup ──────────────────────────────────────────────────────────────────
scoopz-clean() {
    cd "$SCOOPZ_DIR" || return 1

    local keep="${1:-0}"
    if ! [[ "$keep" =~ ^[0-9]+$ ]]; then
        echo "Usage: scoopz-clean [keep_recent_count]"
        return 1
    fi

    local -a mp4s=()
    if command -v python3 >/dev/null 2>&1; then
        mapfile -t mp4s < <(python3 - <<'PY'
from pathlib import Path
downloads = Path('downloads')
files = sorted(downloads.glob('*.mp4'), key=lambda p: p.stat().st_mtime, reverse=True)
for f in files:
    print(f)
PY
) || true
    else
        mapfile -t mp4s < <(ls -1t downloads/*.mp4 2>/dev/null || true)
    fi

    if [ ${#mp4s[@]} -eq 0 ]; then
        echo "Nothing to clean inside downloads/"
    else
        local removed=0
        for idx in "${!mp4s[@]}"; do
            if (( idx >= keep )); then
                local file="${mp4s[idx]}"
                rm -f "$file"
                rm -f "${file%.mp4}.txt"
                ((removed++))
            fi
        done
        echo "🧹 Removed $removed old video(s) from downloads/ (kept $keep)."
    fi

    if [ -d downloads/done ]; then
        local done_count
        done_count=$(find downloads/done -type f 2>/dev/null | wc -l)
        rm -f downloads/done/* 2>/dev/null
        echo "🗑 Cleared downloads/done/ ($done_count file(s))."
    fi

    if [ -d "$SCOOPZ_SHARED" ]; then
        local shared_count
        shared_count=$(find "$SCOOPZ_SHARED" -maxdepth 1 -type f 2>/dev/null | wc -l)
        rm -f "$SCOOPZ_SHARED"/* 2>/dev/null
        echo "🧼 Emptied shared gallery folder ($shared_count file(s))."
        if command -v termux-media-scan >/dev/null 2>&1; then
            termux-media-scan "$SCOOPZ_SHARED"
        fi
    else
        echo "ℹ️  Shared folder not found: $SCOOPZ_SHARED"
    fi

}

# ── Logs ──────────────────────────────────────────────────────────────────────
alias logs-upload="tail -f $SCOOPZ_DIR/logs/uploader.log"
alias logs-collect="tail -f $SCOOPZ_DIR/logs/collector.log"

# ── Status ────────────────────────────────────────────────────────────────────
scoopz-status() {
    echo ""
    echo "══════════════════════════════════════"
    echo "       Scoopz Workflow Status"
    echo "══════════════════════════════════════"

    echo "── ADB Devices ────────────────────────"
    adb devices -l

    echo ""
    echo "── Downloads ──────────────────────────"
    local total uploaded_count pending
    total=$(ls "$SCOOPZ_DIR/downloads/"*.mp4 2>/dev/null | wc -l)
    uploaded_count=0
    if [ -f "$SCOOPZ_DIR/data/uploaded.json" ]; then
        uploaded_count=$(python3 -c \
            "import json; print(len(json.load(open('$SCOOPZ_DIR/data/uploaded.json'))))" \
            2>/dev/null || echo "?")
    fi
    pending=$((total - uploaded_count))
    echo "   Total MP4s : $total"
    echo "   Uploaded   : $uploaded_count"
    echo "   Pending    : $pending"

    echo ""
    echo "── URL Queue ──────────────────────────"
    if [ -f "$SCOOPZ_DIR/data/queue.json" ]; then
        local qsize
        qsize=$(python3 -c \
            "import json; print(len(json.load(open('$SCOOPZ_DIR/data/queue.json'))))" \
            2>/dev/null || echo "?")
        echo "   URLs pending download: $qsize"
    else
        echo "   queue.json not found yet"
    fi

    echo ""
    echo "── Last 3 collector log lines ─────────"
    tail -3 "$SCOOPZ_DIR/logs/collector.log" 2>/dev/null || echo "   (no log yet)"

    echo ""
    echo "── Last 3 uploader log lines ──────────"
    tail -3 "$SCOOPZ_DIR/logs/uploader.log" 2>/dev/null || echo "   (no log yet)"

    echo "══════════════════════════════════════"
}

# ── Help ──────────────────────────────────────────────────────────────────────
scoopz-help() {
    echo ""
    echo "  collect              Clipboard monitor + TikTok downloader"
    echo "  upload               Post queued videos to Newsbreak"
    echo "  next-caption         Copy the next caption + archive files"
    echo "  install-next-caption-widget  Generate Termux widget script"
    echo "  scoopz-clean [keep]   Remove old local/shared downloads"
    echo "  scoopz-status        Devices · queue · pending · logs"
    echo "  logs-collect         Live tail collector log"
    echo "  logs-upload          Live tail uploader log"
    echo ""
    echo "  Session:"
    echo "  1. collect           ← keep running, browse TikTok"
    echo "  2. scoopz-status     ← check how many are ready"
    echo "  3. plug in USB       ← connect phone to laptop/charger with ADB"
    echo "  4. upload            ← posts up to 40 videos automatically"
    echo ""
}

echo "✅ Scoopz ready — 'scoopz-help' for commands"