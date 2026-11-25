#!/usr/bin/env python3
"""Reel build: talking-head source -> 1080x1920 reel. Template: fill the PROJECT DATA block.

Stage 1 (base): cut kept source ranges, punch-in zooms, concat -> base.mkv (voice only).
Stage 2 (final): Python renders an RGBA overlay stream (captions, hook title, topic
cards, animated emoji, progress bar) piped into ffmpeg over base.mkv; voice is
cleaned + normalized, SFX mixed at overlay events.

Project dir layout: src.mp4, words.json (ASR of the source), words_base.json (ASR of
base.mkv, made after `base`), fonts/, emoji/<codepoints>.gif, sfx/.

Run from the project dir: python reel_build.py base|check|preview|final
"""
import json, math, subprocess, sys
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H, FPS = 1080, 1920, 30
SRC = "src.mp4"
SFX = "sfx"  # needs sparkle.mp3, pop.mp3, whoosh-short.mp3: any short sounds you have the right to use
F_BLACK = "fonts/Montserrat-Black.ttf"       # any heavy face that covers the caption language
F_XB = "fonts/Montserrat-ExtraBold.ttf"
F_SB = "fonts/Montserrat-SemiBold.ttf"
ACCENT = (42, 171, 238, 255)   # progress bar, hook pill, card label; match the topic/brand
YELLOW = (255, 212, 59, 255)   # active caption word, second hook pill

# ------------------------------------------------------------------ PROJECT DATA
# Kept source ranges (seconds), in order. The first range is the hook: the title covers
# it and captions start after it. Write next to the list WHY each gap was dropped
# (retake / thinking pause / filler) so a later fix does not resurrect it.
KEEP = [
    # (5.95, 9.05),
    # (11.27, 22.72), (23.05, 28.78),
]
ZOOMS = [1.0, 1.18, 1.06, 1.3]
FACE_Y = 0.30  # face height as a fraction of the frame; zoom keeps it in place

HOOK_L1 = "HOOK LINE ONE"      # big title from the first frame, doubles as the cover
HOOK_L2 = "HOOK LINE TWO"

# ASR spelling slips, confirmed by re-ASR of the snippet (the audio is right, the text is not)
FIX = {}

EMOJI = [  # (source time, emoji/<file>.gif without extension, side "L"/"R")
    # (24.30, "2615", "L"), (27.80, "1f525", "R"),
]
STILL = set()  # codes whose animation leaves its own frame: shown as a still, else it reads as a glitch
CARDS = [  # (from src, to src, label, title); both times must fall inside KEEP ranges
    # (46.05, 80.50, "ITEM 1 of 3", "Name of the thing"),
]
CTA_LABEL = None    # label of the closing call-to-action card in CARDS, gets the emoji badge
CTA_EMOJI = None    # emoji code for that badge (file must exist in emoji/)
# ------------------------------------------------------------------------------


def words():
    d = json.load(open("words.json"))
    return [w for s in d["segments"] for w in s["words"]]


def pieces():
    """Split KEEP ranges into shots of ~3-5 s at word gaps, assign zooms."""
    ws = words()
    out, zi = [], 0
    for a, b in KEEP:
        cuts = [a]
        inner = [w for w in ws if a + 0.3 < w["s"] < b - 1.2]
        t = a
        for w in inner:
            if w["s"] - t >= 3.6 and (w["s"] - t >= 5.0 or w is not inner[0]):
                # cut at the start of a word that follows punctuation if possible
                prev = ws[ws.index(w) - 1]
                if prev["w"].strip()[-1:] in ",.?!—–" or w["s"] - t >= 5.0:
                    cut = round(w["s"] - 0.04, 3)
                    cuts.append(cut)
                    t = cut
        cuts.append(b)
        for i in range(len(cuts) - 1):
            out.append({"s": cuts[i], "e": cuts[i + 1], "z": ZOOMS[zi % len(ZOOMS)],
                        "hard": i == 0})
            zi += 1
    return out


