---
name: celestial-compositing
description: Cut Sun or Moon disks from eclipse or time-lapse stills, normalise zoom and exposure, and animate them without flicker.
---

# Celestial compositing (eclipse and disk time-lapses)

Assemble stills of a Sun or Moon into one video where the disk travels a controlled
path, looks identical from frame to frame, and never flickers. Built and validated on
a real 162-frame partial-solar-eclipse shoot (Canon CR3, 250/320/400 mm, f/5.6–f/16,
1/20–1/125 s, mixed portrait and landscape).

The hard part is **not** the compositing. It is that every naive measurement of a
celestial disk breaks at some point in an eclipse, and each break looks like a
different bug downstream. Read the traps before writing code.

## When this applies

- partial or total **solar** eclipse stills → video
- **lunar** eclipse stills (the moon is the disk; it is dark, red and low-contrast)
- **sun or moon time-lapses** where the disk was re-framed between shots
- any sequence where a bright/dark disk must be held at constant size and exposure

Not for: ordinary landscape time-lapses (no disk to fit), video footage (this
operates on stills).

## Pipeline

Five stages. Each writes a file the next stage reads, so any stage can be re-run alone.

```
1 detect   RAW -> disk centre + radius per frame      -> shots.json
2 measure  exposure + limb darkening across sequence   -> gain per frame
3 cut      square, radius-normalised tiles             -> tiles/*.png + meta.json
4 review   contact sheet + short video                 -> human approval
5 animate  tiles on a synthetic path -> final video    -> mp4
```

**Never skip stage 4.** Show the cutouts before animating. A reviewer catches
per-frame errors by eye in seconds that no metric will flag.

## The seven traps

These each cost real time. They are not hypothetical.

### 1. Brightness centroid is useless at crescents
At deep partial eclipse only a crescent of photosphere remains. Its intensity
centroid sits well inside the moon: measured error **121 px** at 8 % coverage.
Fit the **limb circle** instead. Use a radius prior from clean full-disk frames and
solve centre-only for clipped frames. Sub-2 px accuracy at every phase.

### 2. Matching per-frame brightness *causes* flicker
The photosphere is limb-darkened, so at deep crescent the surviving arc is
intrinsically dimmer. Matching "brightness of the disk" therefore brightens crescents
and manufactures a pulse exactly at maximum eclipse.
Fit **one limb-darkening law across the whole sequence**, `L(mu) = 1 - u(1 - mu)`,
and solve each frame's exposure from it. That estimate is phase-independent.

### 3. Photometric exposure solves explode
A per-frame photometric solve returned gains up to **2294x** on thin crescents, where
the only lit pixels hug the limb and the model degenerates. The real optical spread
was 9.5x.
Use **EXIF physics**: illumination ∝ `t / N²` (exposure time over f-number squared).
Exact, phase-independent, and bounded by construction. Clamp any residual correction
to about 2x.

### 4. Zoom is rarely constant — never hard-code radius
A "fixed lens" shoot still had **three** focal lengths (250/320/400 mm), giving disk
radii of 402 / 507 / 618 px. Assuming constant size breaks every crop.
Fit radius per frame; when a frame's own fit is unreliable (clipped limb or low
coverage), borrow the **median of frames sharing its focal length** — zoom never
changed *within* a group, so that prior is physically correct.

### 5. Contour fitting a thin crescent is ill-conditioned
Fitting the moon's own limb returned `r = 0.19R` for a 2 % bite.
Use **forward-model matching**: for candidate moon offsets, synthesise the predicted
bright mask (`inside disk AND outside moon`) and keep the offset with the best IoU.
Well conditioned at every phase because *area* mismatch drives the score, not edges.
Expect ~0.04 R and IoU 0.999. Seed each frame's search from its predecessor — the
eclipse only advances, so a global grid can lock onto a physically impossible fit.

### 6. Display encoding is where eclipse images die
Three separate bugs, all in tone mapping, all producing "washed out, grey sky":

- **Double gamma.** `x**(1/2.2)` *then* sRGB encoding (itself ~2.2) lifts empty sky
  from 1.5e-4 linear to **58/255 grey** and drives the disk to 229/255. Use exactly
  **one** transfer function.
- **Contrast pivot.** Pivoting contrast at mid-grey (0.5) pushes an already-bright
  disk (0.57–0.97 linear) into the top end and clips it flat. Pivot at the measured
  surface level instead.
- **Limb halo.** Local-contrast operators blur *across* the limb, average in black
  sky, and read the edge as a huge positive signal: at dehaze 80 the limb rendered
  **118 where no-dehaze gave 101**. Fix with a **mask-normalised blur** (divide by
  the blurred mask so the baseline is the local *disk* average) plus a **radial
  taper** that fades added detail to zero at the outer ~4 %.

