---
name: blender-modelling
description: Build or rebuild a 3D model in Blender through an MCP connection (site assets, game props, print models) as a re-runnable generator script, judged by renders from fixed cameras and shown to the owner as before/after pictures. Carries the hard-surface method that gives taut surfaces with crisp character lines (quad cage + Subsurf + creases, details projected onto the skin) and a method for clean printable lettering. Use when the owner asks to model, remodel or improve a 3D object.
user-invocable: false
---

# Blender modelling through MCP

Optional skill: it needs Blender with an MCP server connected to your CLI (for example the Blender MCP
add-on with its server registered through `claude mcp add` / Codex's MCP config). Check first that the
Blender tools are available (deferred tools: load them with ToolSearch). Without them, tell the owner
what is missing instead of improvising. Blender may run on this server or on another machine; write down
where, how renders get back to you and how scripts get there, and keep that note in `knowledge/`.

## Procedure

1. **Find what exists and what consumes it.** Locate the current model files AND the pipeline that
   produces and loads them (exporter, per-part metadata, code that draws wheels/interiors around the
   body, cached posters). Write down the dimensions other code depends on (axle positions, wheel
   radius, overall length, axis convention) and keep them, so the new model drops in without touching
   the consumers. Report how many models there really are when the owner says "models".
2. **Load the current model into the scene as its own collection** and render it from the fixed camera
   set (step 4) before changing anything. Those are the "before" pictures and the list of faults to fix;
   name the faults in words (sagging nose, unreadable arches…) because they become the acceptance list.
3. **Write the model as ONE generator script** (`.scratch/<job>/build.py`), not as a chain of ad-hoc
   code snippets. The script rebuilds its own collection from scratch on every run (remove its objects,
   recreate), with all shape numbers as named constants / curve knots at the top. Loop: edit the file →
   put it where Blender can read it → one MCP call `exec(open('<path>').read())` → render → fetch → Read
   the pictures → next edit. A scene edited by hand-typed snippets cannot be rebuilt after a restart and
   cannot be diffed.
   - Helpers stored in `bpy.app.driver_namespace` (camera/shoot functions) die with Blender; keep them
     as a text datablock or a file and re-exec after a cold start.
4. **Fixed camera set, same for old and new**: side, front 3/4, rear 3/4 every pass; front, rear, top
   when working on those areas. Same studio, same lens, same framing, file names
   `<stage>_<view>.jpg` so stages compare one-to-one. LOOK at every render with Read before the next
   edit; geometry stats do not show a pinched corner or a floating part.
5. **Work coarse to fine, one layer per pass**: bare body proportions → openings (arches) and wheels →
   glass and material split → character lines → small details → corner/edge clean-up. Fix silhouette
   faults in the side view before adding detail; detail on a wrong silhouette is wasted passes.
6. **Finish the session**: save the .blend (generator script embedded as a text datablock), keep the
   script and the renders, and write the state and the "how to resume" lines into `knowledge/`.
7. **Report**: before pictures, after pictures (same views), what was wrong, what is fixed, what is
   still weak (say it plainly), and what integration work remains. Publishing to a site or game is a
   separate step that needs the owner's "yes" when they asked only to see it; do not touch the consumer
   project until then. End with one decision question (keep this direction and refine, or change the type).

## Hard-surface bodies (cars, housings, shells): the method that gives clean surfaces

A dense lofted triangle skin looks melted: no control over where curvature concentrates. Instead:

- **Sparse quad cage + Mirror + Subsurf 3.** Build half the body (x ≥ 0) as a grid of cross-sections
  ("stations") along the length; each section has a fixed number of points with fixed roles (floor →
  rocker → side → shoulder → belt → rail/crease → crown → centreline). Around 25 stations × 18 points
  is enough for a car body. Mirror with clipping, then Subsurf.
- **Drive the sections with design curves**, one per role along the length (centreline height, shoulder
  height, max half-width, rocker bottom, rail width/height), interpolated with a monotone spline
  (PCHIP). Proportion fixes are then edits of a few knots, not of vertices; cubic splines without the
  monotone constraint overshoot between knots and put waves in the surface.
- **Character lines = `crease_edge` on cage edge rows** (shoulder 0.8, belt/rail 0.45–0.7, rocker
  0.3–0.6). Fade creases to 0 over the last two stations before the end caps: a crease that runs into
  a cap pinches the corner.
- **Round openings** (wheel arches): put extra stations at the opening, snap a ring of cage verts to
  the circle and a second ring just outside it, skip the faces inside, extrude a short creased return
  flange inward for the lip. Put a dark tub behind so the arch does not show through.
- **End caps** (nose, tail) as a quad grid between the last section's bottom, side and top runs, with
  a bulge profile; n-gon caps under Subsurf wrinkle.
- **Details are separate geometry projected onto the evaluated skin**, never cut into the cage: build
  a `BVHTree.FromObject(body, depsgraph)` after modifiers, ray-cast 2D polylines (side view, top view,
  or radial around a vertical axis for nose/tail) and lay thin ribbons (shut lines ~7 mm, handles,
  pillars) or grid patches (lamps, intakes, plates) lifted 1–5 mm along the normal. Glass and sill can
  be material indices on cage faces when their borders follow cage rows. Cutting details into the
  cage destroys the surface; classifying dense faces by panel gives staircase edges.
- **Revolved parts** (tyres, rims, discs) as lathed profiles; one mesh instanced per wheel with a
  mirrored scale.
- Splitting into named panels for a consumer that explodes or highlights parts is done on cage faces
  (station/row ranges per panel) after the shape is approved, not before.

## Lettering and flat-outline print parts (signs, plaques): front edges that print clean

Curve bevel or a Bevel modifier on font meshes gives torn triangle fans and self-intersections on thin
strokes. Build the solid yourself instead:

- **Outline → height field.** Flat extrusion only supplies the outline loops. Constrained Delaunay
  (`mathutils.geometry.delaunay_2d_cdt`) of the outline plus offset rings; every vertex height comes from
  its distance to the outline (BVHTree of thin vertical walls along the outline, `find_nearest`). Vertical
  walls up to `D - R`, flat back at z = 0, quarter-round profile (a straight 45° line for a flat chamfer).
- **Rings at equal profile-angle steps** (8 for R = 4 mm). Make the point-exclusion radius proportional to
  the ring step; a fixed radius drops the steep rings next to the wall and the first facet spans 30°+.
- **Strokes thinner than 2R close into a ridge** on the medial line. Add one point where the distance
  peaks, then run data-dependent edge flips (flip when the other diagonal fits the true height better at
  the diagonal midpoints and triangle centroids). Plain Delaunay cuts across the ridge and renders as a
  saw-tooth. Tell the owner that a radius above half a stroke width becomes a soft ridge.
- **Keep the previous version's metrics** unless asked: same fonts (verify file size/hash), size from the
  measured ink height, target width by uniform tracking between letter groups (islands that overlap in x
  stay one group, so colon dots and diacritic dots move with their letter).
- **Triangulate before export** with `bmesh.ops.triangulate(quad_method='BEAUTY', ngon_method='BEAUTY')`.
  The STL exporter's own ear clipping turns n-gons with collinear vertices (left by `dissolve_limit`) into
  zero-area triangles and edges with 4 faces, even when Blender's checks on the n-gon mesh are clean.
- **Verify the exported STL outside Blender**: weld on exact float32 bits, every edge used by exactly 2
  triangles, no orientation clashes, no zero-area triangles, positive signed volume, part count and
  per-part sizes equal to the previous version. Look at close-ups of the thinnest serifs.
- **Measure stroke widths before promising a size** (ray across the stroke from each outline sample):
  display serif hairlines can be 2-3 mm at 190 mm cap height, so "2 mm on thin parts" may not fit at all.
- **"Straight, crisp chamfer" = CAD miters, one size everywhere** (the default). Distance-to-outline
  (V-bit) cones round every inner corner into a blob, which reads as a defect. Inner (reflex) vertex:
  cut = c - max(n_in·d, n_out·d) (a straight valley), applied ONLY inside the vertex wedge (beyond the
  incoming segment, before the outgoing one); applied outside it, the extended wall lines cut across the
  far side of narrow slots. A wall never cuts points behind its own line (no cuts through the empty side
  of a gap). Very sharp notches: cap the valley with a flat bevel plane rising along the bisector, not a
  cone. Chamfers of different sizes meeting at an inner corner always leave a vertical tab, so use one
  size; thin strokes then peak into a ridge, say so.
- **Mesh it as planar strips**: for every outline sample put the miter offset point V + b·c/cos(θ/2) and a
  CDT constraint V→M (both sides of a curve become exact planar trapezoids); where the 45° profile hits a
  ridge first, put the ridge point instead and share ridge points between the two sides of a thin stroke
  (merge radius ~0.3 mm), otherwise the ridge is a zipper of slivers that renders crinkled. Find hidden
  cliffs with a fine grid probe of the height field (jumps > 3 × step) before chasing mesh errors.
- **Chamfer sized by stroke width** only when the owner explicitly asks for it and accepts softer inner
  corners: size per outline sample from the width (keep a flat top, no ridge), but ONE size per long
  straight edge (low percentile of its samples), otherwise a baseline under a stem and its serifs swells
  under the stem; short edges (serif ends) take the size of their neighbours (erode + smooth along the
  outline). Surface = lower envelope of 45° cones of depth c from every outline point
  (`find_nearest_range`), which stays continuous where unequal chamfers meet. Then greedy refinement
  (insert the worst point where the linear surface misses the envelope by more than ~0.03 mm; never test
  midpoints of outline edges, those cannot be fixed) and the edge flips above.
- **Classify CDT faces by crossing parity from the hull, not by a centroid ray test**: slivers on three
  near-collinear outline samples have their centroid within 1e-5 mm of the outline, pass the ray test and
  give edges with 3 faces. Skip `dissolve_limit` there too: n-gons merged around slivers triangulate into
  double-back fins.
