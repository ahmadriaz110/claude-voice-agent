#!/usr/bin/env python3
"""
Voice-triggered barge-in for VoiceMode.

VoiceMode already implements the hard half: control_channel.py + control_socket.py,
and streaming.py checks control state before every 4096-byte PCM chunk, aborting
playback through PortAudio in ~85ms. Upstream's CLI even describes skip-forward as
"the same effect as speaking over the assistant, but triggered by a key/button."
Nobody built the speaking-over-it half. This is that half.

HOW IT ARMS
  Tails VoiceMode's JSONL event log for TTS_PLAYBACK_START / TTS_PLAYBACK_END.
  Deliberately NOT ~/.voicemode/state.json: that file is only written by
  _ask_turns_pipeline, never by _converse_core, which is the path actually used, a daemon armed off state.json would never fire.

HOW IT DECIDES
  While armed, every 30ms frame must satisfy BOTH webrtcvad(3) AND an energy
  threshold calibrated from this room, for N consecutive frames. VAD alone is not
  enough: it keys on speech-like spectra and will trip on a cough, a door, or the
  tail of our own speaker.

WHY THE ENERGY GATE MATTERS MORE THAN THE VAD
  Measured on this machine: ambient RMS p90 = 55.5, max = 74. Speech is an order
  of magnitude above that. The floor sits at 8x p90, comfortably above the room
  and comfortably below a spoken word.

THE FAILURE MODE THIS GUARDS AGAINST
  skip_forward does not merely stop audio, converse.py falls straight through
  into the record/listen turn. So a FALSE trigger makes VoiceMode record the room,
  transcribe it, and hand the result to Claude AS THE USER'S WORDS. Silent wrong
  input is worse than no barge-in. Hence: conservative floor, a sustain window, a
  post-arm suppression window (our own first syllable is the classic self-trigger),
  and a cooldown so one noise cannot fire repeatedly.

MICROPHONE OWNERSHIP
  The mic is opened ONLY while armed and closed immediately on fire or on
  playback end, so VoiceMode owns the device for its own recording turn. Holding
  it open across the handoff risks CoreAudio contention on the same device.
"""

import json
import os
import wave
from collections import deque
import re
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import numpy as np
import sounddevice as sd
import webrtcvad
from scipy import signal

HOME = Path.home()
EVENTS_DIR = HOME / ".voicemode" / "logs" / "events"
CONTROL_SOCK = HOME / ".voicemode" / "control.sock"
STATUS_PATH = HOME / ".voicemode" / "indicator" / "bargein.status"
# Written by the menu bar "Pause voice"; the agent refuses to loop while it
# exists. A wake is an explicit request to talk, so launch_session clears it.
# It was referenced without ever being defined - the NameError was eaten by a
# bare except, so the pause silently survived every wake for two days.
PAUSE_FLAG = HOME / ".voicemode" / "indicator" / "paused.flag"
# Touched whenever this daemon verifies HIS voice (barge-in fire, wake, capture).
# inbox_hooks reads its mtime as "he responded" before escalating to WhatsApp.
ACK_FILE = HOME / ".voicemode" / "context" / "ack"


def _ack():
    try:
        ACK_FILE.parent.mkdir(parents=True, exist_ok=True); ACK_FILE.touch()
    except OSError:
        pass
# Written when we interrupt: the audio we already heard while deciding to
# interrupt. VoiceMode prepends it so the opening words of the sentence that
# CAUSED the barge-in are not lost in the mic handover.
PREROLL_PATH = HOME / ".voicemode" / "bargein_preroll.wav"
LOG_PATH = HOME / ".voicemode" / "indicator" / "bargein.log"
VOICEMODE_BIN = str(HOME / ".local" / "bin" / "voicemode")

RATE = 24000                 # VoiceMode captures at 24k; match it
FRAME_MS = 30                # webrtcvad accepts 10/20/30ms only
FRAME = int(RATE * FRAME_MS / 1000)
VAD_RATE = 16000

# Speech is BURSTY at 30ms granularity: frames dip below threshold between
# syllables. Requiring N consecutive qualifying frames is therefore brittle -
# measured on a real capture, only 13 of 795 frames cleared the floor and the
# longest consecutive run was exactly 6, i.e. right on the trigger boundary.
# Use N-of-M instead: tolerant of inter-syllable gaps, still needs sustained
# activity rather than a single spike.
SUSTAIN_HITS = int(os.environ.get("BARGEIN_SUSTAIN_HITS", 5))    # qualifying frames
SUSTAIN_WINDOW = int(os.environ.get("BARGEIN_SUSTAIN_WINDOW", 18))  # out of the last ~540ms     # ~150ms
ARM_SUPPRESS_S = float(os.environ.get("BARGEIN_ARM_SUPPRESS", 0.35))
COOLDOWN_S = float(os.environ.get("BARGEIN_COOLDOWN", 2.0))
FLOOR_MULT = float(os.environ.get("BARGEIN_FLOOR_MULT", 8.0))
CALIB_S = 1.0
MIN_FLOOR = 200.0            # never trust a floor below this
# ...and never trust one above this either. The startup calibration samples a
# single second: catch a noisy moment and the floor lands far too high (seen
# ranging 364 to 1778 across restarts, from ambient p90 of 45 to 222). Real
# speech on this mic measures 2000-7000, so capping at 900 keeps genuine
# interruptions comfortably detectable while still clearing room noise.
# Lowered from 900: attempts measuring 429, 822 and 1188 rms were real speech
# that never cleared the gate. We can afford a lower bar now that speaker
# verification exists - previously the energy floor was the ONLY thing standing
# between our own playback and a false interrupt; now it is the coarse filter
# and the voiceprint is the precise one.
MAX_FLOOR = float(os.environ.get("BARGEIN_MAX_FLOOR", 450.0))
VAD_LEVEL = int(os.environ.get("BARGEIN_VAD_LEVEL", 1))

# Speaker verification. Energy + VAD cannot tell OUR voice from the user's -
# that is why every threshold I tried either missed real interruptions or
# fired on our own playback. A voiceprint can: measured on this setup,
# the user scores +0.88 on a 1.5s window and Kokoro TTS scores +0.53.
VOICEPRINT_PATH = HOME / ".voicemode" / "indicator" / "voiceprint.npy"
# Measured: on CLEAN audio the user scores ~0.99, but MIXED with our own TTS
# playing, the same voice scores 0.58-0.69 - the mixture drags the embedding
# toward the interfering speaker. A 0.65 threshold sat inside that band, so
# real interruptions were rejected three times before one got through.
# Kokoro alone scores 0.46-0.56, so 0.57 clears it while accepting the user.
# This is safe to lower because the energy gate is a second, independent
# filter - both must pass.
SPEAKER_THRESHOLD = float(os.environ.get("BARGEIN_SPEAKER_THRESHOLD", 0.60))
# Longer window = higher similarity, because the embedding sees more of the
# speaker and less of everything else. Measured on clean audio: 1.0s -> 0.74,
# 1.5s -> 0.88. Mixed with TTS the scores cluster at 0.56-0.61 against a 0.57
# threshold, so roughly half of real interruptions were rejected. Widening the
# window raises the signal instead of relaxing the test - which matters because
# Kokoro sits at 0.46-0.56 and there is no room to lower the bar.
# Costs nothing: the ring buffer already holds 2s.
VERIFY_SECONDS = float(os.environ.get("BARGEIN_VERIFY_SECONDS", 1.7))

# ---- Wake word -------------------------------------------------------------
# When NOT speaking, listen for "hey claude" so a session can be started by
# voice instead of the menu bar. Runs in the SAME process as barge-in so only
# one thing ever owns the microphone - two processes contending for the same
# CoreAudio device was already implicated in recordings that never ended.
WAKE_ENABLED = os.environ.get("BARGEIN_WAKE", "1") not in ("0", "false", "False")
# Includes real mis-transcriptions observed on this mic, not invented ones.
WAKE_PHRASES = ("hey claude", "hey clause", "hey cloud", "hi claude",
                "hey, claude", "ok claude", "hey claud", "hey lord",
                "egg lard", "hey clyde", "a claude", "hey glaude", "dick lord",
                "acloth", "a cloth", "a cloud", "a clock", "hey cloth")
