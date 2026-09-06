#!/bin/bash
# Relaunch the Claude desktop app with Chromium's occlusion throttling off, so a
# covered window still takes an accessibility write (background delivery).
LOG="$HOME/.voicemode/indicator/relaunch.log"
FLAGS="--disable-backgrounding-occluded-windows --disable-renderer-backgrounding"
echo "[$(date '+%F %T')] relaunch requested" >> "$LOG"
sleep 30
osascript -e 'tell application "Claude" to quit' >> "$LOG" 2>&1
for i in $(seq 1 40); do pgrep -x Claude >/dev/null || break; sleep 1; done
if pgrep -x Claude >/dev/null; then echo "[$(date '+%F %T')] still running after 40 s, killing" >> "$LOG"; pkill -x Claude; sleep 3; fi
echo "[$(date '+%F %T')] launching with: $FLAGS" >> "$LOG"
open -a Claude --args $FLAGS >> "$LOG" 2>&1
sleep 8
ps -axo pid,command | grep -E "/Claude$|/Claude " | grep -v grep | head -2 >> "$LOG"
echo "[$(date '+%F %T')] done" >> "$LOG"
