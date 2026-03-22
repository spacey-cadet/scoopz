#!/data/data/com.termux/files/usr/bin/bash
# This script is called when you share a video from Gallery to Termux
# The shared file path is passed as $1

export PATH="/data/data/com.termux/files/usr/bin:$PATH"
source /data/data/com.termux/files/usr/etc/profile 2>/dev/null

cd /data/data/com.termux/files/home/scoopz

# Pass the shared file path to the Python script
python newsbreak_caption.py "$1"