WAKE_STT_URL = os.environ.get(
    "BARGEIN_WAKE_STT", "http://127.0.0.1:2022/v1/audio/transcriptions")
WAKE_MIN_SPEECH_S = 0.45     # ignore blips
# The barge-in floor is set high to beat our own playback. When idle there is
# no playback to beat, so reuse the measured ambient instead - otherwise a
# normal-volume "hey Claude" never clears the bar and nothing ever fires.
WAKE_FLOOR_MULT = float(os.environ.get("BARGEIN_WAKE_FLOOR_MULT", 3.0))
# Cooldown was 8s, which silently dropped a second "hey Claude" issued a few
# seconds after the first - exactly the queue-another-task case. The 3s
# sleep after launch already prevents an immediate re-trigger.
WAKE_COOLDOWN_S = float(os.environ.get("BARGEIN_WAKE_COOLDOWN", 3.0))
# Do not end a burst on the first quiet frame. "Hey Claude" has a natural gap
# between the two words, which split it into 0.15s + 0.39s fragments - both
# under the minimum, so both were discarded and nothing ever fired.
# Raised 0.5 -> 1.0 once bursts started carrying a command after the phrase:
# "hey Claude [pause] open the site" must stay ONE burst, or the command
# half arrives without a wake phrase and is discarded.
WAKE_HANGOVER_S = float(os.environ.get("BARGEIN_WAKE_HANGOVER", 1.6))
# Short clips score lower than long ones (measured: 1.0s -> 0.74, 1.5s -> 0.88
# for the same speaker), and a wake phrase is ~1s. Using the barge-in
# threshold here rejects the real speaker. Kokoro still scores 0.48-0.53, and
# the phrase match is a second gate, so 0.55 keeps other voices out.
WAKE_SPEAKER_THRESHOLD = float(os.environ.get("BARGEIN_WAKE_SPK", 0.55))
WAKE_PROMPT = "Start voice mode and talk to me."

# ---- Voice command injection -----------------------------------------------
# "Hey Claude, <instruction>" while VoiceMode is NOT recording - i.e. Claude
# is busy running tools, or the session is sitting idle - pastes
# <instruction> into the running session instead of the fixed WAKE_PROMPT.
# This is how a second or third task is queued while the first still runs.
# Prefixed so the agent can tell mic-originated text from typed text and
# confirm anything destructive before acting on a possible mis-hearing.
INJECT_PREFIX = os.environ.get("BARGEIN_INJECT_PREFIX", "[voice] ")
# The phrase is always FIRST in a burst, so detection looks only at the
# opening slice; the whole burst, up to the cap, is the command. Truncate
# rather than slide so the phrase is never scrolled out of the buffer.
WAKE_DETECT_S = 3.0
CMD_MAX_SPEECH_S = float(os.environ.get("BARGEIN_CMD_MAX_S", 30.0))
# "Hey Claude" [pause] "what time is it" arrives as TWO bursts when the pause
# beats the hangover (00:08:28 / 00:08:32). For this long after a wake-only
# match, a phrase-less follow-up from the enrolled speaker IS the command.
CMD_WINDOW_S = float(os.environ.get("BARGEIN_CMD_WINDOW", 4.0))
# Manual/debug hook: write text here and the daemon pastes it as if spoken.
INJECT_FILE = HOME / ".voicemode" / "indicator" / "inject.txt"
# An injected message is only seen by the agent at its next tool boundary. If
# the next thing it does is speak for a minute, the message waits a minute -
# he called this out. So for a short window after any injection, cut the next
# TTS short with skip-forward: the converse returns, the agent sees the
# message, and answers it instead of finishing a now-stale sentence.
CUT_TTS_WINDOW_S = float(os.environ.get("BARGEIN_CUT_TTS_WINDOW", 30.0))
_cut_tts_until = [0.0]
# True while our TTS is playing (event log). An injection that lands DURING
# speech or DURING a listen window must interrupt that step right away, not
# just the next one - he waited a full sentence plus a listen for a message
# he had already answered.
_tts_playing = [False]

# ---- Verification service + recording interlock ----------------------------
# VoiceMode's own interpreter has no resemblyzer, and installing it there once
# pulled in a conflicting webrtcvad and broke the stack. This daemon already
# holds the model warm (~10ms/check), so it serves verification over loopback
# instead.
VERIFY_PORT = int(os.environ.get("BARGEIN_VERIFY_PORT", 8899))
# Written by VoiceMode the instant it opens the mic. The event-log tail lags by
# a second or more; in that gap the daemon grabbed the mic for wake listening
# while VoiceMode was recording, and silence detection then ran to max_duration.
# A flag file is immediate and unambiguous.
VM_RECORDING_FLAG = HOME / ".voicemode" / "indicator" / "vm_recording.flag"
# Keep ~2s of rolling audio so the whole interrupting phrase survives, not
# just the part after VoiceMode reopens the mic.
PREROLL_SECONDS = float(os.environ.get("BARGEIN_PREROLL_S", 2.0))
# Upper bound on PortAudio stop/close before we treat it as the CoreAudio
# deadlock above and restart. Normal stop is ~85ms.
STREAM_STOP_TIMEOUT_S = float(os.environ.get("BARGEIN_STOP_TIMEOUT", 3.0))
# Rolling background estimate of our own playback as heard by this mic.
BG_WINDOW = int(os.environ.get("BARGEIN_BG_WINDOW", 50))        # ~1.5s of frames
BG_MIN_SAMPLES = int(os.environ.get("BARGEIN_BG_MIN", 20))      # ~600ms before arming
BG_MULT = float(os.environ.get("BARGEIN_BG_MULT", 3.0))
# Minimum multiple of the ambient floor before anything counts as speech.
MIN_MARGIN = float(os.environ.get("BARGEIN_MIN_MARGIN", 2.5))


def log(msg):
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def write_status(state, extra=""):
    """Heartbeat. A dead daemon is otherwise invisible, you just discover
    one day that barge-in silently stopped working."""
    try:
        STATUS_PATH.write_text(json.dumps({
            "state": state, "detail": extra, "ts": time.time(),
        }))
    except OSError:
        pass


def find_mic():
    """Prefer the headset named in BARGEIN_MIC_NAME; index order is not stable."""
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and os.environ.get("BARGEIN_MIC_NAME", "COUGAR") in d["name"]:
            return i, d["name"]
    d = sd.query_devices(kind="input")
    return None, d["name"]


def today_log():
    return EVENTS_DIR / f"voicemode_events_{date.today().isoformat()}.jsonl"


class EventTail:
    """Follows the current day's event log, tolerating rotation and the file
    not existing yet (VoiceMode creates it on first use)."""

    def __init__(self):
        self.fh = None
        self.path = None
        self._open(seek_end=True)

    def _open(self, seek_end=False):
        p = today_log()
        if p == self.path and self.fh:
            return
        if self.fh:
            try:
                self.fh.close()
            except Exception:
                pass
            self.fh = None
        if p.exists():
            try:
                self.fh = p.open("r", errors="replace")
                self.path = p
                if seek_end:
                    self.fh.seek(0, os.SEEK_END)
            except OSError:
                self.fh = None

    def events(self):
        if self.path != today_log() or self.fh is None:
            self._open(seek_end=True)
        if self.fh is None:
            return
        for line in self.fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def calibrate(device):
    """Measure this room now rather than trusting a constant. Rooms change:
    a fan, a window, a different time of day."""
    vals = []
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                        blocksize=FRAME, device=device) as s:
        for _ in range(int(CALIB_S * 1000 / FRAME_MS)):
            buf, _ = s.read(FRAME)
            a = buf[:, 0].astype(np.float32)
            vals.append(float(np.sqrt(np.mean(a * a))))
    p90 = float(np.percentile(vals, 90))
    floor = min(max(p90 * FLOOR_MULT, MIN_FLOOR), MAX_FLOOR)
    log(f"calibrated: ambient p90={p90:.1f} -> energy floor={floor:.0f} "
        f"(clamped to {MIN_FLOOR:.0f}-{MAX_FLOOR:.0f})")
    return floor, p90