def src_to_out(t, ps=None):
    ps = ps or PIECES
    acc = 0.0
    for p in ps:
        if p["s"] <= t < p["e"]:
            return acc + (t - p["s"])
        acc += p["e"] - p["s"]
    return None


if not KEEP:
    sys.exit("fill KEEP (and the rest of PROJECT DATA) first")
PIECES = pieces()
DUR = sum(p["e"] - p["s"] for p in PIECES)


def build_base():
    """Encode each shot on its own (bounded memory), then concat losslessly."""
    import os
    os.makedirs("shots", exist_ok=True)
    lst = []
    for i, p in enumerate(PIECES):
        z = p["z"]
        sw, sh = int(W * z) // 2 * 2, int(H * z) // 2 * 2
        x, y = (sw - W) // 2, int(sh * FACE_Y - H * FACE_Y)
        d = p["e"] - p["s"]
        af = "anull"
        if p["hard"]:  # tiny fades at real cuts: no clicks
            af = f"afade=t=in:d=0.015,afade=t=out:st={d - 0.02:.3f}:d=0.02"
        vf = (f"scale={sw}:{sh}:flags=lanczos,crop={W}:{H}:{x}:{y},setsar=1,fps={FPS},"
              "eq=contrast=1.05:saturation=1.08,unsharp=5:5:0.4")
        out = f"shots/{i:02d}.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{p['s']:.3f}", "-i", SRC,
                        "-t", f"{d:.3f}", "-vf", vf, "-af", af, "-c:v", "libx264", "-crf", "14",
                        "-preset", "fast", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le",
                        "-ar", "48000", "-ac", "2", out], check=True)
        lst.append(f"file '{out}'")
    open("shots.txt", "w").write("\n".join(lst) + "\n")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", "shots.txt",
                    "-c", "copy", "base.mkv"], check=True)
    print("base", round(DUR, 2), "s,", len(PIECES), "shots")


# ---------------------------------------------------------------- overlay model

def ease_out_back(x, k=1.7):
    x = min(max(x, 0.0), 1.0) - 1
    return 1 + (k + 1) * x ** 3 + k * x ** 2


def ease_out(x):
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def caption_pages():
    """Pages of <= 3 words / 16 chars, from ASR of the CUT audio (output time)."""
    d = json.load(open("words_base.json"))
    ws = []
    for w in (w for s in d["segments"] for w in s["words"]):
        txt = w["w"].replace("«", "").replace("»", "").strip().strip("–—").strip()
        if not txt:
            continue
        txt = FIX.get(txt, txt)
        if txt.startswith("-") and ws:
            ws[-1]["w"] += txt
            ws[-1]["e"] = w["e"]
            continue
        ws.append({"t": w["s"], "e": w["e"], "w": txt})
    hook_end = KEEP[0][1] - KEEP[0][0]
    ws = [w for w in ws if w["t"] >= hook_end - 0.05]  # hook is carried by the title
    for i in range(len(ws) - 1):
        ws[i]["e"] = min(max(ws[i]["e"], ws[i]["t"] + 0.12), ws[i + 1]["t"])
    pages, cur = [], []
    for w in ws:
        clean = w["w"].rstrip(",.?!")
        long = sum(len(x["w"]) for x in cur) + len(clean) > 16
        gap = cur and w["t"] - cur[-1]["e"] > 0.45
        if cur and (len(cur) >= 3 or long or gap):
            pages.append(cur)
            cur = []
        cur.append(w)
        if w["w"][-1] in ",.?!":
            pages.append(cur)
            cur = []
    if cur:
        pages.append(cur)
    out = []
    for i, pg in enumerate(pages):
        end = pg[-1]["e"] + 0.25
        if i + 1 < len(pages):
            end = min(end, pages[i + 1][0]["t"])
        out.append({"t": pg[0]["t"], "e": end, "words": pg})
    return out


def at(src_t):
    return src_to_out(src_t)


@lru_cache(None)
def font(path, size):
    return ImageFont.truetype(path, size)


