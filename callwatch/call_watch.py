#!/usr/bin/env python3
"""
Call watch: go silent on Teams / WhatsApp calls, record both sides, transcribe after.

The request behind it: whenever a call comes in over WhatsApp or Teams, the
agent goes silent automatically, but stays there and listens to everything,
including the remote sound and the mic, and then digests that as well; it may
be one or two long calls.

WHAT IT DOES
  1. Detects a live call. Every 2 s it asks CoreAudio (macOS 14+) for the process
     objects of Microsoft Teams (com.microsoft.teams2 + its helpers) and WhatsApp
     (net.whatsapp.WhatsApp) and reads kAudioProcessPropertyIsRunningInput: an app
     holding the microphone open AND running output is in a call (input alone is a
     voice note being recorded, output alone a ringtone). Start after START_AFTER_S
     continuous, end after END_AFTER_S without input. Detection runs through
     ./audiotap/audiotap (Swift, see audiotap/main.swift); if the binary is missing
     the same query is done with ctypes on CoreAudio directly (no tap in that mode).
  2. Records the call with audiotap: the microphone (IOProc on the headset named by
     CALLWATCH_MIC, or the default input; shared with other clients) and the REMOTE
     side through a CoreAudio process tap on the app's process objects wrapped in a
     private aggregate device - no BlackHole, no loopback device. 16 kHz mono WAV in
     10-minute chunks:
       ~/.voicemode/calls/<YYYYMMDD-HHMM>-<app>/mic-NNN.wav, remote-NNN.wav, mix-NNN.wav
     The recorder is supervised: if it dies mid-call it is restarted at the next chunk
     number. Memory is bounded (chunking), so a 2-hour call is fine.
     Note: the aggregate only starts ticking once the tapped app produces output
     (kAudioAggregateDeviceTapAutoStartKey); in a call it always does. Until then the
     mix is padded with silence.
  3. Tells the agent through the barge-in daemon's inject.txt (same path as
     inbox_hooks._flush_inject):
       [call] started: <Teams|WhatsApp> at HH:MM
       [call] ended: <app> after <N> min, recording at <dir>, transcript pending
       [call] transcript ready: <path> (<N> words)
     and keeps ~/.voicemode/indicator/call_active.json while a call is on. The
     barge-in daemon treats "[call]" like "[inbox]" (never cuts TTS) and stops
     wake-word / barge-in listening while call_active.json exists, so the voice
     loop pauses itself and never grabs the mic during a call.
  4. Transcribes afterwards: each chunk -> ffmpeg -ar 16000 -ac 1 -> POST to the
     whisper server (CALLWATCH_WHISPER_URL, OpenAI shape, verbose_json, language
     auto; re-run as Urdu when it says hi/pa, like inbox_hooks._whisper), quiet
     chunks get a fixed gain first, silent ones are skipped, pieces with under
     CALLWATCH_MIN_SPEECH_S seconds of speech are not uploaded, and repeated
     segments (whisper looping on silence) are dropped. transcript.txt has one
     line per segment, sorted by time, tagged MIC / REMOTE. The agent summarises it;
     this script never does.
  5. Runs as ~/Library/LaunchAgents/com.voicemode.callwatch.plist (KeepAlive,
     RunAtLoad) under the indicator venv python. Log: indicator/callwatch.log.
     Times in the [call] lines and call.json are in AGENT_TZ (an IANA name), or
     the machine's local zone when unset.

USAGE
  call_watch.py                 run the daemon
  call_watch.py --dry-run       print the detection state as JSON and exit (alias --once)
  call_watch.py --test-record N record N seconds of mic (+ a tap of Teams/WhatsApp if they
                                run) to /tmp/callwatch-test/, then exit; checks with ffprobe
  call_watch.py --transcribe DIR  (re)build DIR/transcript.txt and exit; no inject

PERMISSIONS (first run)
  audiotap re-spawns itself as its own TCC "responsible process", so macOS attributes
  the prompts to "audiotap" (signed with the Apple Development identity found in the
  keychain when there is one, so grants survive rebuilds): Microphone, and "System
  Audio Recording Only" (Privacy & Security > Screen & System Audio Recording) for
  the process tap. Grant both on the first --test-record. No prompt is expected for
  python.

FILES
  ~/.voicemode/indicator/call_active.json   {"app","pid","since","dir"} while a call is on
  ~/.voicemode/indicator/callwatch.log      this log
  ~/.voicemode/calls/<stamp>-<app>/call.json  metadata written at the end of the call
  ~/.voicemode/indicator/call_pending.jsonl   [call] lines that could not be pasted (Claude not running)

TEST HOOKS (env)
  AUDIOTAP_MATCH_NAME=ffmpeg   audiotap treats processes with that name as "Teams", so
                               `ffmpeg -f avfoundation -i ":<your mic name>" -t 60 x.wav`
                               (holds the mic) plus `ffmpeg -re -f lavfi -i sine=440 -af
                               volume=0.02 -f audiotoolbox -` (plays) simulate a call.
  CALLWATCH_INJECT_FILE / CALLWATCH_ACTIVE_FILE / CALLWATCH_CALLS_DIR / CALLWATCH_LOG
                               redirect the files so a simulation never touches the live
                               session. CALLWATCH_CHUNK_S=15 exercises chunk rotation.
  Verified with that simulation: start/end detection, 3 chunks, tap_update on process
  exit, daemon SIGKILL mid-call -> audiotap exits, restart resumes at the next chunk,
  recorder SIGKILL -> restarted at the next chunk, transcript built and all three
  [call] lines produced.
"""
import dns_fallback  # noqa: F401  DNS-over-HTTPS fallback when the system resolver dies
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path.home()
IND = HOME / ".voicemode" / "indicator"
AUDIOTAP = IND / "audiotap" / "audiotap"
# The three paths below can be redirected for tests (CALLWATCH_INJECT_FILE etc.)
# so a simulated call never reaches the live Claude session.
INJECT_FILE = Path(os.environ.get("CALLWATCH_INJECT_FILE", IND / "inject.txt"))
CALL_ACTIVE = Path(os.environ.get("CALLWATCH_ACTIVE_FILE", IND / "call_active.json"))
CALLS_DIR = Path(os.environ.get("CALLWATCH_CALLS_DIR", HOME / ".voicemode" / "calls"))
LOG_PATH = Path(os.environ.get("CALLWATCH_LOG", IND / "callwatch.log"))
# Any OpenAI-shaped /v1/audio/transcriptions endpoint; the local whisper by
# default, a LAN GPU box running large-v3 in the reference setup.
WHISPER_URL = os.environ.get("CALLWATCH_WHISPER_URL",
                             "http://127.0.0.1:2022/v1/audio/transcriptions")


