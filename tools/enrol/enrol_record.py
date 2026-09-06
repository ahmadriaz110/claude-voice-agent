#!/usr/bin/env python3
"""Record an enrolment reading from the headset mic while the barge-in daemon
stays off the device (it honours vm_recording.flag). Usage: enrol_record.py [seconds]"""
import os, sys, time, wave
import numpy as np, sounddevice as sd
from pathlib import Path
RATE = 24000
secs = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0
flag = Path.home() / ".voicemode/indicator/vm_recording.flag"
out = Path.home() / ".voicemode/indicator/enrolment_v2.wav"
dev = next((i for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0 and os.environ.get("BARGEIN_MIC_NAME", "USB") in d["name"]), None)
flag.write_text(str(time.time())); time.sleep(0.6)          # daemon releases the mic
try:
    for n in (3, 2, 1):
        print(f"starting in {n}...", flush=True); time.sleep(1)
    print("RECORDING", flush=True)
    audio = sd.rec(int(secs * RATE), samplerate=RATE, channels=1, dtype="int16", device=dev)
    sd.wait()
finally:
    flag.unlink(missing_ok=True)
with wave.open(str(out), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(audio.tobytes())
a = audio[:, 0].astype(np.float32); rms = float(np.sqrt(np.mean(a * a)))
print(f"saved {out} ({secs:.0f}s, overall rms {rms:.0f})", flush=True)