@lru_cache(None)
def emoji_frames(code, size=250):
    im = Image.open(f"emoji/{code}.gif")
    frames, durs = [], []
    try:
        while True:
            frames.append(im.convert("RGBA").resize((size, size), Image.LANCZOS))
            durs.append(max(im.info.get("duration", 40), 20) / 1000)
            im.seek(im.tell() + 1)
    except EOFError:
        pass
    return frames, durs


def emoji_at(code, t):
    frames, durs = emoji_frames(code)
    if code in STILL:
        return frames[0]
    t %= sum(durs)
    for f, d in zip(frames, durs):
        if t < d:
            return f
        t -= d
    return frames[-1]


def text_img(txt, fnt, fill, stroke=10, shadow=True):
    l, t, r, b = fnt.getbbox(txt, stroke_width=stroke)
    pad = 24
    im = Image.new("RGBA", (r - l + pad * 2, b - t + pad * 2), (0, 0, 0, 0))
    if shadow:
        sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).text((pad - l + 4, pad - t + 8), txt, font=fnt, fill=(0, 0, 0, 150),
                                stroke_width=stroke, stroke_fill=(0, 0, 0, 150))
        im = Image.alpha_composite(im, sh.filter(ImageFilter.GaussianBlur(8)))
    ImageDraw.Draw(im).text((pad - l, pad - t), txt, font=fnt, fill=fill,
                            stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    return im


@lru_cache(None)
def page_img(key, active):
    """Render one caption page with word `active` highlighted (cached)."""
    words_ = key.split("\u0001")
    fnt = font(F_BLACK, 82)
    imgs = []
    for i, w in enumerate(words_):
        imgs.append(text_img(w.upper(), fnt, YELLOW if i == active else (255, 255, 255, 255)))
    space = -22
    lines, cur, cw = [], [], 0
    for im in imgs:  # wrap at ~940 px
        if cur and cw + im.width + space > 940:
            lines.append(cur)
            cur, cw = [], 0
        cur.append(im)
        cw += im.width + space
    lines.append(cur)
    lh = max(im.height for im in imgs) - 34
    tw = max(sum(i.width for i in ln) + space * (len(ln) - 1) for ln in lines)
    out = Image.new("RGBA", (tw, lh * len(lines) + 34), (0, 0, 0, 0))
    for li, ln in enumerate(lines):
        x = (tw - (sum(i.width for i in ln) + space * (len(ln) - 1))) // 2
        for im in ln:
            out.alpha_composite(im, (x, li * lh))
            x += im.width + space
    return out


def rounded(size, radius, fill):
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=fill)
    return im


@lru_cache(None)
def topic_badge(d=104):
    """Round badge on the left of a card. Draw the topic's own mark here (this one is a paper plane)."""
    im = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.ellipse((0, 0, d - 1, d - 1), fill=ACCENT)
    s = d / 100
    dr.polygon([(22 * s, 49 * s), (76 * s, 28 * s), (66 * s, 75 * s), (47 * s, 61 * s),
                (38 * s, 70 * s), (37 * s, 56 * s)], fill=(255, 255, 255, 255))
    dr.polygon([(37 * s, 56 * s), (68 * s, 35 * s), (47 * s, 61 * s)], fill=(200, 230, 248, 255))
    return im


