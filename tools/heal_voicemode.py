#!/usr/bin/env python3
"""
Self-heal the local VoiceMode patches after an upgrade.

`uv tool upgrade voice-mode` replaces everything in site-packages, silently
reverting local fixes. Rather than relying on remembering, this runs at
login (and on demand) and re-applies anything missing.

Currently heals ONE patch:

  simple_failover.py - the OpenAI client is built with a bare
  `timeout=60.0`, which httpx applies to *connect* as well as read. A
  blackholed SYN to the LAN Whisper host therefore stalled a whole voice
  turn for 60s before the SDK retried (and then succeeded in ~1s). We keep
  the generous read budget but fail a dead connect in 5s.

Safe to run repeatedly: it checks before writing, and never touches a file
that already looks correct. Exits non-zero only on genuine failure, so
launchd's log is meaningful.

Upstream issue worth filing: a bare float timeout is wrong for any
LAN-hosted STT endpoint.
"""

import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

SITE = Path.home() / ".local/share/uv/tools/voice-mode/lib/python3.10/site-packages/voice_mode"
TARGET = SITE / "simple_failover.py"

ORIGINAL = "timeout=60.0,  # Allow time for slower transcriptions"
PATCHED = "timeout=httpx.Timeout(60.0, connect=5.0),"
MARKER = "httpx.Timeout(60.0, connect=5.0)"


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] heal: {msg}", flush=True)



# ---------------------------------------------------------------------------
# Additional patches added 2026-08-22. Each is a (file, marker, description)
# triple; we only VERIFY these and shout if they are missing, rather than
# re-applying them blind. Re-applying a multi-line edit to code that upstream
# has since changed is how you silently corrupt a working install - and one of
# these (the preroll) already caused a NameError that disabled silence
# detection entirely and made every recording run to its maximum duration.
EXTRA_PATCHES = [
    ("tools/converse.py", "_consume_bargein_preroll",
     "barge-in preroll (keeps the start of an interrupting sentence)"),
    ("tools/converse.py", "NO_SPEECH_TIMEOUT",
     "no-speech timeout (upstream waits out max_duration instead)"),
    ("tools/converse.py", "_VM_NOISE_FLOOR",
     "energy gate on silence detection (webrtcvad alone false-fires on room noise)"),
    ("tools/converse.py", "_VM_SPEAKER_GATE",
     "speaker-gated end-of-turn (asks the daemon's voiceprint before ending a turn)"),
    ("tools/converse.py", "_VM_SPEAKER_REJECT",
     "speaker filter on the listen window (drops takes that are not the enrolled voice)"),
    ("simple_failover.py", "_strip_prompt_echo",
     "prompt-echo guard (Whisper repeating VOICEMODE_STT_PROMPT as user speech)"),
    ("simple_failover.py", "_collapse_repeats",
     "repeated prompt-echo collapse (Whisper looping vocabulary terms)"),
    ("simple_failover.py", "_is_probable_hallucination",
     "silence-hallucination guard (\"Thank you for watching\")"),
    ("simple_failover.py", "_VM_URL_HALLUCINATION",
     "URL-shaped silence hallucination guard (\"www.adblue.com\")"),
    ("simple_failover.py", "_VM_URDU_NOT_HINDI",
     "Urdu re-transcription when Whisper returns Devanagari/Gurmukhi"),
]


def verify_extra() -> int:
    """Report any patch that an upgrade has removed. Returns count missing."""
    missing = 0
    for rel, marker, desc in EXTRA_PATCHES:
        f = SITE / rel
        if not f.exists():
            log(f"WARNING: {rel} not found; cannot verify {desc}")
            missing += 1
            continue
        try:
            if marker not in f.read_text():
                log(f"MISSING PATCH in {rel}: {desc}. An upgrade has reverted "
                    f"it. Re-apply by hand - see the notes in that file's .bak.")
                missing += 1
        except OSError as e:
            log(f"WARNING: could not read {rel}: {e}")
            missing += 1
    if not missing:
        log(f"all {len(EXTRA_PATCHES)} additional patches present")
    return missing


def heal() -> int:
    if not TARGET.exists():
        # Not an error: voice-mode may live elsewhere, or python may have
        # been bumped to a new minor version (path contains python3.10).
        log(f"target not found, nothing to do: {TARGET}")
        return 0

    try:
        src = TARGET.read_text()
    except OSError as e:
        log(f"ERROR reading {TARGET}: {e}")
        return 1

    if MARKER in src:
        log("patch already present, no action")
        return 1 if verify_extra() else 0

    if ORIGINAL not in src:
        # Upstream changed this line. Do NOT guess - a wrong edit here
        # breaks all speech-to-text. Report loudly and leave it alone.
        log("WARNING: upstream code changed - the original timeout line is "
            "gone and the patch is absent. Not editing blindly. "
            "Re-check simple_failover.py by hand.")
        return 2

    backup = TARGET.with_suffix(".py.bak")
    try:
        if not backup.exists():
            shutil.copy2(TARGET, backup)

        patched = src.replace(ORIGINAL, PATCHED, 1)

        # The patch needs httpx imported; add it next to the stdlib imports
        # if the upgrade dropped our import line.
        if not re.search(r"^import httpx$", patched, re.MULTILINE):
            patched = patched.replace(
                "import logging\n", "import logging\nimport httpx\n", 1)

        TARGET.write_text(patched)
    except OSError as e:
        log(f"ERROR writing patch: {e}")
        return 1

    # Verify rather than assume the write did what we wanted.
    check = TARGET.read_text()
    if MARKER in check and re.search(r"^import httpx$", check, re.MULTILINE):
        log("patch re-applied after upgrade")
        verify_extra()
        return 0

    log("ERROR: patch verification failed after write")
    return 1


def strip_kokoro_limit() -> None:
    """Kokoro's launchd plist used to carry UVICORN_LIMIT_MAX_REQUESTS=25, so
    uvicorn exited cleanly after 25 requests and the menu bar's health probes
    restarted it every ~10 min (blue icon, no sound, OpenAI failover). The
    key comes from VoiceMode's plist template via the service installer, so
    an upgrade or reinstall brings it back. Strip it from both, every run."""
    import plistlib
    import subprocess
    import os
    for path, live in ((Path.home() / "Library/LaunchAgents/com.voicemode.kokoro.plist", True),
                       (SITE / "templates/launchd/com.voicemode.kokoro.plist", False)):
        if not path.exists():
            continue
        try:
            raw = path.read_text()
            if "UVICORN_LIMIT_MAX_REQUESTS" not in raw:
                continue
            if live:
                d = plistlib.loads(path.read_bytes())
                d.get("EnvironmentVariables", {}).pop("UVICORN_LIMIT_MAX_REQUESTS", None)
                path.write_bytes(plistlib.dumps(d))
                uid = os.getuid()
                subprocess.run(["launchctl", "bootout", f"gui/{uid}/com.voicemode.kokoro"], capture_output=True)
                subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True)
                log("kokoro: request limit stripped from live plist and job reloaded")
            else:
                import re as _re
                raw = _re.sub(r"\s*<key>UVICORN_LIMIT_MAX_REQUESTS</key>\s*<string>[^<]*</string>", "", raw)
                path.write_text(raw)
                log("kokoro: request limit stripped from the plist template")
        except Exception as e:
            log(f"kokoro: could not strip request limit from {path.name}: {e}")


if __name__ == "__main__":
    rc = heal()
    strip_kokoro_limit()
    sys.exit(rc)