def _tz():
    name = os.environ.get("AGENT_TZ", "")
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


LOCAL_TZ = _tz()

POLL_S = float(os.environ.get("CALLWATCH_POLL_S", 2.0))
# 20 s, not 3: recording a WhatsApp voice note also runs input and output for
# a few seconds and was flagged as a "call" four times in one afternoon of
# voice notes. A real call always outlives 20 s; a voice note rarely does.
START_AFTER_S = float(os.environ.get("CALLWATCH_START_AFTER_S", 20.0))
END_AFTER_S = float(os.environ.get("CALLWATCH_END_AFTER_S", 8.0))
CHUNK_S = int(os.environ.get("CALLWATCH_CHUNK_S", 600))
# Seconds per upload to whisper (see transcribe_dir).
PIECE_S = int(os.environ.get("CALLWATCH_PIECE_S", 90))
# A call has the app running BOTH input and output. Input alone is a WhatsApp voice
# note being recorded or a Teams device test; output alone is a ringtone or a
# voice note playing. So the start needs both; once started, the call lasts as
# long as the input side is held (END_AFTER_S without it ends the call).
REQUIRE_OUTPUT = os.environ.get("CALLWATCH_REQUIRE_OUTPUT", "1") != "0"
# Substring of the input device name to record from (your headset); empty
# means the system default input.
MIC_DEVICE = os.environ.get("CALLWATCH_MIC", "")
# Minimum seconds of detected speech for a piece to be worth uploading. Whisper
# hallucinates on pieces where the speaker only listens (a 21-minute call once
# came back as one sentence repeated 280 times on the mic side), so pieces with
# almost no speech are not uploaded at all.
MIN_SPEECH_S = float(os.environ.get("CALLWATCH_MIN_SPEECH_S", "6"))
APPS = ("Teams", "WhatsApp")
# launchd agents get a bare PATH; homebrew ffmpeg lives outside it.
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"

_stdout_log = [sys.stdout.isatty() or os.environ.get("CALLWATCH_STDOUT") == "1"]


def log(msg):
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}"
    if _stdout_log[0]:
        print(line, flush=True)
    try:
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def local_now():
    return datetime.now(timezone.utc).astimezone(LOCAL_TZ)


