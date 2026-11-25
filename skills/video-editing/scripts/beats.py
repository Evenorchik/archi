#!/usr/bin/env python3
"""Music grid for beat-synced cutting: tempo, beat times, per-beat strength, strong onsets.

python beats.py <audio-or-video>

"low" is onset strength below 150 Hz (the kick): beats with a high "low" value are the bar
accents a cut should land on. "strong onsets" lists one-off hits (good for a logo / final shot).
"""
import sys, numpy as np, librosa

y, sr = librosa.load(sys.argv[1], sr=22050, mono=True)
hop = 256
oenv = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
tempo, beats = librosa.beat.beat_track(onset_envelope=oenv, sr=sr, hop_length=hop, units="time")
print("tempo", np.round(tempo, 2), "dur", round(len(y) / sr, 2))
print("beats", np.round(beats, 3).tolist())
# beat strength: low-band (kick) and full
S = np.abs(librosa.stft(y, n_fft=2048, hop_length=hop))
f = librosa.fft_frequencies(sr=sr, n_fft=2048)
low = librosa.onset.onset_strength(S=librosa.amplitude_to_db(S[f < 150]), sr=sr, hop_length=hop)
t = librosa.frames_to_time(np.arange(len(oenv)), sr=sr, hop_length=hop)


def at(env, bt):
    i = np.searchsorted(t, bt)
    return float(env[max(0, i - 3):i + 4].max())


for b in beats:
    print(f"{b:6.2f} full {at(oenv, b):5.1f} low {at(low, b):5.1f}")
on = librosa.onset.onset_detect(onset_envelope=oenv, sr=sr, hop_length=hop, units="time", delta=0.25)
print("strong onsets", [(round(float(o), 2), round(at(oenv, o), 1)) for o in on
                        if at(oenv, o) > np.percentile(oenv, 97)])
rms = librosa.feature.rms(y=y, frame_length=4096, hop_length=sr // 2)[0]
print("rms/0.5s", np.round(20 * np.log10(rms + 1e-6), 0).astype(int).tolist())
