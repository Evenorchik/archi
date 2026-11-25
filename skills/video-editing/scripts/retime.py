#!/usr/bin/env python3
"""Re-cut an existing edit to the music grid without touching the audio.

Each source shot is retimed so its cut lands on a chosen beat. Timing is done by
rewriting frame timestamps (setpts) and letting ffmpeg's motion-compensated
interpolation (minterpolate, honours variable pts) synthesize the output frames.
Shots with spare footage get an ease-out speed ramp: fast entry on the beat,
settling into a calm drift.

python3 retime.py src.mp4 out.mp4 [--plan]      (--plan prints speeds, renders nothing)

Fill FR and SHOTS for the project first. All positions are FRAME numbers:
src frames from the detected cuts, out end frame = round(target_beat_seconds * FR).
"""
import subprocess, sys
from concurrent.futures import ThreadPoolExecutor

FR = 30000 / 1001          # source frame rate (ffprobe r_frame_rate)
FR_S = "30000/1001"        # the same as an ffmpeg rational
# (name, src first frame, src end frame (exclusive), out end frame, ramp a, const speed or None)
# ramp a: out_u = (1-a)*s + a*s^2  ->  entry speed avg/(1-a), exit speed avg/(1+a); 0 = even speed
# const speed: use only the first round(n*speed)+1 source frames (e.g. to place an in-shot
# event such as a logo fade on a hit); None = use the whole shot.
SHOTS = [
    # ("field",      0,  47,  53, 0.00, None),
    # ("wall",      47, 114, 104, 0.40, None),
    # ("final",    717, 840, 840, 0.00, 0.93),
]
MI = f"minterpolate=fps={FR_S}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1:scd=none"


def plan():
    jobs, start = [], 0
    for i, (name, f0, f1, end, a, speed) in enumerate(SHOTS):
        n = end - start
        used = f1 - f0 if speed is None else min(f1 - f0, round(n * speed) + 1)
        S, D = (used - 1) / FR, n / FR      # source span -> output span (seconds)
        avg = S / D
        print(f"{name:9s} out {start / FR:6.2f}-{end / FR:6.2f} ({D:.2f}s) src {f0}-{f0 + used}"
              f" avg x{avg:.2f} entry x{avg / (1 - a):.2f} exit x{avg / (1 + a):.2f}")
        vf = (f"trim=start_frame={f0}:end_frame={f0 + used},setpts=PTS-STARTPTS,"
              f"setpts='{D:.5f}*({1 - a}*(T/{S:.5f})+{a}*pow(T/{S:.5f},2))/TB',"
              f"{MI},tpad=stop_mode=clone:stop_duration=1,trim=end_frame={n},setpts=PTS-STARTPTS")
        jobs.append((i, vf, n))
        start = end
    return jobs


def main(src, out):
    if not SHOTS:
        sys.exit("fill SHOTS first")
    jobs = plan()
    if "--plan" in sys.argv:
        return

    def run(job):
        i, vf, n = job
        # -frames:v n: minterpolate emits a couple of extra frames per shot, which would
        # push every later cut off its beat
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-an", "-vf", vf, "-fps_mode", "cfr",
                        "-r", FR_S, "-frames:v", str(n), "-c:v", "libx264", "-crf", "12", "-preset", "medium",
                        "-pix_fmt", "yuv420p", f"shot_{i:02d}.mkv"], check=True)

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(run, jobs))
    open("shots.txt", "w").write("".join(f"file 'shot_{i:02d}.mkv'\n" for i, _, _ in jobs))
    # video from the retimed shots, audio stream copied from the source untouched
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "shots.txt", "-i", src,
                    "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-crf", "16", "-preset", "slow",
                    "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", out], check=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
