#!/usr/bin/env python3
"""Print the presence flag set from the menu bar: 'at_desk' or 'away'.
Exit 0 when at desk, 1 when away, so shell checks read naturally:

    presence.py && say-it-out-loud || send-it-to-the-phone

The file (~/.voicemode/presence.json, {"at_desk": bool, "since": iso}) is
written by the menu-bar toggle in voicemode_indicator.py; inbox_hooks.py
injects a "[presence]" line into the session when it flips."""
import json, sys
from pathlib import Path
try:
    d = json.loads((Path.home() / ".voicemode" / "presence.json").read_text())
    at = bool(d.get("at_desk")); since = d.get("since", "?")
except Exception:
    at, since = False, "no file"
print(("at_desk" if at else "away") + f" since {since}")
sys.exit(0 if at else 1)
