# Interactive 3D object on a site section (three.js)

For a product/object scene embedded in a page section (a car, a device, a part with clickable zones).

## Look: stylized, seamless, light
- **Default to stylized, not photoreal.** A realistic PBR model clashes with an illustrated site. Use flat
  toon shading (MeshToonMaterial or unlit 2–3 tone ramp), ink outline on the silhouette and panel seams,
  brand-colored accents, no textures. Selected part: soft emissive pulse in the brand color.
- **Background must blend into the page:** `alpha: true` renderer, transparent clear color, object sits on
  the site background with a soft fake contact shadow (blurred blob plane). Never a dark rectangle box.
- **Cheap by default:** no PBR, no env maps/reflections, no tone mapping, no real shadow maps; cap
  `setPixelRatio(Math.min(devicePixelRatio, 1.5))`, antialias off at high DPR; render on demand (only while
  something moves — auto-rotate, camera tween, drag), lazy-load the module when the section nears viewport,
  keep the 2D schematic as the no-WebGL fallback.
- Fit the viewport: the block must fit one screen on desktop and phone (check e.g. 390×844).

## Model source
1. Look for a free model with a clear license (CC0 / CC BY — credit the author in the footer for BY).
   Compress with gltf-transform (strip textures, simplify, draco/meshopt) but **keep node names** (no
   join/flatten) — clickable zones are mapped by node name.
2. If a **specific real model** is required (e.g. a named car) and no freely licensed asset exists, build it
   procedurally in code from its real dimensions: one mesh per selectable zone (hood, fenders, doors,
   bumpers, roof, tailgate…), so highlighting hits exactly the chosen part and no model file is downloaded.

## Verify the model itself
Screenshot front, side, rear, 3/4 and the x-ray/cutaway layer, and inspect each: parts floating in wrong
places (mirrors, grille), stair-stepped edges, panels bleeding into each other, seams that don't match zone
borders, highlight bleeding onto neighbouring parts. After changing material modes (e.g. transparency for
x-ray) set `material.needsUpdate = true`, or the shader is not rebuilt and the change silently does nothing.

## Orbit and framing
- **Pin the orbit target to the object's logical center** (center of the pedestal/turntable). Selecting a
  part must only rotate/tween the camera toward it, never move `controls.target` — a moved target makes the
  object slide sideways on every later drag.
- **Keep camera distance constant.** Do not auto-dolly to keep the pedestal inside the canvas: the object
  visibly shrinks and jitters while dragging, which users notice and dislike. Instead let it pass under overlay UI
  (button rows, captions) and fade the canvas edges into the page with a CSS `mask-image` gradient
  (strong at the bottom, soft at sides/top), so nothing is ever hard-cropped by the canvas border.
- Something "hiding under an invisible line" is usually the canvas bottom edge, not z-index — check canvas
  bounds before debugging layers.
- Part chips (a scrollable button row on the scene) beat hunting for hotspots: click → highlight, camera
  turns, info card opens; swap the chip list when the layer (e.g. x-ray) changes.

## Clean procedural model (common artifacts → fix)
- Thin dark lines across panels → small parts merged into the outline pass; outline per part, not glued.
- "Melted" nose/rear → smoothed normals wrapping round end caps; split normals so caps are flat, edges crisp.
- Interior showing through empty wheel arches → add dark inner liners in every arch.
- Diagonal lines through glass → far-side seams visible through transparent glass; use opaque tinted glass
  and hide the interior.
- Isolate a stray line by toggling layers (seams off, outline off) and re-screenshotting a zoomed crop.

## Mobile overflow
Measure `document.documentElement.scrollWidth` vs viewport width before and after the 3D loads. Floating
3D labels near the edge and invisible-but-scaled animated elements (stamps, badges pre-animation) widen the
page and show as a white strip on the right; clip them (`overflow: clip` on the section / `overflow-x: clip`
on body) rather than hiding the symptom.

## Headless testing
Server headless browsers render WebGL in software and are very slow. Keep 3D screenshot runs short and
bounded with `timeout`, and send the user the status/link reply BEFORE long test runs — a test that eats the
turn leaves him with no answer.
