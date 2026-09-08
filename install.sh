#!/bin/sh
set -e
D="$HOME/.voicemode/indicator"; mkdir -p "$D" "$HOME/.voicemode/context" "$HOME/Library/LaunchAgents"
cp daemon/*.py inbox/*.py tools/*.py tools/enrol/*.py tools/enrol/enrolment_text.txt "$D/"
python3 -m venv "$D/.venv" && "$D/.venv/bin/pip" install -q speechbrain torch torchaudio resemblyzer webrtcvad sounddevice numpy scipy pyobjc-framework-ApplicationServices pyobjc-framework-Quartz pyobjc-framework-Cocoa msal pypdf rumps
for p in launchd/*.plist; do sed "s#__HOME__#$HOME#g" "$p" > "$HOME/Library/LaunchAgents/$(basename "$p")"; done
echo "Installed. Next: fill config.example.env, enrol your voice, apply patches/, then launchctl bootstrap the plists."