_encoder = None
_voiceprint = None


def load_speaker_model():
    """Load the voiceprint and encoder. Absent voiceprint = fall back to the
    old energy/VAD behaviour rather than refusing to run."""
    global _encoder, _voiceprint
    if not VOICEPRINT_PATH.exists():
        log("no voiceprint enrolled - falling back to energy+VAD only")
        return False
    try:
        from resemblyzer import VoiceEncoder
        _voiceprint = np.load(str(VOICEPRINT_PATH))
        _encoder = VoiceEncoder("cpu")
        # Warm it: the first embed takes ~2.7s, later ones ~10ms. Paying that
        # during a live interruption would blow the latency budget.
        _encoder.embed_utterance((np.random.randn(16000) * 0.05).astype(np.float32))
        log(f"voiceprint loaded (dim={_voiceprint.shape[0]}), "
            f"threshold={SPEAKER_THRESHOLD}")
        return True
    except Exception as e:
        log(f"speaker model unavailable ({e}) - energy+VAD only")
        _encoder = None
        return False


def _loudest_span(frames_bytes, keep=0.75):
    """Return the loudest contiguous portion of a clip.

    A speaker embedding is computed over everything it is given, so leading and
    trailing near-silence pulls the result toward nothing-in-particular and
    lowers the similarity. Trimming to where the energy actually is makes the
    comparison a fair one.
    """
    if len(frames_bytes) < 4:
        return frames_bytes
    rms = []
    for f in frames_bytes:
        a = np.frombuffer(f, dtype=np.int16).astype(np.float32)
        rms.append(float(np.sqrt(np.mean(a * a))) if a.size else 0.0)
    # Never trim below what the verifier needs (RATE//2 samples). A 0.51s
    # burst trimmed to 75% fell under that and scored 0.00 - rejected before
    # any comparison happened.
    min_frames = int((RATE // 2) / FRAME) + 1
    n = max(min_frames, int(len(frames_bytes) * keep))
    n = min(n, len(frames_bytes))
    best_i, best_sum = 0, -1.0
    for i in range(0, len(frames_bytes) - n + 1):
        tot = sum(rms[i:i + n])
        if tot > best_sum:
            best_sum, best_i = tot, i
    return frames_bytes[best_i:best_i + n]


def is_user_speaking(frames_bytes):
    """Compare recent audio against the enrolled voiceprint.

    Returns (matched, score). Without a model, returns (True, -1) so the
    caller falls back to the previous behaviour instead of never firing.
    """
    if _encoder is None or _voiceprint is None:
        return True, -1.0
    try:
        frames_bytes = _loudest_span(frames_bytes)
        pcm = np.frombuffer(b"".join(frames_bytes), dtype=np.int16)
        if len(pcm) < RATE // 2:
            return False, 0.0
        # resemblyzer expects float32 @16k
        x = pcm.astype(np.float32) / 32768.0
        x16 = signal.resample(x, int(len(x) * 16000 / RATE)).astype(np.float32)
        emb = _encoder.embed_utterance(x16)
        return float(np.dot(_voiceprint, emb)) >= SPEAKER_THRESHOLD, float(np.dot(_voiceprint, emb))
    except Exception as e:
        log(f"speaker check failed ({e}) - allowing")
        return True, -1.0


def transcribe_clip(frames_bytes, language="en", prompt="Hey Claude."):
    """Send a clip to Whisper. Defaults suit wake-word matching on a short
    English clip; pass language=None / prompt=None for a full command so a
    mixed-language instruction is auto-detected and nothing biases it."""
    import io, wave as _w, urllib.request, json as _j
    try:
        buf = io.BytesIO()
        with _w.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
            w.writeframes(b"".join(frames_bytes))
        data = buf.getvalue()
        boundary = "----vmwake"
        head = f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n"
        # Force English for wake detection. Auto-detection is unreliable on a
        # ~1s clip and was transcribing "hey Claude" as Hindi ('एक लॉट'), so the
        # phrase never matched. Commands pass language=None for auto-detect.
        if language:
            head += f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{language}\r\n"
        # Bias toward the wake phrase. On a ~1s clip Whisper rendered "Hey
        # Claude" as "egg lard". NOT used for commands: a vocabulary prompt is
        # what Whisper echoes back as phantom speech on quiet audio, and here
        # there is no _strip_prompt_echo guard between us and the paste.
        if prompt:
            head += f"--{boundary}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\n{prompt}\r\n"
        body = (head +
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"w.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(WAKE_STT_URL, data=body,
              headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        raw = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace")
        try:
            return (_j.loads(raw).get("text") or "").strip()
        except Exception:
            return raw.strip()
    except Exception as e:
        log(f"wake transcribe failed: {e}")
        return ""


def _strip_wake(text):
    """Remove the wake phrase and the pause-punctuation after it, leaving
    what the user actually asked for. Empty string when it was wake-only."""
    t = (text or "").strip()
    low = t.lower()
    for ph in sorted(WAKE_PHRASES, key=len, reverse=True):
        i = low.find(ph)
        if i >= 0:
            t = (t[:i] + t[i + len(ph):]).strip()
            break
    return re.sub(r"^[\s,.:;!?\-\u2013\u2014]+", "", t).strip()


AX_DUMP_PATH = HOME / ".voicemode" / "indicator" / "ax_dump.txt"


def _ax_dump():
    """Debug: write the focused element and every text-ish AX element of
    Claude's front window to ax_dump.txt. Only this daemon has assistive
    access, so this is the only way to see where a paste actually lands."""
    script = '''
tell application "System Events" to tell process "Claude"
  set out to ""
  try
    set value of attribute "AXManualAccessibility" to true
    delay 2
    set out to out & "AXManualAccessibility: " & ((value of attribute "AXManualAccessibility") as text) & linefeed
  on error e
    set out to out & "AXManualAccessibility: err " & e & linefeed
  end try
  try
    set fe to value of attribute "AXFocusedUIElement"
    set fd to ""
    try
      set fd to description of fe
    end try
    set out to out & "FOCUSED: " & (role of fe) & " | " & fd & linefeed
  on error e
    set out to out & "FOCUSED: err " & e & linefeed
  end try
  set out to out & "WINDOWS: " & (count of windows) & linefeed
  repeat with w in windows
    try
      set out to out & "WIN: " & (name of w) & " | pos " & ((position of w) as text) & linefeed
    end try
  end repeat
  set els to entire contents of window 1
  set out to out & "ELEMENTS: " & (count of els) & linefeed
  set showAll to ((count of els) < 80)
  repeat with el in els
    try
      set r to role of el
      set f to "-"
      try
        set f to (value of attribute "AXFocused" of el) as text
      end try
      if showAll or (r is in {"AXTextArea", "AXTextField", "AXComboBox", "AXWebArea"}) or f is "true" then
        set d to ""
        try
          set d to description of el
        end try
        set t to ""
        try
          set t to title of el
        end try
        set v to ""
        try
          set v to (value of el) as text
        end try
        if length of v > 60 then set v to text 1 thru 60 of v
        set pos to ""
        try
          set pos to ((position of el) as text) & " " & ((size of el) as text)
        end try
        set out to out & r & " | focused=" & f & " | desc=" & d & " | title=" & t & " | pos=" & pos & " | val=" & v & linefeed
      end if
    end try
  end repeat
  return out
end tell
'''
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=90)
        AX_DUMP_PATH.write_text((r.stdout or "") + ("\nSTDERR: " + r.stderr if r.stderr else ""))
        log(f"axdump: wrote {AX_DUMP_PATH.name} ({len(r.stdout or '')} chars)")
    except Exception as e:
        AX_DUMP_PATH.write_text(f"FAILED: {e}")
        log(f"axdump: failed ({e})")


# ---- Native Accessibility paste (no System Events) -------------------------
# The System Events route pastes into WHATEVER has keyboard focus, and after a
# Claude restart nothing did: AXFocusedUIElement was `missing value`, so four
# probes were typed into the void while the script still returned "OK". It
# also depends on a System Events instance that can wedge (-600). This path
# talks to the AX API directly from this process, which already holds the
# Accessibility grant: find the chat composer, focus it, verify, then paste.
CLAUDE_BUNDLE = "com.anthropic.claudefordesktop"
COMPOSER_HINTS = ("reply", "message", "ask", "claude", "prompt", "how can", "type")
AX_MAX_NODES = int(os.environ.get("BARGEIN_AX_MAX_NODES", 6000))
_composer_cache = {"el": None}


def _ax_attr(el, name):
    import ApplicationServices as AS
    try:
        err, val = AS.AXUIElementCopyAttributeValue(el, name, None)
        return val if err == 0 else None
    except Exception:
        return None


def _ax_point(el):
    """(x, y) of an element, or None."""
    import ApplicationServices as AS
    v = _ax_attr(el, AS.kAXPositionAttribute)
    if v is None:
        return None
    try:
        ok, pt = AS.AXValueGetValue(v, AS.kAXValueCGPointType, None)
        return (float(pt.x), float(pt.y)) if ok else None
    except Exception:
        return None


def _ax_size(el):
    import ApplicationServices as AS
    v = _ax_attr(el, AS.kAXSizeAttribute)
    if v is None:
        return None
    try:
        ok, sz = AS.AXValueGetValue(v, AS.kAXValueCGSizeType, None)
        return (float(sz.width), float(sz.height)) if ok else None
    except Exception:
        return None


def _ax_find_composer(pid):
    """Breadth-first search of Claude's focused window for text inputs.
    Returns (candidates, nodes_visited, seconds)."""
    import ApplicationServices as AS
    from collections import deque
    t0 = time.time()
    app = AS.AXUIElementCreateApplication(pid)
    # Electron builds its AX tree lazily; this attribute is the documented way
    # to ask for it without VoiceOver. Idempotent.
    try:
        AS.AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)
    except Exception:
        pass
    win = _ax_attr(app, AS.kAXFocusedWindowAttribute)
    if win is None:
        wins = _ax_attr(app, AS.kAXWindowsAttribute) or []
        win = wins[0] if wins else None
    if win is None:
        return [], 0, time.time() - t0
    q = deque([(win, 0)])
    seen = 0
    cands = []
    while q and seen < AX_MAX_NODES:
        el, depth = q.popleft()
        seen += 1
        role = _ax_attr(el, AS.kAXRoleAttribute)
        if role in ("AXTextArea", "AXTextField", "AXComboBox"):
            pt = _ax_point(el) or (0.0, 0.0)
            sz = _ax_size(el) or (0.0, 0.0)
            cands.append({
                "el": el, "role": role, "depth": depth,
                "x": pt[0], "y": pt[1], "w": sz[0], "h": sz[1],
                "focused": bool(_ax_attr(el, AS.kAXFocusedAttribute)),
                "desc": str(_ax_attr(el, AS.kAXDescriptionAttribute) or ""),
                "title": str(_ax_attr(el, AS.kAXTitleAttribute) or ""),
                "ph": str(_ax_attr(el, "AXPlaceholderValue") or ""),
                "dom_id": str(_ax_attr(el, "AXDOMIdentifier") or ""),
                "dom_cls": " ".join(list(_ax_attr(el, "AXDOMClassList") or []))[:80],
                "val": str(_ax_attr(el, AS.kAXValueAttribute) or "")[:60],
            })
        for k in (_ax_attr(el, AS.kAXChildrenAttribute) or []):
            q.append((k, depth + 1))
    return cands, seen, time.time() - t0


def _pick_composer(cands):
    areas = [c for c in cands if c["role"] == "AXTextArea"]
    if not areas:
        return None
    def hinted(c):
        blob = " ".join((c["ph"], c["desc"], c["title"], c["dom_id"], c["dom_cls"])).lower()
        return any(h in blob for h in COMPOSER_HINTS)
    hits = [c for c in areas if hinted(c)]
    pool = hits or areas
    # The composer sits at the bottom of the chat column: lowest on screen wins.
    return max(pool, key=lambda c: c["y"])


def _fmt_cand(c):
    return (f'{c["role"]} d={c["depth"]} focused={c["focused"]} '
            f'xy=({c["x"]:.0f},{c["y"]:.0f}) wh=({c["w"]:.0f}x{c["h"]:.0f}) '
            f'id={c["dom_id"]!r} cls={c["dom_cls"]!r} ph={c["ph"]!r} '
            f'desc={c["desc"]!r} title={c["title"]!r} val={c["val"]!r}')


def _key(code, flags=0):
    import Quartz as Q
    for down in (True, False):
        ev = Q.CGEventCreateKeyboardEvent(None, code, down)
        Q.CGEventSetFlags(ev, flags)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)
        time.sleep(0.02)


