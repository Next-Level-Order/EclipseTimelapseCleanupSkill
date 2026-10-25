# Measured evidence for each trap

Numbers from a real 162-frame partial solar eclipse (Canon EOS, CR3, 250/320/400 mm,
f/5.6–f/16, 1/20–1/125 s, ISO 100, 6022x4024 and 4024x6022). Keep these as the
rationale for the rules; if a future case contradicts one, measure before trusting it.

## 1. Centroid vs limb fit

Synthetic crescents with known geometry, 2100 px wide:

| case | centroid error | limb-fit error |
|---|---|---|
| full disk | 0.0 px | 0.1 px |
| crescent, 30 % angular | 29.2 px | 0.2 px |
| crescent, 60 % angular | 76.4 px | 0.1 px |
| crescent, 85 % angular | 120.9 px | 0.5 px |
| clipped left edge | 78.5 px | 0.8 px |
| clipped corner | 101.5 px | 2.2 px |

Conclusion: centroid degrades without bound as coverage grows; limb fit does not.

## 2. Why brightness matching flickers

One limb-darkening law fitted across the sequence gave u = 0.76 (linear in mu).
Exposure recovered from the model, compared with known synthetic exposures:

- median error **1.6 %**
- p90 error 57.9 % -- and those are exactly the frames whose true exposure
  (2.6) saturated the sensor. Unrecoverable, and correctly identifiable as such.

Matching per-frame disk brightness instead produced a visible pulse at maximum
eclipse, because the surviving crescent is limb photosphere and genuinely dimmer.

## 3. The 2294x gain

Photometric per-frame solve on the real sequence:

```
gain: min=2.94  p50=5.62  max=2294.22
frames clamped: 7  (IMG_3991, 3992, 3993, 3994, 4005, 4006, 4007)
```

EXIF physics on the same frames:

```
E = t/N^2, spread 9.5x over the whole shoot
gain_exif range: 0.33 .. 3.17
```

A 2294x gain is not a tuning problem, it is a modelling failure: on a thin crescent
the only lit pixels sit at mu ~ 0.2 where `1 - u(1 - mu)` is small, so `A = I/L`
divides by nearly zero.

## 4. Zoom was not constant

EXIF summary across 162 files:

```
57 files  250 mm   disk radius ~402 px
59 files  320 mm   disk radius ~507 px
46 files  400 mm   disk radius ~601 px
```

Radius tracks focal length almost exactly, so radius is a zoom measurement.
Within each group radius scatter was under 1 %.

## 5. Moon fit conditioning

Contour fit on a 2 % bite: returned radius **0.19 R** (physically impossible; the
Moon's angular size is within a few percent of the Sun's).
Forward-model IoU fit on the same frame: r = 0.94-1.06 R, IoU 0.999.

Frames with thin crescents still yield low-confidence fits (IoU 0.36-0.69) -- report
those as uncertain rather than silently trusting them.

## 6. Tone mapping, before and after

IMG_3872, measured in the tile:

| variant | sky p50 | disk mean | limb | detail (HF std) |
|---|---|---|---|---|
| double gamma + mid-grey pivot | 58 | 229 | 174 | 0.34 |
| single transfer, no local contrast | 0 | 131.9 | 64.3 | 0.51 |
| single transfer + local contrast | 0 | 132.2 | 65.4 | 1.10 |

The first row is the failure: empty sky at 58/255 grey, disk clipped near white,
least detail of all three.

Limb halo, radial profile before the mask-normalised blur:

```
rho 0.88-0.96:  dehaze 0 -> 101.4    dehaze 80 -> 117.9   (+16.5)
```

After the fix (masked blur + radial taper):

```
rho 0.92-0.96:  +0.4
rho 0.96-1.00:  +0.0
interior detail 0.51 -> 1.11
```

The taper is what buys the +0.0: local contrast fades to nothing at the edge, so it
can never lift the outermost pixels.

## 7. Motion artefacts

Easing each segment separately (smoothstep per keyframe) makes velocity zero at every
keyframe. With 21 keyframes that is 21 accelerate/stall cycles -- reported by the
reviewer as "rubberbanding disks".

Global monotone cubic (PCHIP) fixes it: C1 continuous, no overshoot, constant speed
between keyframes. Verified in the final render as a steady cx progression
(0.214 -> 0.721 with monotonically positive dx).
