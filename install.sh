#!/bin/sh
set -e
D="$HOME/.voicemode/indicator"; mkdir -p "$D" "$D/audiotap" "$HOME/.voicemode/context" "$HOME/.voicemode/calls" "$HOME/.voicemode/templates" "$HOME/Library/LaunchAgents"
cp daemon/*.py inbox/*.py tools/*.py tools/*.sh tools/enrol/*.py tools/enrol/enrolment_text.txt callwatch/call_watch.py "$D/"
cp callwatch/audiotap/main.swift callwatch/audiotap/build.sh callwatch/audiotap/embedded-Info.plist "$D/audiotap/"
chmod +x "$D"/*.sh "$D/audiotap/build.sh"
python3 -m venv "$D/.venv" && "$D/.venv/bin/pip" install -q speechbrain torch torchaudio resemblyzer webrtcvad sounddevice numpy scipy pyobjc-framework-ApplicationServices pyobjc-framework-Quartz pyobjc-framework-Cocoa msal pypdf rumps requests
for p in launchd/*.plist; do sed "s#__HOME__#$HOME#g" "$p" > "$HOME/Library/LaunchAgents/$(basename "$p")"; done
# The call watch needs the Swift helper (macOS 14.2+, Xcode command line tools) and ffmpeg.
if command -v swiftc >/dev/null 2>&1; then
  (cd "$D/audiotap" && sh build.sh) || echo "audiotap build failed; call_watch.py falls back to ctypes detection without recording"
else
  echo "swiftc not found: skipping audiotap (call_watch.py will detect calls but not record them)"
fi
command -v ffmpeg >/dev/null 2>&1 || echo "ffmpeg not found (brew install ffmpeg): call transcription and voice notes need it"
echo "Installed. Next: fill config.example.env, enrol your voice, apply patches/, then launchctl bootstrap the plists."