def _click(x, y):
    import Quartz as Q
    pt = Q.CGPointMake(x, y)
    for et in (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp):
        ev = Q.CGEventCreateMouseEvent(None, et, pt, Q.kCGMouseButtonLeft)
        Q.CGEventPost(Q.kCGHIDEventTap, ev)
        time.sleep(0.03)


def _ax_find_titled(pid, title, max_nodes=6000):
    """First element in Claude's window whose title/description/value contains
    `title` (case-insensitive). Used to pick a session in the sidebar."""
    import ApplicationServices as AS
    from collections import deque
    want = title.lower().strip()
    app = AS.AXUIElementCreateApplication(pid)
    try:
        AS.AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)
    except Exception:
        pass
    win = _ax_attr(app, AS.kAXFocusedWindowAttribute)
    if win is None:
        return None
    q = deque([win]); seen = 0
    partial = None
    roles = ("AXButton", "AXRadioButton", "AXTab", "AXStaticText", "AXLink", "AXRow",
             "AXOutlineRow", "AXCell", "AXGroup", "AXMenuItem", "AXDisclosureTriangle")
    while q and seen < max_nodes:
        el = q.popleft(); seen += 1
        role = _ax_attr(el, AS.kAXRoleAttribute) or ""
        if role in roles:
            for attr in (AS.kAXTitleAttribute, AS.kAXDescriptionAttribute, AS.kAXValueAttribute):
                v = _ax_attr(el, attr)
                if not isinstance(v, str):
                    continue
                lv = v.lower().strip()
                if lv == want:
                    return el                       # exact label wins
                if want in lv and partial is None and role != "AXGroup":
                    partial = el
        for k in (_ax_attr(el, AS.kAXChildrenAttribute) or []):
            q.append(k)
    return partial


def _ax_press(el):
    import ApplicationServices as AS
    try:
        if AS.AXUIElementPerformAction(el, AS.kAXPressAction) == 0:
            return True
    except Exception:
        pass
    pt, sz = _ax_point(el), _ax_size(el)
    if pt and sz:
        _click(pt[0] + sz[0] / 2, pt[1] + sz[1] / 2)
        return True
    return False


def switch_session(title):
    """Bring a session to the front by clicking its sidebar entry. `title` may
    be a ">"-separated path, e.g. "Chat and Cowork>My chat" (click the tab, then
    the chat), because chat sessions are not rendered while the Code tab is
    showing. Composer cache is dropped: it belongs to the old view."""
    steps = [t.strip() for t in title.split(">") if t.strip()]
    if len(steps) > 1:
        for st in steps[:-1]:
            if not switch_session(st):
                return False
        title = steps[-1]
    try:
        from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
    except Exception:
        return False
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(CLAUDE_BUNDLE)
    if not apps:
        return False
    app = apps[0]
    app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    time.sleep(0.3)
    el = _ax_find_titled(app.processIdentifier(), title)
    if el is None:
        log(f"switch_session: no sidebar entry containing {title!r}")
        return False
    ok = _ax_press(el)
    _composer_cache["el"] = None
    time.sleep(1.2)
    log(f"switch_session: {title!r} {'selected' if ok else 'press failed'}")
    return ok


