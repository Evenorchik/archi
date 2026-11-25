import sys, numpy as np, librosa
y, sr = librosa.load(sys.argv[1], sr=22050, mono=True)
hop = 220  # ~10 ms
S = np.abs(librosa.stft(y, n_fft=2048, hop_length=hop))
f = librosa.fft_frequencies(sr=sr, n_fft=2048)
t = librosa.frames_to_time(np.arange(S.shape[1]), sr=sr, hop_length=hop)
def flux(lo, hi):
    b = librosa.amplitude_to_db(S[(f >= lo) & (f < hi)], ref=1.0, top_db=None)
    d = np.maximum(np.diff(b, axis=1, prepend=b[:, :1]), 0).mean(axis=0)
    return d
bands = {"low<150": flux(0, 150), "mid150-2k": flux(150, 2000), "hi2k+": flux(2000, 11000)}
full = flux(0, 11000)
# tempo candidates by autocorrelation of the full flux
ac = librosa.autocorrelate(full - full.mean(), max_size=int(4 / (hop / sr)))
lags = np.arange(len(ac)) * hop / sr
cand = [(round(float(lags[i]), 3), round(float(ac[i] / ac[0]), 2)) for i in range(20, len(ac) - 1)
        if ac[i] > ac[i - 1] and ac[i] > ac[i + 1] and ac[i] / ac[0] > 0.08]
print("autocorr period peaks (s, strength):", cand[:25])
# onsets: peaks of full flux
pk = librosa.util.peak_pick(full, pre_max=8, post_max=8, pre_avg=30, post_avg=30, delta=0.6, wait=12)
thr = np.percentile(full, 90)
print("onsets  time  full  low  mid  hi")
for i in pk:
    w = slice(max(0, i - 3), i + 4)
    print(f"{t[i]:6.2f} {full[w].max():5.1f} {bands['low<150'][w].max():5.1f} {bands['mid150-2k'][w].max():5.1f} {bands['hi2k+'][w].max():5.1f}")
# band energy per 0.5 s
def en(lo, hi):
    e = (S[(f >= lo) & (f < hi)] ** 2).sum(axis=0)
    n = int(0.5 / (hop / sr))
    return [int(round(10 * np.log10(e[i:i + n].mean() + 1e-9))) for i in range(0, len(e), n)]
for name, (lo, hi) in {"<200": (0, 200), "200-2k": (200, 2000), "2k-5k": (2000, 5000), "5k+": (5000, 11000)}.items():
    print(f"energy/0.5s {name:7s}", en(lo, hi))
