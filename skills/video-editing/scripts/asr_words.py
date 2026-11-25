#!/usr/bin/env python3
"""Word-timestamp ASR (faster-whisper) -> JSON used by reel_build.py.

Output: {"language", "prob", "segments": [{"start", "end", "text", "words": [{"w", "s", "e"}]}]}
Times are in SOURCE seconds even when --from/--to select a fragment.

python asr_words.py <media> <out.json> [--lang uk] [--from S] [--to S] [--model large-v3]

Audio is decoded by ffmpeg and handed to the model as a numpy array: passing a file path
goes through PyAV inside faster-whisper, which is the part that breaks on version mismatches.
"""
import argparse, json, subprocess

import numpy as np
from faster_whisper import WhisperModel


def load(path, a=None, b=None):
    cmd = ["ffmpeg", "-v", "error"]
    if a is not None:
        cmd += ["-ss", str(a)]
    if b is not None:
        cmd += ["-to", str(b)]
    cmd += ["-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("out")
    ap.add_argument("--lang", default=None)
    ap.add_argument("--from", dest="a", type=float, default=None)
    ap.add_argument("--to", dest="b", type=float, default=None)
    ap.add_argument("--model", default="large-v3")
    args = ap.parse_args()

    off = args.a or 0.0
    audio = load(args.media, args.a, args.b)
    model = WhisperModel(args.model, device="cuda", compute_type="float16")
    segs, info = model.transcribe(audio, language=args.lang, word_timestamps=True,
                                  vad_filter=False, condition_on_previous_text=False)
    out = {"language": info.language, "prob": info.language_probability, "segments": []}
    for s in segs:
        out["segments"].append({
            "start": round(s.start + off, 2), "end": round(s.end + off, 2), "text": s.text,
            "words": [{"w": w.word, "s": round(w.start + off, 2), "e": round(w.end + off, 2)}
                      for w in (s.words or [])],
        })
        print(f"{s.start + off:7.2f}-{s.end + off:7.2f} {s.text}")
    json.dump(out, open(args.out, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