def _native_attach(path):
    """Attach a file to the focused composer: file URL on the pasteboard,
    then cmd+V (the composer takes a pasted file as an attachment, like the
    paperclip). Falls back to nothing if the file is missing. No Return."""
    import Quartz as Q
    from AppKit import NSPasteboard, NSURL
    if not Path(path).exists():
        log(f"attach: file missing {path}")
        return False
    ok, why = _native_paste("", dry_run=True)           # focus the composer only
    if not ok:
        log(f"attach: composer not focused ({why})")
        return False
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.writeObjects_([NSURL.fileURLWithPath_(str(path))])
    time.sleep(0.1)
    _key(9, Q.kCGEventFlagMaskCommand)                  # cmd+V
    time.sleep(2.0)                                     # give the upload a moment
    log(f"attach: pasted {Path(path).name}")
    return True


def _native_paste(text, dry_run=False):
    """Focus Claude's composer and paste `text` + Return. Returns (ok, why)."""
    try:
        import ApplicationServices as AS
        import Quartz as Q
        from AppKit import (NSRunningApplication, NSPasteboard, NSPasteboardTypeString,
                            NSApplicationActivateIgnoringOtherApps)
    except Exception as e:
        return False, f"pyobjc unavailable: {e}"
    if not AS.AXIsProcessTrusted():
        return False, "this process is not Accessibility-trusted"
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(CLAUDE_BUNDLE)
    if not apps:
        return False, "Claude not running"
    app = apps[0]
    pid = app.processIdentifier()
    app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    for _ in range(20):
        if app.isActive():
            break
        time.sleep(0.05)

    ax_app = AS.AXUIElementCreateApplication(pid)
    el = _composer_cache["el"]
    if el is not None and _ax_attr(el, AS.kAXRoleAttribute) != "AXTextArea":
        el = _composer_cache["el"] = None          # stale after a reload
    report = []
    if el is None:
        cands, seen, secs = _ax_find_composer(pid)
        report.append(f"search: {seen} nodes in {secs*1000:.0f}ms, {len(cands)} text inputs")
        report += ["  " + _fmt_cand(c) for c in cands]
        pick = _pick_composer(cands)
        if pick is None:
            AX_DUMP_PATH.write_text("\n".join(report) + "\n")
            return False, f"no composer found ({seen} nodes, {len(cands)} inputs)"
        el = _composer_cache["el"] = pick["el"]
        report.append("picked: " + _fmt_cand(pick))
    else:
        report.append("composer: cached")

    def focused_now():
        fe = _ax_attr(ax_app, AS.kAXFocusedUIElementAttribute)
        try:
            return fe is not None and bool(AS.CFEqual(fe, el))
        except Exception:
            return False

    AS.AXUIElementSetAttributeValue(el, AS.kAXFocusedAttribute, True)
    time.sleep(0.1)
    how = "AXFocused"
    if not focused_now():
        pt, sz = _ax_point(el), _ax_size(el)
        if pt and sz:
            _click(pt[0] + sz[0] / 2, pt[1] + min(sz[1] / 2, 20))
            time.sleep(0.15)
            how = "click"
    ok = focused_now()
    report.append(f"focus via {how}: {'OK' if ok else 'NOT focused'}")
    if dry_run:
        AX_DUMP_PATH.write_text("\n".join(report) + "\n")
        return ok, "dry-run"
    if not ok:
        AX_DUMP_PATH.write_text("\n".join(report) + "\n")
        return False, "could not focus composer"
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)
    time.sleep(0.05)
    _key(9, Q.kCGEventFlagMaskCommand)     # cmd+V
    time.sleep(0.15)
    _key(36, 0)                            # Return
    return True, "ok"


def launch_session(text=WAKE_PROMPT):
    """Type `text` into the CURRENT Claude session and send it.

    While Claude is mid-turn the desktop app queues the message, which is
    what lets a spoken instruction land while an earlier task is running.


    Deliberately NOT a new tab. A fresh session loses all history, so every
    wake would start from nothing and you would re-explain context each time.
    There is no deep link that resumes a local session -
    claude://claude.ai/code/<id> is for cloud teleport sessions and wedges the
    app - so we drive the UI, the same way the menu bar "Resume voice here"
    does. Falls back to opening a new tab only if Claude is not running.
    """
    import urllib.parse as _up

    # Saying the wake phrase is an explicit request to talk, so it clears any
    # pause exactly as the menu bar "Resume voice here" does. Without this the
    # session wakes but the agent immediately declines to start, which looks
    # like the wake word failed.
    try:
        PAUSE_FLAG.unlink(missing_ok=True)
    except Exception:
        pass

    # `System Events ... process "Claude"` enumerates every process and measured
    # 2183ms. Asking the app directly is 87ms - a 25x saving, and this ran twice
    # per wake.
    running = subprocess.run(
        ["osascript", "-e", 'application "Claude" is running'],
        capture_output=True, text=True, timeout=10).stdout.strip()

    if running != "true":
        subprocess.Popen(["open", "claude://code/new?q=" +
                          _up.quote(text, safe="")])
        log("WAKE: Claude was not running - opened a new session")
        return

    ok, why = _native_paste(text)
    if ok and text != WAKE_PROMPT:
        if _tts_playing[0] or VM_RECORDING_FLAG.exists():
            log("inject: agent is mid-step (speaking/listening) - skipping forward now")
            threading.Thread(target=fire, daemon=True).start()
            _cut_tts_until[0] = 0.0
        else:
            _cut_tts_until[0] = time.time() + CUT_TTS_WINDOW_S
    if ok:
        log(f"WAKE: sent to existing Claude session (native): {text[:70]!r}")
        return
    log(f"WAKE: native paste failed ({why}); falling back to System Events")

    esc = text.replace("\\", "\\\\").replace('"', '\\"')
    # Put the prompt on the clipboard and paste it. `keystroke` sends one
    # character at a time through the accessibility layer, which dominates the
    # wake latency; a paste is a single event regardless of length.
    script = (
        f'set the clipboard to "{esc}"\n'
        'tell application "Claude" to activate\n'
        'set ready to false\n'
        'repeat 20 times\n'
        '  delay 0.05\n'
        # Ask the app itself rather than enumerating processes through System
        # Events: same guarantee, 87ms vs 2183ms per check.
        '  if (frontmost of application "Claude") then\n'
        '    set ready to true\n'
        '    exit repeat\n'
        '  end if\n'
        'end repeat\n'
        'if not ready then return "ABORT"\n'
        'delay 0.12\n'
        'tell application "System Events"\n'
        '  keystroke "v" using command down\n'
        '  delay 0.12\n'
        '  key code 36\n'
        'end tell\n'
        'return "OK"'
    )
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=20)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    # -600 "Application isn't running" raised INSIDE the System Events block
    # means the System Events instance itself is unreachable, not Claude.
    # Seen 2026-09-05: the instance spawned by our first paste after a Claude
    # restart came up without a usable Apple Event port, and every paste
    # from every process failed for 25 minutes until it was killed. It
    # relaunches on demand, so kill it and retry once.
    if out != "OK" and "-600" in err:
        log("WAKE: System Events unreachable (-600); restarting it and retrying")
        subprocess.run(["killall", "System Events"], capture_output=True)
        time.sleep(1.0)
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=20)
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
    if out == "OK":
        log(f"WAKE: sent to existing Claude session: {text[:70]!r}")
    else:
        log(f"WAKE: could not resume ({(err or out)[:140]}) - grant Accessibility "
            f"to this daemon, or it cannot type")



def _capture_and_inject(device, preroll, floor):
    """Record one utterance (energy-gated, 1.0s hangover, 20s cap), verify
    the speaker, transcribe, and paste it as a [voice] line. Called after a
    barge-in when VoiceMode did NOT open the mic - i.e. the agent was talking
    with wait_for_response=false - so what he said is not silently lost."""
    frames = list(preroll)
    quiet = 0
    t0 = time.time()
    try:
        st = sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                            blocksize=FRAME, device=device)
        st.start()
        while time.time() - t0 < 20.0:
            buf, _ = st.read(FRAME)
            frames.append(bytes(buf))
            a = buf[:, 0].astype(np.float32)
            rms = float(np.sqrt(np.mean(a * a)))
            quiet = 0 if rms >= floor else quiet + 1
            if quiet >= int(WAKE_HANGOVER_S * 1000 / FRAME_MS) and time.time() - t0 > 0.8:
                break
        st.stop(); st.close()
    except Exception as e:
        log(f"capture: mic failed ({e})")
        return
    matched, score = is_user_speaking(frames)
    if not matched:
        log(f"capture: dropped, not your voice ({score:.2f})")
        return
    text = _strip_wake(transcribe_clip(frames, language=None, prompt=None))
    log(f"capture: {len(frames)*FRAME_MS/1000:.1f}s -> {text[:80]!r}")
    if text:
        launch_session(INJECT_PREFIX + text)