# ----------------------------------------------------------------------------- detection

def detect_audiotap():
    """{"Teams": {"present","input","output","pids","input_pids"}, "WhatsApp": {...}}"""
    r = subprocess.run([str(AUDIOTAP), "detect"], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError(f"audiotap detect rc={r.returncode}: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)["apps"]


def _fourcc(s):
    return int.from_bytes(s.encode("ascii"), "big")


def detect_ctypes():
    """Fallback without the Swift binary: same CoreAudio properties over ctypes.
    Process -> app mapping uses the executable path (proc_pidpath)."""
    import ctypes

    ca = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    libproc = ctypes.cdll.LoadLibrary("/usr/lib/libproc.dylib")

    class Addr(ctypes.Structure):
        _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]

    def addr(sel):
        return Addr(_fourcc(sel), _fourcc("glob"), 0)

    def get_size(obj, sel):
        a, size = addr(sel), ctypes.c_uint32(0)
        st = ca.AudioObjectGetPropertyDataSize(ctypes.c_uint32(obj), ctypes.byref(a), 0, None, ctypes.byref(size))
        return size.value if st == 0 else 0

    def get_u32s(obj, sel):
        n = get_size(obj, sel) // 4
        if n == 0:
            return []
        a, size = addr(sel), ctypes.c_uint32(n * 4)
        buf = (ctypes.c_uint32 * n)()
        st = ca.AudioObjectGetPropertyData(ctypes.c_uint32(obj), ctypes.byref(a), 0, None, ctypes.byref(size), buf)
        return list(buf) if st == 0 else []

    def get_u32(obj, sel):
        v = get_u32s(obj, sel)
        return v[0] if v else 0

    def pid_path(pid):
        buf = ctypes.create_string_buffer(4 * 1024)
        n = libproc.proc_pidpath(ctypes.c_int(pid), buf, ctypes.c_uint32(len(buf)))
        return buf.value.decode("utf-8", "replace") if n > 0 else ""

    def classify(path):
        if "/Microsoft Teams" in path:
            return "Teams"
        if "/WhatsApp.app/" in path:
            return "WhatsApp"
        return None

    out = {a: {"present": False, "input": False, "output": False, "pids": [], "input_pids": []} for a in APPS}
    for obj in get_u32s(1, "prs#"):           # kAudioObjectSystemObject, kAudioHardwarePropertyProcessObjectList
        pid = get_u32(obj, "ppid")
        app = classify(pid_path(pid))
        if not app:
            continue
        inp = get_u32(obj, "piri") == 1
        outp = get_u32(obj, "piro") == 1
        d = out[app]
        d["present"] = True
        d["pids"].append(pid)
        if inp:
            d["input"] = True
            d["input_pids"].append(pid)
        if outp:
            d["output"] = True
    return out


def detect():
    if AUDIOTAP.exists():
        try:
            return detect_audiotap(), "audiotap"
        except Exception as e:
            log(f"detect: audiotap failed ({e}); using ctypes")
    return detect_ctypes(), "ctypes"


# ----------------------------------------------------------------------------- inject

def _claude_running():
    # Not `pgrep -x Claude` (inbox_hooks does that): pgrep did not see the main
    # Claude process at all on one Mac (-x and -f both missed the pid while ps
    # listed it). Ask LaunchServices by bundle id, like the daemon.
    try:
        from AppKit import NSRunningApplication
        return bool(NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.anthropic.claudefordesktop"))
    except Exception:
        r = subprocess.run(["osascript", "-e", 'application "Claude" is running'],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "true"


_inject_lock = threading.Lock()


def inject(text):
    """Hand a line to the barge-in daemon exactly like inbox_hooks._flush_inject:
    write INJECT_FILE; the daemon pastes it into the Claude session and unlinks it.
    If a previous line has not been consumed yet, wait a little, then merge."""
    with _inject_lock:
        return _inject(text)


def _inject(text):
    if not _claude_running():
        log(f"inject: Claude not running; not pasted: {text!r}")
        try:
            with (IND / "call_pending.jsonl").open("a") as f:
                f.write(json.dumps({"ts": time.time(), "line": text}) + "\n")
        except OSError:
            pass
        return False
    for _ in range(20):
        if not INJECT_FILE.exists():
            break
        time.sleep(0.5)
    if INJECT_FILE.exists():
        try:
            prev = INJECT_FILE.read_text().strip()
        except OSError:
            prev = ""
        if prev and not prev.startswith("@@"):
            text = prev + " | " + text
    INJECT_FILE.write_text(text[:1500])
    log(f"inject: {text[:160]!r}")
    return True


# ----------------------------------------------------------------------------- recorder

class Recorder:
    """One audiotap record process. stdout JSON lines are logged; chunk numbers and
    the mic/tap availability are tracked so a restart can continue the numbering."""

    def __init__(self, app, out_dir, start_index=1, seconds=0, tap=True):
        self.app, self.out_dir = app, Path(out_dir)
        self.proc = None
        self.mic = self.tap = None
        self.last_index = start_index - 1
        self.events = 0
        cmd = [str(AUDIOTAP), "record", "--out", str(self.out_dir), "--chunk-seconds", str(CHUNK_S),
               "--rate", "16000", "--start-index", str(start_index), "--mic-device", MIC_DEVICE]
        if app in APPS and tap:
            cmd += ["--app", app]
        else:
            cmd += ["--no-tap"]
        if seconds:
            cmd += ["--seconds", str(seconds)]
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        threading.Thread(target=self._pump, args=(self.proc.stdout, False), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, True), daemon=True).start()
        log(f"recorder: started pid {self.proc.pid}: {' '.join(cmd[1:])}")

    def _pump(self, stream, is_err):
        for line in stream:
            line = line.rstrip()
            if not line:
                continue
            if is_err:
                log(f"recorder[err]: {line[:300]}")
                continue
            self.events += 1
            try:
                ev = json.loads(line)
            except Exception:
                log(f"recorder: {line[:300]}")
                continue
            kind = ev.get("event")
            if kind == "started":
                self.mic, self.tap = bool(ev.get("mic")), bool(ev.get("tap"))
                log(f"recorder: recording mic={self.mic} tap={self.tap} -> {ev.get('dir')}")
            elif kind == "chunk":
                self.last_index = max(self.last_index, int(ev.get("index", 0)))
                log(f"recorder: chunk {ev.get('side')}-{ev.get('index'):03d}")
            elif kind in ("status",):
                log(f"recorder: {ev.get('elapsed')}s mic_cb={ev.get('mic_callbacks')} "
                    f"remote_cb={ev.get('remote_callbacks')} dropped={ev.get('dropped')}")
            elif kind == "stopped":
                log(f"recorder: stopped after {ev.get('elapsed')}s "
                    f"(mic_cb={ev.get('mic_callbacks')} remote_cb={ev.get('remote_callbacks')})")
            elif kind in ("tap", "mic", "tap_update", "tap_agg", "remote_first_audio"):
                log(f"recorder: {kind} {json.dumps({k: v for k, v in ev.items() if k not in ('event', 't')})[:200]}")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout=20.0):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
            except Exception:
                pass
            t0 = time.time()
            while self.proc.poll() is None and time.time() - t0 < timeout:
                time.sleep(0.2)
            if self.proc.poll() is None:
                log("recorder: did not stop in time; killing")
                self.proc.kill()
                self.proc.wait(5)
        log(f"recorder: exit code {self.proc.returncode}")


def kill_stale_recorders():
    subprocess.run(["pkill", "-f", f"{AUDIOTAP} record"], capture_output=True)


# ----------------------------------------------------------------------------- transcription

def _whisper(path, language=None):
    """POST one WAV to the remote whisper. Returns (text, segments, detected_language).
    Same Urdu rule as inbox_hooks._whisper: auto-detect first, re-run as ur on hi/pa."""
    data = path.read_bytes()
    b = "----callwatch"
    lang = f"--{b}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{language}\r\n" if language else ""
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\nverbose_json\r\n" + lang +
            f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(WHISPER_URL, data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        j = json.loads(raw)
    except Exception:
        return raw.strip(), [], ""
    text = (j.get("text") or "").strip()
    detected = (j.get("language") or "").lower()
    segs = [(float(s.get("start", 0)), float(s.get("end", 0)), (s.get("text") or "").strip())
            for s in (j.get("segments") or []) if (s.get("text") or "").strip()]
    if language is None and (detected in ("hi", "hindi", "pa", "panjabi", "punjabi")
                             or any("\u0900" <= ch <= "\u097f" or "\u0a00" <= ch <= "\u0a7f" for ch in text)):
        log(f"whisper: {path.name} detected {detected or 'devanagari'}; re-running as Urdu")
        return _whisper(path, language="ur")
    return text, segs, detected


def _peak_db(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if m else 0.0


def _prepare_chunk(src, dst):
    """ffmpeg -y -i chunk.wav -ar 16000 -ac 1 chunk16.wav (already 16k; kept for safety).
    A quiet chunk (tap audio can arrive well below -30 dBFS) gets a fixed gain so
    whisper sees a normal level; a silent one returns None and is skipped."""
    peak = _peak_db(src)
    if peak <= -70.0:
        return None, peak
    # 80 Hz high-pass: the headset mic carries a lot of sub-30 Hz rumble (seen at
    # rms ~2000 with the spectral centroid at 30 Hz) that only hurts whisper.
    filters = ["highpass=f=80"]
    if peak < -20.0:
        filters.append(f"volume={min(-6.0 - peak, 36.0):.1f}dB")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src), "-ar", "16000", "-ac", "1",
                    "-af", ",".join(filters), str(dst)], check=True, capture_output=True)
    return dst, peak


