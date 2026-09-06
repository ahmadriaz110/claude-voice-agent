#!/usr/bin/env python3
"""Build voiceprints from enrolment_v2.wav: ECAPA-TDNN (speechbrain) and
resemblyzer, averaged over voiced 3-second windows. Prints self-consistency."""
import wave, numpy as np
from pathlib import Path
from scipy import signal
D = Path.home() / ".voicemode/indicator"
with wave.open(str(D / "enrolment_v2.wav")) as w:
    rate = w.getframerate(); pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
x = pcm.astype(np.float32) / 32768.0
x16 = signal.resample(x, int(len(x) * 16000 / rate)).astype(np.float32)
win, hop = 3 * 16000, int(1.5 * 16000)
chunks = [x16[i:i + win] for i in range(0, len(x16) - win, hop)]
rms = np.array([float(np.sqrt(np.mean(c * c))) for c in chunks])
voiced = [c for c, r in zip(chunks, rms) if r >= np.percentile(rms, 30)]
print(f"{len(chunks)} windows, {len(voiced)} voiced")
# ECAPA
import torch
from speechbrain.inference.speaker import EncoderClassifier
enc = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(D / "ecapa"), run_opts={"device": "cpu"})
embs = []
with torch.no_grad():
    for c in voiced:
        e = enc.encode_batch(torch.tensor(c)[None]).squeeze().numpy(); embs.append(e / (np.linalg.norm(e) + 1e-9))
E = np.stack(embs); mean = E.mean(0); mean /= np.linalg.norm(mean)
sims = E @ mean
np.save(D / "voiceprint_ecapa.npy", mean.astype(np.float32))
print(f"ECAPA: dim {mean.shape[0]}, window-vs-print similarity min {sims.min():.3f} median {np.median(sims):.3f} max {sims.max():.3f}")
# resemblyzer, for comparison and as fallback
from resemblyzer import VoiceEncoder
r = VoiceEncoder("cpu")
R = np.stack([r.embed_utterance(c) for c in voiced]); rm = R.mean(0); rm /= np.linalg.norm(rm)
np.save(D / "voiceprint_v2.npy", rm.astype(np.float32))
rs = R @ rm
print(f"resemblyzer: window-vs-print similarity min {rs.min():.3f} median {np.median(rs):.3f} max {rs.max():.3f}")