def _start_verify_server():
    """Serve speaker verification on loopback so VoiceMode can reuse the warm
    model. POST raw int16 PCM @24k; returns the cosine similarity as text."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(n)
                frames = [raw[i:i + FRAME * 2] for i in range(0, len(raw) - FRAME * 2, FRAME * 2)]
                _, score = is_user_speaking(frames)
                body = f"{score:.4f}".encode()
            except Exception as e:
                body = f"-1 {e}".encode()[:120]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass          # keep the daemon log readable

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", VERIFY_PORT), H)
    except OSError as e:
        log(f"verify server could not bind :{VERIFY_PORT} ({e})")
        return
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="bargein-verify").start()
    log(f"verify server listening on 127.0.0.1:{VERIFY_PORT}")

def write_preroll(frames):
    """Persist the rolling buffer so VoiceMode can prepend it.

    Without this, barge-in loses the front of the sentence: the daemon hears
    you, cuts playback, closes the mic, and VoiceMode then opens the mic and
    starts recording from scratch. Everything spoken during that handover is
    gone - which is why an interruption came back as just "and many more".
    """
    if not frames:
        return False
    try:
        data = b"".join(frames)
        with wave.open(str(PREROLL_PATH), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)          # int16
            w.setframerate(RATE)
            w.writeframes(data)
        secs = len(data) / 2 / RATE
        log(f"preroll written: {secs:.2f}s -> {PREROLL_PATH.name}")
        return True
    except Exception as e:
        log(f"preroll write failed: {e}")
        return False

def fire():
    """Send skip_forward. Only meaningful while VoiceMode is speaking: the
    socket exists only for the duration of an utterance."""
    if not CONTROL_SOCK.exists():
        log("TRIGGER suppressed: control socket absent (not speaking?)")
        return False
    try:
        r = subprocess.run([VOICEMODE_BIN, "control", "skip-forward"],
                           capture_output=True, text=True, timeout=6)
        ok = r.returncode == 0
        log(f"TRIGGER fired -> rc={r.returncode} {(r.stdout or r.stderr).strip()[:90]}")
        return ok
    except Exception as e:
        log(f"TRIGGER failed: {e}")
        return False


def main():
    # Aggressiveness 3 is the STRICTEST setting and rejects most of the
    # user's speech when it arrives mixed with our own TTS: measured
    # over_floor=30 frames but vad_ok=3, so VAD - not loudness - was the
    # bottleneck. We already gate hard on energy (floor 900 vs speech at
    # 2000-7000), so VAD only needs to reject non-speech transients like a
    # door or a keyboard. 1 is the right level for that.
    vad = webrtcvad.Vad(VAD_LEVEL)
    device, name = find_mic()
    log(f"starting; mic={name} (index {device})")

    load_speaker_model()
    _start_verify_server()

    try:
        floor, ambient_p90 = calibrate(device)
    except Exception as e:
        log(f"calibration failed ({e}); using MIN_FLOOR")
        floor = MIN_FLOOR; ambient_p90 = MIN_FLOOR/FLOOR_MULT

    tail = EventTail()
    armed = False
    armed_at = 0.0
    last_fire = 0.0
    hits = deque(maxlen=SUSTAIN_WINDOW)
    consec = 0
    stream = None
    ring = deque(maxlen=int(PREROLL_SECONDS * 1000 / FRAME_MS))
    # Samples taken during the suppression window at the START of each
    # utterance. That window contains ambient PLUS our own speaker bleed,
    # which is exactly what a real interruption has to beat. A single floor
    # measured once at startup drifts badly: it was calibrated at 1510 in a
    # quiet moment and 1778 later, while genuine speech measured anywhere
    # from 1934 to 7262. Adapting per utterance keeps the margin honest.
    bleed = []
    live_floor = None
    diag = {"frames": 0, "max_rms": 0.0, "vad_frames": 0,
            "over_floor": 0, "max_consec": 0}
    last_wake = 0.0
    cmd_window_until = 0.0
    # True while VoiceMode itself is recording the user. The wake listener must
    # release the mic then: two processes on the same CoreAudio input device is
    # what left recordings running to their max duration.
    vm_recording = False
    # Wake needs a HIGHER floor than barge-in, not a shared one. Barge-in has
    # to catch speech competing with our playback, so a low bar helps. Wake runs
    # in silence, where a low bar means bursts start on room noise and the clip
    # arrives padded with near-silence - which drags the speaker score from
    # ~0.65 down to ~0.46 and rejects the real speaker. Floor of 500 minimum.
    wake_floor = min(max(ambient_p90 * WAKE_FLOOR_MULT, 500.0), 900.0)
    wake_buf = []
    wake_active = False
    wake_quiet = 0
    # Rolling ambient estimate while idle. The startup calibration is a single
    # second and is easily poisoned - it measured 1570 once while our own TTS
    # was playing, which pinned the wake floor at its 900 ceiling for 28 hours
    # and turned every "hey Claude" into a near-miss. Track quiet frames
    # continuously instead and recompute the floor from their median.
    idle_bg = deque(maxlen=int(20.0 * 1000 / FRAME_MS))   # ~20s of frames
    # Suppress wake-mode listening for a grace window whenever VoiceMode is
    # LIKELY to open the mic next. vm_recording is set from the event-log tail,
    # which lags by up to a second or two; in that gap the daemon was dropping
    # into wake mode and grabbing the mic while VoiceMode was recording -
    # two readers on one CoreAudio device, and silence detection ran to max.
    wake_hold_until = 0.0
    WAKE_HOLD_S = 4.0
    # Set when barge-in fires; if VoiceMode has not opened the mic within
    # 1.5s, the agent was not going to listen, so we capture the words.
    capture_after_fire = None
    last_recal = time.time()
    write_status("idle")

    def close_stream():
        nonlocal stream, consec
        if stream is not None:
            s_, stream = stream, None
            # PortAudio's stop can deadlock on a CoreAudio HAL mutex
            # (FinishStoppingStream -> AudioOutputUnitStop -> HALB_Mutex::Lock,
            # sampled 2026-09-05 23:47). The process then looks alive - launchd
            # KeepAlive never restarts it - and barge-in and wake are silently
            # dead until someone notices. cffi releases the GIL during the C
            # call, so run it on a thread and bound the wait; if it never
            # returns, exit and let launchd bring up a clean process in ~5s.
            def _stop():
                try:
                    s_.stop(); s_.close()
                except Exception:
                    pass
            t = threading.Thread(target=_stop, daemon=True)
            t.start(); t.join(STREAM_STOP_TIMEOUT_S)
            if t.is_alive():
                log(f"FATAL: CoreAudio stream stop deadlocked (>{STREAM_STOP_TIMEOUT_S}s); "
                    f"exiting so launchd restarts a clean daemon")
                write_status("error", "coreaudio stop deadlock - restarting")
                os._exit(3)
        consec = 0

    try:
        while True:
            # Manual/debug injection: paste the file's contents as if spoken.
            # Also the only way to exercise the paste path without a mic.
            if INJECT_FILE.exists():
                try:
                    _txt = INJECT_FILE.read_text().strip()
                    INJECT_FILE.unlink()
                    if _txt.startswith("@@session="):
                        try:
                            head, msg = _txt.split(";msg=", 1)
                            parts = dict(kv.split("=", 1) for kv in head[2:].split(";") if "=" in kv)
                            target, back = parts.get("session", ""), parts.get("back", "")
                            if switch_session(target):
                                close_stream()
                                for f in [x for x in parts.get("file", "").split("|") if x]:
                                    _native_attach(f)
                                launch_session(msg)
                                _cut_tts_until[0] = 0.0     # not for this session
                                log(f"inject: typed into session {target!r}")
                                if back:
                                    time.sleep(1.0)
                                    switch_session(back)
                            else:
                                log(f"inject: session {target!r} not found; nothing typed")
                        except Exception as e:
                            log(f"inject: session command failed ({e})")
                    elif _txt.startswith("@@axtitles"):
                        try:
                            _path = _txt.split("=", 1)[1] if "=" in _txt else ""
                            if _path:
                                switch_session(_path)
                                time.sleep(1.0)
                            import ApplicationServices as AS
                            from AppKit import NSRunningApplication
                            _app = NSRunningApplication.runningApplicationsWithBundleIdentifier_(CLAUDE_BUNDLE)[0]
                            _ax = AS.AXUIElementCreateApplication(_app.processIdentifier())
                            AS.AXUIElementSetAttributeValue(_ax, "AXManualAccessibility", True)
                            _win = _ax_attr(_ax, AS.kAXFocusedWindowAttribute)
                            _q, _n, _out = deque([(_win, 0)]), 0, []
                            while _q and _n < 8000:
                                _el, _d = _q.popleft(); _n += 1
                                _r = _ax_attr(_el, AS.kAXRoleAttribute) or ""
                                _t = _ax_attr(_el, AS.kAXTitleAttribute) or ""
                                _ds = _ax_attr(_el, AS.kAXDescriptionAttribute) or ""
                                _v = _ax_attr(_el, AS.kAXValueAttribute)
                                _v = _v if isinstance(_v, str) else ""
                                if (_t or _ds or (_v and len(_v) < 60)) and _r not in ("AXTextArea",):
                                    _out.append(f"{_r} d={_d} title={_t[:50]!r} desc={_ds[:50]!r} val={_v[:50]!r}")
                                for _k in (_ax_attr(_el, AS.kAXChildrenAttribute) or []):
                                    _q.append((_k, _d + 1))
                            AX_DUMP_PATH.write_text(f"nodes={_n}\n" + "\n".join(_out) + "\n")
                            log(f"axtitles: {len(_out)} labelled elements of {_n}")
                            if _path:
                                switch_session(os.environ.get("BARGEIN_HOME_SESSION", "Code"))
                        except Exception as e:
                            log(f"axtitles: failed ({e})")
                    elif _txt == "@@axdump":
                        _ax_dump()
                    elif _txt == "@@axfind":
                        ok, why = _native_paste("", dry_run=True)
                        log(f"axfind: ok={ok} {why}")
                    elif _txt:
                        close_stream()
                        launch_session(_txt)
                        log(f"inject: pasted from file ({len(_txt)} chars)")
                except Exception as e:
                    log(f"inject: failed ({e})")
            if capture_after_fire and time.time() - capture_after_fire["t"] >= 1.5:
                caf, capture_after_fire = capture_after_fire, None
                if vm_recording or VM_RECORDING_FLAG.exists():
                    log("capture: VoiceMode is listening, leaving it to the mic")
                else:
                    log("capture: agent was not listening after the interrupt - recording you")
                    close_stream()
                    _capture_and_inject(device, caf["pre"], max(caf["floor"] or floor, floor))
            for ev in tail.events():
                et = ev.get("event_type")
                if et in ("RECORDING_END", "STT_START", "TOOL_REQUEST_END"):
                    if vm_recording:
                        vm_recording = False
                        log("wake listening resumed (VoiceMode released mic)")

                if et == "TTS_PLAYBACK_START":
                    _tts_playing[0] = True
                    if time.time() < _cut_tts_until[0]:
                        _cut_tts_until[0] = 0.0
                        log("TTS started with an injected message pending - cutting it "
                            "short so the agent reads the message now")
                        # The control socket binds only once playback is
                        # underway; give it a moment, then skip forward.
                        threading.Timer(1.0, fire).start()
                        last_fire = time.time()
                        continue
                    if time.time() - last_fire < COOLDOWN_S:
                        log("arm skipped: within cooldown")
                        continue
                    armed, armed_at, consec = True, time.time(), 0
                    ring.clear()
                    hits.clear()
                    bleed = []
                    live_floor = None
                    diag = {"frames": 0, "max_rms": 0.0, "vad_frames": 0,
                            "over_floor": 0, "max_consec": 0}
                    write_status("armed")
                    log("ARMED (playback started)")
                elif et in ("TTS_PLAYBACK_END", "RECORDING_START"):
                    _tts_playing[0] = False
                    if et == "TTS_PLAYBACK_END":
                        # A recording almost always follows playback. Do not
                        # touch the mic until we know one way or the other.
                        wake_hold_until = time.time() + WAKE_HOLD_S
                        close_stream()
                    if et == "RECORDING_START":
                        vm_recording = True
                        close_stream()          # hand the mic to VoiceMode
                        log("mic handed to VoiceMode (RECORDING_START)")
                    if armed:
                        log(f"DISARMED ({et}) DIAG frames={diag['frames']} "
                            f"max_rms={diag['max_rms']:.0f} floor={live_floor if live_floor else floor:.0f} "
                            f"over_floor={diag['over_floor']} vad_ok={diag['vad_frames']} "
                            f"max_hits={diag['max_consec']}/{SUSTAIN_HITS} in {SUSTAIN_WINDOW}")
                    armed = False
                    close_stream()
                    write_status("idle")

            if armed and stream is None:
                try:
                    stream = sd.InputStream(samplerate=RATE, channels=1,
                                            dtype="int16", blocksize=FRAME,
                                            device=device)
                    stream.start()
                except Exception as e:
                    log(f"mic open failed: {e}")
                    armed = False
                    write_status("error", str(e)[:80])

            if armed and stream is not None:
                try:
                    buf, overflow = stream.read(FRAME)
                except Exception as e:
                    log(f"mic read failed: {e}")
                    close_stream(); armed = False
                    write_status("error", str(e)[:80])
                    continue

                # Buffer every frame, including suppressed ones: the user may
                # start speaking inside the suppression window and we still want
                # those samples in the preroll.
                ring.append(bytes(buf))

                a = buf[:, 0].astype(np.float32)
                rms = float(np.sqrt(np.mean(a * a)))
                diag["frames"] += 1
                diag["max_rms"] = max(diag["max_rms"], rms)

                # Our own first syllable is the classic self-trigger. Use that
                # window to measure how loud our own playback reads on this mic.
                if time.time() - armed_at < ARM_SUPPRESS_S:
                    bleed.append(rms)
                    continue

                # Track the background level CONTINUOUSLY while no candidate
                # run is in progress. Sampling only a fixed window at the start
                # failed: TTS_PLAYBACK_START fires before sound actually leaves
                # the speaker, so that window measured silence (bleed p90=40),
                # set the floor at bare ambient, and our own playback at 605
                # then triggered a false interrupt.
                # Feed the background estimate ONLY with frames that are quiet
                # relative to what we have already seen. The previous version
                # appended whenever consec == 0, which meant the FIRST frames of
                # real speech were absorbed into the baseline they were supposed
                # to beat: the floor chased the voice upward, the run never
                # reached SUSTAIN_FRAMES, and a genuine interruption was missed
                # entirely (03:41 armed 24s, never fired). Using a median of
                # quiet frames keeps the baseline anchored to silence/bleed.
                if not bleed:
                    bleed.append(rms)
                else:
                    med = float(np.median(bleed))
                    if rms <= max(med * 2.0, floor):
                        bleed.append(rms)
                        if len(bleed) > BG_WINDOW:
                            bleed.pop(0)

                # Refuse to trigger until we actually know how loud playback is
                # on this mic. Otherwise the first moments are decided against a
                # floor derived from silence.
                if len(bleed) < BG_MIN_SAMPLES:
                    hits.clear()
                    continue

                # Median, not p75: p75 drifts up as soon as any loud frame
                # slips in, and the margin collapses (a false fire went off
                # at rms 714 vs floor 603 - only 1.18x).
                live_floor = max(floor, float(np.median(bleed)) * BG_MULT)

                if rms < live_floor:
                    hits.append(0)
                    continue
                diag["over_floor"] += 1

                # Absolute sanity margin: a trigger 1.18x over the floor was
                # our own speaker. Genuine speech measured 3023 against 603.
                if rms < floor * MIN_MARGIN:
                    hits.append(0)
                    continue

                r16 = signal.resample(buf[:, 0], int(FRAME * VAD_RATE / RATE)).astype(np.int16)
                try:
                    is_speech = vad.is_speech(r16.tobytes(), VAD_RATE)
                except Exception:
                    is_speech = False

                if not is_speech:
                    hits.append(0)
                    continue
                diag["vad_frames"] += 1

                hits.append(1)
                consec = sum(hits)
                diag["max_consec"] = max(diag["max_consec"], consec)
                if consec >= SUSTAIN_HITS:
                    # Final gate: is this actually the enrolled speaker? This is
                    # what stops our own TTS from triggering an interrupt.
                    verify_frames = list(ring)[-int(VERIFY_SECONDS * 1000 / FRAME_MS):]
                    matched, score = is_user_speaking(verify_frames)
                    if not matched:
                        log(f"speech over TTS REJECTED - not your voice "
                            f"(similarity {score:.2f} < {SPEAKER_THRESHOLD})")
                        hits.clear()
                        continue
                    log(f"YOUR VOICE detected over TTS (rms={rms:.0f} floor={live_floor:.0f} similarity={score:.2f}) - interrupting")
                    write_preroll(list(ring))
                    fire()
                    _ack()
                    capture_after_fire = {"t": time.time(), "pre": list(ring), "floor": live_floor}
                    # skip_forward makes VoiceMode fall straight into its
                    # listen turn. Hold wake mode off NOW rather than waiting
                    # for RECORDING_START to trickle through the log.
                    wake_hold_until = time.time() + WAKE_HOLD_S
                    last_fire = time.time()
                    armed = False
                    close_stream()
                    write_status("fired")
            elif (WAKE_ENABLED and not vm_recording
                  and not VM_RECORDING_FLAG.exists()
                  and time.time() >= wake_hold_until):
                # IDLE: listen for the wake phrase. Same stream discipline as
                # barge-in - open only while we need it.
                if stream is None:
                    try:
                        stream = sd.InputStream(samplerate=RATE, channels=1,
                                                dtype="int16", blocksize=FRAME,
                                                device=device)
                        stream.start()
                    except Exception as e:
                        log(f"wake mic open failed: {e}")
                        time.sleep(2.0)
                        continue
                try:
                    buf, _of = stream.read(FRAME)
                except Exception:
                    close_stream(); time.sleep(0.5); continue

                a = buf[:, 0].astype(np.float32)
                rms = float(np.sqrt(np.mean(a * a)))
                loud = rms >= wake_floor

                # Only frames below the floor feed the ambient estimate, so a
                # burst of speech cannot inflate it. Median (not mean/p90) so a
                # stray loud frame that slips under the floor barely moves it.
                if not loud:
                    idle_bg.append(rms)
                if time.time() - last_recal > 15.0 and len(idle_bg) >= 100:
                    last_recal = time.time()
                    new_floor = min(max(float(np.median(idle_bg)) * WAKE_FLOOR_MULT, 500.0), 900.0)
                    if abs(new_floor - wake_floor) > 25:
                        log(f"wake floor recalibrated {wake_floor:.0f} -> {new_floor:.0f} "
                            f"(ambient median {float(np.median(idle_bg)):.0f})")
                        wake_floor = new_floor

                _cap = int(CMD_MAX_SPEECH_S * 1000 / FRAME_MS)
                if loud:
                    if len(wake_buf) < _cap:
                        wake_buf.append(bytes(buf))
                    wake_active = True
                    wake_quiet = 0
                elif wake_active and wake_quiet < int(WAKE_HANGOVER_S * 1000 / FRAME_MS):
                    # Inside the hangover: keep the frame, keep the burst open.
                    if len(wake_buf) < _cap:
                        wake_buf.append(bytes(buf))
                    wake_quiet += 1
                elif wake_active:
                    # Burst ended. Only now do the expensive checks.
                    dur = len(wake_buf) * FRAME_MS / 1000.0
                    full = list(wake_buf)
                    # The phrase is always at the START; detect on that
                    # slice only so a long command does not dilute the match.
                    clip = full[:int(WAKE_DETECT_S * 1000 / FRAME_MS)]
                    wake_buf = []; wake_active = False; wake_quiet = 0
                    peak = max((float(np.sqrt(np.mean(np.frombuffer(f,dtype=np.int16).astype(np.float32)**2))) for f in clip), default=0.0)
                    log(f"wake: burst {dur:.2f}s peak_rms={peak:.0f} (floor {wake_floor:.0f})")
                    if dur < WAKE_MIN_SPEECH_S:
                        continue
                    if (time.time() - last_wake < WAKE_COOLDOWN_S
                            and time.time() >= cmd_window_until):
                        continue
                    _t0 = time.time()
                    _, score = is_user_speaking(clip)
                    _t_spk = time.time() - _t0
                    if score >= 0 and score < WAKE_SPEAKER_THRESHOLD:
                        # Keep the audio we rejected. Guessing at why a score
                        # dropped has cost several rounds tonight; having the
                        # actual clip makes it a measurement instead.
                        try:
                            import wave as _w
                            dbg = HOME / ".voicemode" / "indicator" / "rejected"
                            dbg.mkdir(exist_ok=True)
                            fn = dbg / f"rej_{int(time.time())}_{score:.2f}.wav"
                            with _w.open(str(fn), "wb") as w:
                                w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
                                w.writeframes(b"".join(clip))
                        except Exception:
                            pass
                        log(f"wake: ignored - not your voice ({score:.2f})")
                        continue
                    _t1 = time.time()
                    text = transcribe_clip(clip).lower()
                    _t_stt = time.time() - _t1
                    if not text:
                        continue
                    log(f"wake: heard {text[:60]!r} (speaker {score:.2f}) [verify {_t_spk*1000:.0f}ms stt {_t_stt*1000:.0f}ms]")
                    if any(ph in text for ph in WAKE_PHRASES):
                        last_wake = time.time()
                        _ack()
                        close_stream()
                        # Anything after the phrase is a command. The
                        # detection pass saw only the opening slice, forced
                        # to English; re-run the WHOLE burst with auto
                        # language so a mixed-language instruction and its
                        # original casing survive.
                        cmd = _strip_wake(text)
                        if cmd or dur > WAKE_DETECT_S:
                            _t3 = time.time()
                            cmd = _strip_wake(transcribe_clip(full, language=None, prompt=None))
                            log(f"wake: command stt {(time.time()-_t3)*1000:.0f}ms "
                                f"({dur:.1f}s) -> {cmd[:80]!r}")
                        _t2 = time.time()
                        launch_session(INJECT_PREFIX + cmd if cmd else WAKE_PROMPT)
                        log(f"wake: launch took {(time.time()-_t2)*1000:.0f}ms")
                        # No blocking sleep here: it is exactly what would
                        # swallow a follow-up burst. The cooldown on
                        # last_wake already prevents an immediate re-wake.
                        cmd_window_until = 0.0 if cmd else time.time() + CMD_WINDOW_S
                    elif time.time() < cmd_window_until:
                        cmd_window_until = 0.0
                        close_stream()
                        _t3 = time.time()
                        cmd = _strip_wake(transcribe_clip(full, language=None, prompt=None))
                        log(f"wake: follow-up command stt {(time.time()-_t3)*1000:.0f}ms "
                            f"({dur:.1f}s) -> {cmd[:80]!r}")
                        if cmd:
                            launch_session(INJECT_PREFIX + cmd)
                else:
                    time.sleep(0.01)
            else:
                if stream is not None and (vm_recording or not armed
                                           or VM_RECORDING_FLAG.exists()
                                           or time.time() < wake_hold_until):
                    close_stream()
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        close_stream()
        write_status("stopped")


if __name__ == "__main__":
    sys.exit(main())
