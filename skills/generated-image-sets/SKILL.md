---
name: generated-image-sets
description: Generate a SET of matching AI illustrations (cards, tiles, icons, banner panels) through the Gemini image API (Nano Banana) and composite them into one final graphic (banner, poster, sheet) with crisp layout-drawn frames and text. Use for any multi-image creative where style consistency and final composition quality matter.
user-invocable: false
---

# Generated image sets → composed graphic

Key: `GEMINI_API_KEY` in `config/archi.env` (never print it). Model options, prices and the
request format notes: `knowledge/genmedia-api-pricing-2026-11.md`.

## Procedure

1. **Fix the budget tier first.** For test/preview passes the user usually wants the art at medium
   settings (2K, thinking `medium`) while the *composition* is final quality. Use 4K / `high` only
   when he asks or the piece will be printed/seen up close. State the spend in the reply.
2. **Gather brand material from the real site** (colors, fonts, logo file, background textures,
   signature effects like a neon sign) and reuse it in the composition instead of inventing a look.
3. **Write the set as data** (`cards.json`: slug, title, scene prompt) and a re-runnable
   generator script (`gen.py <slug> [--ref <img>] [--tag vN]`) so one item can be regenerated alone.
4. **Generate ONE sample, look at it, then the rest with the sample as a style reference** (`--ref`),
   a few in parallel. This keeps the palette and rendering consistent across the set.
5. **Let the layout, not the model, draw frames, numbers and titles.** Prompt for the illustration
   only; models misspell text and vary borders between images. If the model still paints a frame,
   auto-crop it (`crop.py`). Pick a real font in the target style; check it has the glyphs you need
   (Cyrillic!) before promising localized titles.
6. **Compose in HTML/CSS and screenshot with headless Chromium** (or PIL) at the exact target pixel
   size; keep a `--scale` option for a print master.
7. **Check before sending:** a contact sheet of all items, an overview of the whole piece, and
   2–3 full-resolution crops. Look at each for overflowed titles, wrong numbering, seams, colour drift.
   Fix and re-render — the user should see the fixed version, not a list of known bugs.
8. **Deliver** a JPEG preview (images now arrive in Element as previews) plus full-res parts for
   very wide formats; list the open choices (spacing, language, resolution) as short bullets.

## Variant: dense scatter of cut-out objects (parts collage, "look-at-it" banner)

1. Generate sheets of ~12 objects each on plain white, same flat style (sample sheet as `--ref`).
2. Cut objects from white by connected components; make enclosed white regions (holes in gaskets,
   gears, tyres) transparent, otherwise they read as white fills on the dark background.
3. Label each cut object with a cheap vision model (Gemini Flash) on a numbered sheet, including its
   real-world size, and scale by that size so a fuse is smaller than a brake disc.
4. Place every distinct object once before allowing repeats; keep identical large items far apart.
5. Avoid "mush" from a distance: add a few large anchor objects (barely rotated, used once, spread
   in a zigzag, more clearance and deeper shadow), widen the size spread, let small items fill gaps,
   and never drop small items inside holes of tyres/gears. Check a downscaled copy as the far view.
   When more visual variety is asked for, add a top "mega" tier above the anchors: whole-assembly
   silhouettes of the theme (e.g. bare car bodies, a frame, an engine on a stand, big body panels),
   generated as a separate small sheet in the same style, scaled by their own real size, alternated
   with anchors in the zigzag. Flat look-alike pieces (panels) are labelled by colour, not shape,
   because the vision model confuses hood/trunk/bumper outlines.
6. Logos sit INSIDE the scatter, not on empty side panels: cover the whole canvas, keep a clearing
   that follows the logo outline (objects never enter letters), and thin the scatter toward the
   canvas edges (larger gaps, soft vignette) so it dissolves instead of ending at a hard line.
7. Keep layout as a seeded script with physical size and px/m as inputs, so a new print size or
   another arrangement is a re-render with no new generation spend.

## Pitfalls
- Very wide formats (10:1+) leave big gaps between items or empty ends; plan filler (slogan, phone,
  ornaments) or ask about spacing up front.
- Original-deck motifs the model adds (letters on pillars etc.) are fine when the source genre has
  them — mention it so it isn't read as a glitch.
