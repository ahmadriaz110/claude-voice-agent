#!/usr/bin/env python3
"""Score known-genuine and known-impostor audio against the ECAPA print and
suggest a threshold. Genuine: enrolment windows. Impostors: the Kokoro sample
(our own TTS) and the rejected wake clips from tonight (room / video voices)."""
import glob, wave, numpy as np, torch
from pathlib import Path
from scipy import signal
from speechbrain.inference.speaker import EncoderClassifier
D = Path.home() / ".voicemode/indicator"
enc = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(D / "ecapa"), run_opts={"device": "cpu"})
vp = np.load(D / "voiceprint_ecapa.npy")
def load16(path):
    with wave.open(str(path)) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1: pcm = pcm.reshape(-1, ch)[:, 0]
    x = pcm.astype(np.float32) / 32768.0
    return signal.resample(x, int(len(x) * 16000 / rate)).astype(np.float32) if rate != 16000 else x
def score(x16):
    if len(x16) < 8000: return None
    with torch.no_grad():
        e = enc.encode_batch(torch.tensor(x16)[None]).squeeze().numpy()
    e /= np.linalg.norm(e) + 1e-9
    return float(vp @ e)
def windows(x16, win=1.7):
    n = int(win * 16000)
    return [x16[i:i+n] for i in range(0, max(1, len(x16) - n), n)]
# genuine: enrolment (1.7 s windows, the same length the daemon verifies on)
gen = [s for s in (score(w) for w in windows(load16(D / "enrolment_v2.wav"))) if s is not None]
# impostor 1: our TTS
tts = [s for s in (score(w) for w in windows(load16(D / "tts_sample.wav"))) if s is not None]
# impostor 2: rejected room clips (24 kHz, various lengths)
rej = []
for f in sorted(glob.glob(str(D / "rejected" / "rej_*.wav")))[-60:]:
    s = score(load16(f))
    if s is not None: rej.append(s)
def stats(name, v):
    v = np.array(v); print(f"{name:22} n={len(v):3}  min {v.min():.3f}  p10 {np.percentile(v,10):.3f}  median {np.median(v):.3f}  p90 {np.percentile(v,90):.3f}  max {v.max():.3f}")
stats("genuine (enrolment)", gen); stats("impostor: Kokoro TTS", tts)
if rej: stats("impostor: room clips", rej)
imp_max = max(max(tts), max(rej) if rej else -1)
gen_p10 = float(np.percentile(gen, 10))
print(f"\nhighest impostor {imp_max:.3f} | genuine 10th percentile {gen_p10:.3f}")
thr = round((imp_max + gen_p10) / 2, 2) if gen_p10 > imp_max else round(imp_max + 0.03, 2)
print(f"suggested BARGEIN_SPEAKER_THRESHOLD_ECAPA = {thr}  (separation {'OK' if gen_p10 > imp_max else 'OVERLAP - tighten with more data'})")
