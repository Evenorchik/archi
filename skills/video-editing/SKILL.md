---
name: video-editing
description: Edit the owner's videos end to end with ffmpeg + Python. Two proven flows - a talking-head clip into a vertical reel (transcript-driven cuts, word-by-word captions, hook title, stickers, punch-in zooms, SFX, loudness) and re-cutting a finished edit so every cut lands on the music beat (retime with speed ramps, audio untouched). Use when the owner sends a video to cut, caption, make more dynamic, or sync to music.
user-invocable: false
---

# Video editing

Own pipeline: ffmpeg plus the small Python scripts in `scripts/` (copy them into the project directory and
fill their data block). Optional skill: it needs tools that a fresh install does not have.

## Requirements (check, install with the owner's OK)

- `ffmpeg` / `ffprobe` (the installer already puts ffmpeg on the server).
- A Python venv with `faster-whisper` (word-timestamp ASR), `pillow`, `numpy`, `librosa` (beat flow only).
  A GPU makes ASR and renders fast; on a CPU use a smaller Whisper model for drafts and `large-v3` for
  the final captions. If the owner has a GPU machine you can reach, run ASR and renders there.
- Fonts with heavy weights that cover the caption language (Montserrat, Inter, Noto Sans...), short
  pop / whoosh / sparkle sounds the owner may use.

## Intake

Before the file arrives, ask only for what changes the result, then start with whatever there is:
- **The original file.** Messengers recompress videos sent as media; ask for "send as file" or a link for
  anything large.
- **Platform** (default vertical reel: 1080×1920, 30 fps, about -14 LUFS).
- **Beat-sync jobs:** the separate source shots and the music file, if they exist. From a finished
  render the shots can only be shortened or retimed; with sources cuts can move freely.

When the owner says to do it to your taste, deliver the finished video in the same turn. Do not send a
cut plan for approval first; the report lists the decisions instead.

## Where to work

- One project directory per video (for example `~/video/<project>/`); check `df -h` before copying
  anything big.
- Look at the picture before deciding anything: `ffprobe`, then a contact sheet
  (`-vf "fps=1/5,scale=270:-1,tile=6x5"`) and Read it. Framing, face height and free areas for
  stickers come from there.

## A. Talking head → reel

1. **ASR with word timestamps** on the source: `scripts/asr_words.py src.mp4 words.json --lang <xx>`.
   It decodes audio with ffmpeg and passes a numpy array, because file input goes through PyAV inside
   faster-whisper and that is the part that fails on version mismatches.
2. **Cut list from the transcript** (`KEEP` ranges in source seconds, in `scripts/reel_build.py`):
   - Hook first: the strongest opening line starts at frame 0. Drop silence at both ends,
     throat-clearing openers, "so, well, yes" restarts, thinking pauses.
   - A long gap in the Whisper output is usually a RETAKE it skipped, not silence. Re-ASR every gap
     over ~2 s (`--from/--to`) and look at frames from it before calling it a pause. Keep one take of
     each pair; a splice between takes at a word boundary is fine when one take has a broken word.
   - Start a range ~40 ms before its first word. Trim inner pauses over ~0.4 s.
3. **Base cut**: `python reel_build.py base`. Each shot is encoded on its own and joined with concat
   copy; one filter graph with dozens of trims of a long source runs out of memory. Punch-ins change
   every 3–5 s at a word start after punctuation and on every real cut (the size jump hides the jump
   cut), with the face kept at the same height (`FACE_Y`). Real cuts get 15–20 ms audio fades.
4. **Re-ASR the CUT audio**: `asr_words.py base.mkv words_base.json`. Read it against the intended
   text: a clipped or missing word shows up here; move that boundary and rebuild. Captions are built
   from this file, so their timing is already in output time. Fix ASR spelling slips through `FIX`
   only after re-ASR of that snippet confirms the audio is right.