@lru_cache(None)
def card_img(label, title):
    tf = font(F_BLACK, 50)
    while tf.getlength(title) > 700:
        tf = font(F_BLACK, tf.size - 2)
    lf = font(F_SB, 30)
    w = int(max(tf.getlength(title), lf.getlength(label)) + 104 + 100)
    h = 160
    shadow = rounded((w + 40, h + 40), 44, (0, 0, 0, 90)).filter(ImageFilter.GaussianBlur(14))
    im = Image.new("RGBA", (w + 40, h + 40), (0, 0, 0, 0))
    im.alpha_composite(shadow, (0, 10))
    im.alpha_composite(rounded((w, h), 36, (255, 255, 255, 245)), (20, 14))
    badge = topic_badge()
    if CTA_LABEL and CTA_EMOJI and label == CTA_LABEL:
        badge = Image.new("RGBA", (104, 104), (0, 0, 0, 0))
        ImageDraw.Draw(badge).ellipse((0, 0, 103, 103), fill=YELLOW)
        badge.alpha_composite(emoji_frames(CTA_EMOJI, 72)[0][0], (16, 16))
    im.alpha_composite(badge, (20 + 28, 14 + (h - 104) // 2))
    dr = ImageDraw.Draw(im)
    dr.text((20 + 28 + 104 + 26, 14 + 30), label, font=lf, fill=(42, 140, 200, 255))
    dr.text((20 + 28 + 104 + 26, 14 + 68), title, font=tf, fill=(20, 20, 24, 255))
    return im


@lru_cache(None)
def hook_img():
    fnt = font(F_BLACK, 74)
    l1 = text_img(HOOK_L1, fnt, (255, 255, 255, 255), stroke=0, shadow=False)
    l2 = text_img(HOOK_L2, fnt, (20, 20, 24, 255), stroke=0, shadow=False)
    p1 = rounded((l1.width + 30, l1.height - 6), 30, ACCENT)
    p1.alpha_composite(l1, (15, -3))
    p2 = rounded((l2.width + 30, l2.height - 6), 30, YELLOW)
    p2.alpha_composite(l2, (15, -3))
    w = max(p1.width, p2.width)
    r2 = p2.rotate(-2.5, resample=Image.BICUBIC, expand=True)
    w = max(w, r2.width)
    im = Image.new("RGBA", (w, p1.height + r2.height), (0, 0, 0, 0))
    im.alpha_composite(p1, ((w - p1.width) // 2, 0))
    im.alpha_composite(r2, ((w - r2.width) // 2, p1.height - 14))
    return im


def paste_scaled(canvas, im, cx, cy, scale, alpha=1.0, rot=0.0):
    if scale <= 0.01 or alpha <= 0.01:
        return
    if abs(scale - 1) > 1e-3:
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.BILINEAR)
    if rot:
        im = im.rotate(rot, resample=Image.BICUBIC, expand=True)
    if alpha < 1:
        a = im.getchannel("A").point(lambda v: int(v * alpha))
        im = im.copy()
        im.putalpha(a)
    canvas.alpha_composite(im, (int(cx - im.width / 2), int(cy - im.height / 2)))


def events():
    hook_dur = KEEP[0][1] - KEEP[0][0]
    emo = [(at(s), c, side) for s, c, side in EMOJI if at(s) is not None]
    cards = [(at(a + 0.01), at(b - 0.01) + 0.01, lab, tit) for a, b, lab, tit in CARDS]
    return hook_dur, emo, cards


def render_frame(t, pages, hook_dur, emo, cards):
    cv = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # progress bar
    ImageDraw.Draw(cv).rectangle((0, 0, int(W * t / DUR), 9), fill=ACCENT)
    # hook title (body area, big); gone before the first caption page
    if t < hook_dur - 0.04:
        k = ease_out_back(t / 0.45)
        out = 1 - ease_out((t - hook_dur + 0.2) / 0.16) if t > hook_dur - 0.2 else 1
        paste_scaled(cv, hook_img(), W / 2, 1180, (0.88 + 0.12 * k) * (0.6 + 0.4 * out), alpha=out)
    # topic cards (upper area)
    for a, b, lab, tit in cards:
        if a - 0.01 <= t <= b + 0.3:
            im = card_img(lab, tit)
            k = ease_out((t - a) / 0.4)
            o = 1 - ease_out((t - b) / 0.3) if t > b else 1
            paste_scaled(cv, im, W / 2, 300 - 60 * (1 - k), 0.92 + 0.08 * k, alpha=k * o)
    # emoji stickers (beside the head)
    for a, code, side in emo:
        dt = t - a
        if -0.01 <= dt <= 1.9:
            k = ease_out_back(dt / 0.35, 2.6)
            o = 1 - ease_out((dt - 1.6) / 0.3) if dt > 1.6 else 1
            cx = 860 if side == "R" else 220
            cy = 640 + 10 * math.sin(dt * 5)
            rot = (8 if side == "R" else -8) * math.sin(dt * 3.2)
            paste_scaled(cv, emoji_at(code, dt), cx, cy, max(k, 0) * (0.85 + 0.15 * o), alpha=o, rot=rot)
    # captions (chest area, clear of the app UI at the bottom)
    for pg in pages:
        if pg["t"] <= t < pg["e"]:
            active = 0
            for i, w in enumerate(pg["words"]):
                if t >= w["t"]:
                    active = i
            key = "\u0001".join(w["w"] for w in pg["words"])
            im = page_img(key, active)
            k = ease_out_back((t - pg["t"]) / 0.16, 2.0)
            paste_scaled(cv, im, W / 2, 1290, 0.82 + 0.18 * k)
            break
    return cv


def build_final(preview=False):
    pages = caption_pages()
    hook_dur, emo, cards = events()
    sfx = [(0.05, "sparkle.mp3", 0.35)]
    sfx += [(a, "pop.mp3", 0.30) for a, _, _ in emo]
    sfx += [(a, "whoosh-short.mp3", 0.30) for a, _, _, _ in cards]
    ins = ["-i", "base.mkv", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "pipe:0"]
    for _, f, _ in sfx:
        ins += ["-i", f"{SFX}/{f}"]
    fc = ["[0:v][1:v]overlay=0:0:format=auto,format=yuv420p[v]",
          "[0:a]highpass=f=80,acompressor=threshold=-20dB:ratio=3:attack=5:release=120:makeup=2,"
          "loudnorm=I=-14:TP=-1.5:LRA=9,aresample=48000[voice]"]
    labels = []
    for i, (t, _, vol) in enumerate(sfx):
        ms = int(t * 1000)
        fc.append(f"[{i + 2}:a]aresample=48000,volume={vol},adelay={ms}|{ms}[s{i}]")
        labels.append(f"[s{i}]")
    fc.append("[voice]" + "".join(labels) + f"amix=inputs={len(labels) + 1}:normalize=0:duration=first,"
              "alimiter=limit=0.89[a]")
    out = "preview.mp4" if preview else "reel_final.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y", *ins, "-filter_complex", ";".join(fc),
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "18", "-preset", "slow",
           "-profile:v", "high", "-r", str(FPS), "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", "-t", f"{DUR:.3f}", out]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    n = int(DUR * FPS)
    for i in range(n):
        p.stdin.write(render_frame(i / FPS, pages, hook_dur, emo, cards).tobytes())
    p.stdin.close()
    if p.wait():
        sys.exit("ffmpeg failed")
    print(out, n, "frames")


def check(ts=None):
    """Print caption pages + event times, render 12 stills with overlays -> stills.jpg.

    Pass explicit output times (python reel_build.py check 0.5 3.4 ...) to look at the
    frames where elements appear or meet: hook end, each card start, each emoji.
    """
    pages = caption_pages()
    print("duration", round(DUR, 2), "shots", len(PIECES))
    for p in pages[:400]:
        print(f"{p['t']:6.2f}-{p['e']:6.2f}", " ".join(w["w"] for w in p["words"]))
    hook_dur, emo, cards = events()
    print("emoji", [(round(a, 2), c) for a, c, _ in emo])
    print("cards", [(round(a, 2), round(b, 2), t) for a, b, _, t in cards])
    import glob, os
    for old in glob.glob("st_[0-9][0-9].png"):  # stale stills would be tiled into the sheet
        os.remove(old)
    ts = (ts or [DUR * (i + 0.5) / 12 for i in range(12)])[:12]
    for i, t in enumerate(ts):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", "base.mkv", "-frames:v", "1",
                        f"st_{i:02d}.png"], check=True)
        bg = Image.open(f"st_{i:02d}.png").convert("RGBA")
        bg.alpha_composite(render_frame(t, pages, hook_dur, emo, cards))
        bg.convert("RGB").resize((360, 640)).save(f"st_{i:02d}.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", "st_%02d.png", "-vf", "tile=6x2",
                    "-frames:v", "1", "stills.jpg"], check=True)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "check":
        check([float(x) for x in sys.argv[2:]] or None)
    else:
        {"base": build_base, "final": build_final,
         "preview": lambda: build_final(True)}[cmd]()