def _split(src, tx, stem):
    """Cut a prepared 16 kHz WAV into PIECE_S-second files; returns them in order."""
    pattern = tx / f"{stem}.p%03d.wav"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src), "-f", "segment",
                    "-segment_time", str(PIECE_S), "-c", "copy", str(pattern)], check=True, capture_output=True)
    return sorted(tx.glob(f"{stem}.p*.wav"))


def _fmt_t(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def _speech_seconds(path, thresh_db=-38.0, min_sil=0.5):
    """Seconds of non-silence in a WAV per ffmpeg silencedetect, None if unknown."""
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path), "-af",
                            f"silencedetect=noise={thresh_db}dB:d={min_sil}", "-f", "null", "-"],
                           capture_output=True, text=True)
    except Exception:
        return None
    dur = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    silent = sum(float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", r.stderr))
    return max(0.0, dur - silent) if dur else None


def _norm_txt(t):
    return re.sub(r"[\s\W_]+", " ", t.lower()).strip()


def _drop_loops(segs):
    """Remove whisper's repetition hallucinations: a segment that repeats one of the
    previous three on the same side, and any run where one phrase dominates."""
    out, recent = [], []
    for s0, s1, t in segs:
        n = _norm_txt(t)
        if not n or n in recent:
            continue
        out.append((s0, s1, t))
        recent = (recent + [n])[-3:]
    if len(out) >= 6:
        from collections import Counter
        top = Counter(_norm_txt(t) for _, _, t in out).most_common(1)[0]
        if top[1] >= max(4, len(out) // 2):
            out = [x for x in out if _norm_txt(x[2]) != top[0]][:1] + [x for x in out if _norm_txt(x[2]) == top[0]][:1]
    return out


def transcribe_dir(call_dir):
    """Build <dir>/transcript.txt from mic-NNN.wav / remote-NNN.wav. Returns (path, words)."""
    call_dir = Path(call_dir)
    meta = {}
    try:
        meta = json.loads((call_dir / "call.json").read_text())
    except Exception:
        pass
    sides = {"MIC": sorted(call_dir.glob("mic-*.wav")), "REMOTE": sorted(call_dir.glob("remote-*.wav"))}
    tx = call_dir / "tx"
    tx.mkdir(exist_ok=True)
    entries = []         # (t_start, side, text)
    notes = []
    for side, files in sides.items():
        for f in files:
            m = re.search(r"-(\d+)\.wav$", f.name)
            idx = int(m.group(1)) if m else 1
            offset = (idx - 1) * CHUNK_S
            try:
                prepared, peak = _prepare_chunk(f, tx / (f.stem + ".16k.wav"))
            except Exception as e:
                notes.append(f"{f.name}: ffmpeg failed ({e})")
                continue
            if prepared is None:
                log(f"transcribe: {f.name} silent (peak {peak:.0f} dB), skipped")
                continue
            # Upload in pieces of PIECE_S seconds. A single 10-minute WAV kept
            # the Windows whisper server busy for minutes, the client gave up,
            # and uvicorn's accept loop died on the reset (WinError 64 on a
            # Windows whisper box), taking the whole STT service down. Short pieces keep each
            # request under a minute and survive a server restart mid-call.
            pieces = _split(prepared, tx, f.stem)
            got_any = False
            for pi, piece in enumerate(pieces):
                poff = offset + pi * PIECE_S
                text = ""
                spoken = _speech_seconds(piece)
                if spoken is not None and spoken < MIN_SPEECH_S:
                    log(f"transcribe: {f.name}[{pi + 1}/{len(pieces)}] only {spoken:.0f}s of speech, skipped")
                    try:
                        piece.unlink()
                    except OSError:
                        pass
                    continue
                for attempt in (1, 2, 3):
                    try:
                        t0 = time.time()
                        text, segs, lang = _whisper(piece)
                        log(f"transcribe: {f.name}[{pi + 1}/{len(pieces)}] peak {peak:.0f} dB -> "
                            f"{len(text.split())} words, lang {lang or '?'} in {time.time() - t0:.0f}s")
                        break
                    except Exception as e:
                        log(f"transcribe: {f.name}[{pi + 1}/{len(pieces)}] attempt {attempt} failed ({e})")
                        segs = []
                        time.sleep(15 * attempt)
                try:
                    piece.unlink()
                except OSError:
                    pass
                if not text:
                    continue
                got_any = True
                if segs:
                    kept = _drop_loops(segs)
                    if len(kept) < len(segs):
                        log(f"transcribe: {f.name}[{pi + 1}/{len(pieces)}] dropped {len(segs) - len(kept)} repeated segment(s)")
                    for s0, s1, t in kept:
                        entries.append((poff + s0, side, t))
                else:
                    entries.append((poff, side, text))
            try:
                prepared.unlink()
            except OSError:
                pass
            if not got_any:
                notes.append(f"{f.name}: no transcript")
    try:
        tx.rmdir()
    except OSError:
        pass
    entries.sort(key=lambda e: (e[0], e[1]))
    lines = []
    app = meta.get("app", call_dir.name.split("-")[-1])
    started = meta.get("started_local", "?")
    ended = meta.get("ended_local", "?")
    mins = meta.get("minutes", "?")
    lines.append(f"Call: {app}, {started} to {ended} ({LOCAL_TZ}), {mins} min")
    lines.append(f"Recording: {call_dir}")
    have = [s for s, fs in sides.items() if fs]
    if "REMOTE" in have and "MIC" in have:
        lines.append("Sides: MIC = your headset, REMOTE = the app's audio (process tap)")
    elif "MIC" in have:
        lines.append("Sides: MIC only - the remote side was not captured"
                     + (f" ({meta['tap_note']})" if meta.get("tap_note") else ""))
    elif "REMOTE" in have:
        lines.append("Sides: REMOTE only - the microphone was not captured")
    else:
        lines.append("Sides: nothing recorded")
    for n in notes:
        lines.append(f"Note: {n}")
    lines.append("")
    for t, side, text in entries:
        lines.append(f"[{_fmt_t(t)}] {side}: {text}")
    out = call_dir / "transcript.txt"
    out.write_text("\n".join(lines) + "\n")
    words = sum(len(t.split()) for _, _, t in entries)
    return out, words


# ----------------------------------------------------------------------------- the watch

class CallWatch:
    def __init__(self):
        self.state = "idle"          # idle | pending | active
        self.app = None
        self.pending_since = 0.0
        self.since = None            # datetime (UTC) call start
        self.last_seen = 0.0
        self.call_dir = None
        self.recorder = None
        self.restarts = 0
        self.tap_note = ""
        self.method = "?"
        self.noted_input_only = 0.0

    # -- state file
    def write_active(self, pid):
        CALL_ACTIVE.write_text(json.dumps({
            "app": self.app, "pid": pid, "since": self.since.isoformat(),
            "since_local": self.since.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
            "dir": str(self.call_dir)}))

    def clear_active(self):
        try:
            CALL_ACTIVE.unlink()
        except FileNotFoundError:
            pass

    # -- lifecycle
    def start_call(self, app, info, resume=None):
        self.app = app
        self.since = resume["since"] if resume else datetime.now(timezone.utc)
        self.call_dir = Path(resume["dir"]) if resume else (
            CALLS_DIR / f"{self.since.astimezone(LOCAL_TZ):%Y%m%d-%H%M}-{app}")
        self.call_dir.mkdir(parents=True, exist_ok=True)
        pid = (info.get("input_pids") or info.get("pids") or [0])[0]
        self.write_active(pid)
        self.state = "active"
        self.last_seen = time.time()
        start_index = (resume["next_index"] if resume else 1)
        self.recorder = Recorder(app, self.call_dir, start_index=start_index)
        stamp = self.since.astimezone(LOCAL_TZ).strftime("%H:%M")
        if resume:
            log(f"call resumed: {app} since {stamp}, dir {self.call_dir}, chunk {start_index}")
        else:
            log(f"call started: {app} (pids {info.get('input_pids')}) at {stamp} -> {self.call_dir}")
            inject(f"[call] started: {app} at {stamp}")

    def check_recorder(self):
        if self.recorder is None or self.recorder.alive():
            return
        rc = self.recorder.proc.returncode
        self.restarts += 1
        nxt = self.recorder.last_index + 1
        if self.restarts > 20:
            log(f"recorder: died again (rc={rc}); giving up on recording this call")
            self.tap_note = "recorder failed repeatedly"
            self.recorder = None
            return
        log(f"recorder: died (rc={rc}); restarting at chunk {nxt} (restart {self.restarts})")
        time.sleep(1.0)
        self.recorder = Recorder(self.app, self.call_dir, start_index=nxt, tap=self.restarts < 3)
        if self.restarts >= 3:
            self.tap_note = "tap disabled after repeated recorder failures, mic only"

    def end_call(self):
        app, call_dir, since = self.app, self.call_dir, self.since
        ended = datetime.now(timezone.utc)
        mins = max(1, int(round((ended - since).total_seconds() / 60)))
        rec = self.recorder
        mic = tap = None
        if rec is not None:
            mic, tap = rec.mic, rec.tap
            rec.stop()
        if tap is False and not self.tap_note:
            self.tap_note = "process tap not available"
        self.clear_active()
        chunks = sorted(p.name for p in call_dir.glob("*.wav"))
        try:
            (call_dir / "call.json").write_text(json.dumps({
                "app": app, "since": since.isoformat(), "ended": ended.isoformat(),
                "started_local": since.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
                "ended_local": ended.astimezone(LOCAL_TZ).strftime("%H:%M"),
                "minutes": mins, "mic": mic, "tap": tap, "tap_note": self.tap_note,
                "recorder_restarts": self.restarts, "chunks": chunks, "detection": self.method}, indent=1))
        except OSError:
            pass
        log(f"call ended: {app} after {mins} min, {len(chunks)} chunk files in {call_dir}")
        inject(f"[call] ended: {app} after {mins} min, recording at {call_dir}, transcript pending")
        self.state, self.app, self.recorder, self.call_dir, self.since = "idle", None, None, None, None
        self.restarts, self.tap_note = 0, ""
        threading.Thread(target=self._transcribe_and_notify, args=(call_dir,), daemon=True).start()

    def _transcribe_and_notify(self, call_dir):
        try:
            path, words = transcribe_dir(call_dir)
            log(f"transcript ready: {path} ({words} words)")
            inject(f"[call] transcript ready: {path} ({words} words)")
        except Exception as e:
            log(f"transcribe: failed for {call_dir}: {e}")
            inject(f"[call] transcript failed for {call_dir}: {str(e)[:120]}")

    # -- startup recovery
    def recover(self):
        kill_stale_recorders()
        if not CALL_ACTIVE.exists():
            return
        try:
            st = json.loads(CALL_ACTIVE.read_text())
            app, d = st["app"], Path(st["dir"])
            since = datetime.fromisoformat(st["since"])
        except Exception as e:
            log(f"recover: unreadable call_active.json ({e}); removing")
            self.clear_active()
            return
        idx = 0
        for p in d.glob("*-*.wav"):
            m = re.search(r"-(\d+)\.wav$", p.name)
            if m:
                idx = max(idx, int(m.group(1)))
        apps, self.method = detect()
        info = apps.get(app, {})
        if info.get("input"):
            self.start_call(app, info, resume={"since": since, "dir": d, "next_index": idx + 1})
        else:
            log(f"recover: {app} call from {since.isoformat()} is over; finalising")
            self.app, self.call_dir, self.since, self.state = app, d, since, "active"
            self.end_call()

    # -- main loop
    def run(self):
        log(f"starting; audiotap={'yes' if AUDIOTAP.exists() else 'NO (ctypes detection, no tap)'} "
            f"poll {POLL_S}s start {START_AFTER_S}s end {END_AFTER_S}s chunk {CHUNK_S}s")
        self.recover()
        errors = 0
        while True:
            t0 = time.time()
            try:
                apps, self.method = detect()
                errors = 0
            except Exception as e:
                errors += 1
                if errors in (1, 10, 100) or errors % 1000 == 0:
                    log(f"detect failed ({errors}x): {e}")
                time.sleep(POLL_S)
                continue
            live = [a for a in APPS if apps.get(a, {}).get("input")]
            calling = [a for a in live if apps[a].get("output") or not REQUIRE_OUTPUT]
            now = time.time()
            if self.state == "idle":
                if calling:
                    self.state, self.app, self.pending_since = "pending", calling[0], now
                    log(f"{self.app} opened the mic (pids {apps[self.app].get('input_pids')}); confirming")
                elif live and now - self.noted_input_only > 60:
                    self.noted_input_only = now
                    log(f"{live[0]} holds the mic without output (voice note or device test?); not a call")
            elif self.state == "pending":
                if self.app in calling:
                    if now - self.pending_since >= START_AFTER_S:
                        self.start_call(self.app, apps[self.app])
                else:
                    log(f"{self.app} stopped before {START_AFTER_S:.0f}s; not a call")
                    self.state, self.app = "idle", None
            elif self.state == "active":
                if self.app in live:
                    self.last_seen = now
                elif now - self.last_seen >= END_AFTER_S:
                    self.end_call()
                if self.state == "active":
                    self.check_recorder()
            time.sleep(max(0.2, POLL_S - (time.time() - t0)))


# ----------------------------------------------------------------------------- cli

def cmd_dry_run():
    apps, method = detect()
    live = [a for a in APPS if apps.get(a, {}).get("input") and (apps[a].get("output") or not REQUIRE_OUTPUT)]
    state = {"detection": method, "audiotap": str(AUDIOTAP) if AUDIOTAP.exists() else None,
             "call_active_file": json.loads(CALL_ACTIVE.read_text()) if CALL_ACTIVE.exists() else None,
             "live_call": live[0] if live else None,
             "call_rule": "input and output" if REQUIRE_OUTPUT else "input only", "apps": apps,
             "local_time": local_now().strftime("%Y-%m-%d %H:%M"), "tz": str(LOCAL_TZ)}
    print(json.dumps(state, indent=1))
    return 0


def cmd_test_record(seconds):
    _stdout_log[0] = True
    out = Path("/tmp/callwatch-test")
    for p in out.glob("*.wav"):
        p.unlink()
    apps, _ = detect()
    app = next((a for a in APPS if apps.get(a, {}).get("present")), None)
    log(f"test-record: {seconds}s, mic + tap of {app or 'nothing (no Teams/WhatsApp running)'} -> {out}")
    rec = Recorder(app or "none", out, seconds=seconds, tap=app is not None)
    t0 = time.time()
    while rec.alive() and time.time() - t0 < seconds + 30:
        time.sleep(0.5)
    rec.stop()
    ok = True
    for p in sorted(out.glob("*.wav")):
        r = subprocess.run([FFPROBE, "-hide_banner", "-show_entries", "format=duration,size", "-of", "json", str(p)],
                           capture_output=True, text=True)
        try:
            fmt = json.loads(r.stdout)["format"]
            dur, size = float(fmt.get("duration", 0)), int(fmt.get("size", 0))
        except Exception:
            dur, size = 0.0, 0
        peak = _peak_db(p)
        log(f"test-record: {p.name}: {dur:.1f}s, {size} bytes, peak {peak:.1f} dB")
        if size <= 44:
            ok = False
    if not any(out.glob("mic-*.wav")):
        log("test-record: no mic file produced (microphone permission for audiotap?)")
        ok = False
    if app and not any(out.glob("remote-*.wav")):
        log(f"test-record: no remote file - expected when {app} is not playing audio "
            f"(the tap aggregate only runs while the app outputs sound)")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="Teams/WhatsApp call watch")
    ap.add_argument("--dry-run", "--once", action="store_true", help="print detection state and exit")
    ap.add_argument("--test-record", type=int, metavar="SECONDS", help="record N seconds to /tmp/callwatch-test")
    ap.add_argument("--transcribe", metavar="DIR", help="build DIR/transcript.txt and exit")
    a = ap.parse_args()
    if a.dry_run:
        return cmd_dry_run()
    if a.test_record:
        return cmd_test_record(a.test_record)
    if a.transcribe:
        _stdout_log[0] = True
        path, words = transcribe_dir(a.transcribe)
        print(f"{path} ({words} words)")
        return 0
    CALLS_DIR.mkdir(parents=True, exist_ok=True)
    w = CallWatch()

    def _term(signum, frame):
        log(f"signal {signum}: stopping")
        if w.recorder is not None:
            w.recorder.stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    try:
        w.run()
    except SystemExit:
        raise
    except Exception as e:
        log(f"FATAL: {e!r}")
        if w.recorder is not None:
            w.recorder.stop()
        raise


if __name__ == "__main__":
    sys.exit(main())