Verify with a radial profile, not by eye. The test is: limb must stay *darker* than
the interior, and the outermost band must move **+0.0** when you raise local contrast.

### 7. Motion must be synthetic
The disk's position inside each source frame only records where the camera was
pointed. It jitters shot to shot. Driving placement from it produces visible
**rubberbanding** — and easing each segment independently is worse, because velocity
falls to zero at every keyframe, so the disk accelerates and stalls once per shot.

Generate the path: one continuous monotone move with constant speed and soft ramps
**only at the two ends**. Never per-shot easing. Content stays real; motion is drawn.

## Answering "the disk left the frame"

Frames where the disk is partly or fully outside the sensor are still solvable
(radius from the focal group, centre from an arc fit). But they usually contribute
little and should be **reviewed, not silently kept or silently dropped**. Keep
exclusions in a data file so nothing is destroyed:

```json
{ "drop": { "IMG_3993": "dark and mis-cropped", "IMG_4007": "mis-placed" } }
```

Report every excluded frame to the reviewer explicitly.

## Resolution rule

Match the display disk radius to the **native source** radius, or larger. Rendering
a 500 px disk at 324 px throws away detail; a 1080 tile inside a 4K frame gives the
disk only ~25 % of frame width and looks soft.

Rule of thumb: `tile ≈ native_radius / diskfrac`. For a 4K output with the disk at
~34 % of frame width, `tile 2160, diskfrac 0.30` puts the disk at 648 px — no
upscaling. Composite tiles **1:1**, never resample the disk.

## Review artefacts the human needs

1. **Contact sheets** — every tile in a grid, so per-frame errors are visible at once.
2. **A labelled short video** — file, focal length, coverage per tile.
3. **A before/after tone comparison** with numbers, plus a **1:1 limb zoom** (the
   halo only shows at full resolution).
4. **Measured table**: sky level, disk mean, limb level, detail (high-frequency std),
   clipping %.

Report numbers, not adjectives. "Sky 0, disk 132, detail 1.10 vs 0.51" invites
correction; "looks much better" does not.

## Quality gates before delivering

```
sky p99        == 0        (empty sky must be black, not grey)
disk mean      tight across sequence (no flicker)
clipping       ~0 %        (a clipped disk has no detail to recover)
limb < inner   always      (limb darkening preserved, no halo)
border lit     == 0        (nothing touches canvas edge)
disk radius    constant    (within 1-2 px of target)
disk downscale <= 1.0      (never upscale past native)
```

## Encoding

Flat bright disks on black are where H.264 artefacts show. Use **CRF 12** with
`-preset slow`, not the default. Verify with PSNR against a source frame; expect
>60 dB. A 25 s 4K piece at this setting lands around 20 MB — that is not too big,
it is the point.

## Scripts

Working implementations in `scripts/`. `sunlib.py` and `tonemap.py` are the
substance; the drivers show wiring.

| file | role |
|---|---|
| `sunlib.py` | disk detection, limb-darkening fit, moon fit, circle overlap |
| `tonemap.py` | display mapping: single transfer fn, phase-aware anchor, headroom-safe local contrast |
| `pipeline.py` | RAW decode (rawpy), exposure solve, sprite extraction, EXIF listing |
| `cutouts2.py` | radius-prior cutouts, EXIF gains, exclusions, tile writing |
| `render_final.py` | synthetic track, dissolve, raw frames piped to ffmpeg |

Reference material: `docs/traps.md` (measured evidence for each trap) and
`docs/recipes.md` (lunar eclipse, sun time-lapse, moon time-lapse adaptations).

Note the scripts table above refers to files in `scripts/`. `{baseDir}` is this
skill's own folder.

## Toolchain note

Many machines have no system ffmpeg, ImageMagick or exiftool and no root. Everything
here works userland:

```bash
uv venv .venv && uv pip install --python .venv/bin/python \
  rawpy numpy opencv-python-headless scipy pillow imageio-ffmpeg
```

`imageio_ffmpeg.get_ffmpeg_exe()` gives a **static ffmpeg 7.x with the full filter
set**. Note that Remotion's bundled compositor ffmpeg is compiled with
`crop`/`overlay`/`eq`/`curves` **disabled** — it encodes, it cannot composite.
For EXIF, unpack the distro `libimage-exiftool-perl` `.deb` and set `PERL5LIB`;
`rawpy` exposes camera model only on some builds, so use `getattr(..., 'camera_model', '')`.