5. **Overlays** (PIL draws RGBA frames, piped into one ffmpeg overlay):
   - Captions: 1–3 words, ≤16 characters per page, uppercase heavy font with a face that covers the
     language, active word highlighted, small pop-in. Position around y≈1290 of 1920, clear of the
     app UI at the bottom.
   - Hook title from the very first frame (it doubles as the cover); captions start after it. Two
     elements that share a zone must never be on screen together; check the frame where one leaves
     and the other enters.
   - A card per list item ("item N of M" + name) in the upper area, and a closing card with who the
     speaker is / call to action. Style cards after the topic.
   - Animated Noto emoji on meaning words, alternating sides beside the head, ~1.9 s each:
     `https://fonts.gstatic.com/s/e/notoemoji/latest/<codepoints_joined_by_underscore>/512.gif`.
     Not every emoji has an animated version: pick a neighbour that has one, or save a static image
     under the same name (the loader handles single-frame files). An animation that travels out of
     its own frame reads as a glitch on a sticker; list it in `STILL`.
   - Thin progress bar on top. Quiet SFX (volume ~0.3): pop on stickers, whoosh on cards.
6. **Audio**: highpass 80 Hz, light compressor, `loudnorm=I=-14:TP=-1.5`, SFX mixed with
   `amix normalize=0` and a limiter. Measure the loudness of the final file, do not assume it.
7. `python reel_build.py check [times…]` → Read `stills.jpg`; fix; `python reel_build.py final`.
   No background music by default: suggest adding a trending track inside the app, offer an
   underlay if the owner wants one.

## B. Re-cut a finished edit to the music

1. **Current cuts**: `ffmpeg -i src.mp4 -vf "select='gt(scene,0.25)',showinfo" -f null -`, then
   confirm each cut's exact frame on a tile of the frames around it. Fades and in-shot events (a
   logo appearing) are not detected; find them by eye.
2. **Music grid**: `scripts/beats.py src.mp4` gives tempo and a rough grid, but librosa's beat
   tracker can sit ~50 ms late, which puts every cut after its hit. Take the accent times
   from `scripts/onsets.py a.wav` (spectral-flux peaks at 10 ms, per band): fit bar = 4 beats to the
   strong low-band hits and build the grid from those. The same script prints band energy per
   0.5 s: use it to find where the music actually ends and whether the tail is an effect (birds,
   wind) rather than music. Never align anything to "hits" in such a tail, and compare its loudness
   with the music (`ebur128` on both ranges); an effect louder than the track gets pulled down
   (`acrossover` + a timed `volume` on its band), which means re-encoding the audio.
   Cut frame = floor(hit_time × fps): on the hit or one frame early, never late. Cuts go on bar
   lines; beat 3 only as a short pickup shot into a strong hit; never on beats 2 or 4.
3. **Plan** (fill `SHOTS` in `scripts/retime.py`, in frame numbers, then `--plan`): keep the shot
   order and every shot; move each cut to a strong accent while the shot's average speed stays
   within about 0.8–1.3×, since beyond that interpolation shows. Put the highest-contrast transitions
   (day→night, the final shot, the logo) on the strongest hits. State how far the old cuts were
   off the grid; that number is the explanation of why the edit felt amateur.
4. **Dynamics** only where a shot has spare footage: ramp `a` 0.18–0.4 gives a fast entry on the hit
   and a calm exit. Leave the other shots at even speed.
5. **Render**: `python retime.py src.mp4 out.mp4`. Per shot: `setpts` with the ease curve, then
   `minterpolate` (it honours variable timestamps) and `-frames:v n`, because minterpolate emits a
   couple of extra frames per shot and they would push every later cut off its beat. The audio
   stream is copied from the source, so the music and the duration stay identical.
6. **Verify**: run the scene detection on `out.mp4` and compare the cut times with the target beats
   (±1 frame). Read full-size frames at every cut and of fine moving detail (fire, water, foliage,
   text, logo): that is where interpolation ghosts.

## Before sending (both flows)

- Contact sheet of the FINAL file, Read it; full-size frames at cuts and where overlays meet.
- Measured facts only: duration, resolution, loudness, cut positions. Output resolution never
  exceeds the source; say so when the source was small.
- Send with `[FILE …]` in the clean final answer.

## Report

Short, in the owner's language, no tables: result line (length before → after, format, what was
verified and how) → what was cut / moved → what was added → what the owner must check. Always in that
last part: names and titles written by ear (list them as spelled), and that motion as a whole was not
watched, only frames and measurements, so the owner should play it once before publishing.
