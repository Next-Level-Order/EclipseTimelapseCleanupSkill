# Adapting this to other celestial subjects

The pipeline is the same five stages. What changes is the detection polarity, the
exposure model, and what counts as "the disk". Worked notes for each case.

## Lunar eclipse (total or partial)

The Moon is the disk, but at totality it is **dark red and very low contrast**
against a night sky. Several assumptions invert.

**Detection.** Do not threshold on absolute brightness. Use a *local* contrast map
or fit the disk from the bright limb before totality. During totality the disk is
barely above background in luminance, so a MAD threshold will fail; work in a
chromatic channel instead, because totality is strongly red-shifted:

```python
# R - B isolates the copper disk from a grey/blue sky (PIL gives 0-255)
rimg, gimg, bimg = img[...,0], img[...,1], img[...,2]
chroma = rimg.astype(np.float32) - bimg.astype(np.float32)
mask = chroma > np.median(chroma) + 3*chroma.std()
```

**Exposure.** EXIF `t/N^2` still works, but totality spans ~11 stops from partial
phase to mid-totality in minutes. The Moon's brightness also varies with the
umbra depth (Danjon scale), which EXIF cannot know. Use a three-part model:

1. EXIF `t/N^2` for the camera's intent.
2. Per-frame measured surface brightness as a *residual* on top, clamped hard.
3. Interpolate gains smoothly in time.

Unlike a solar eclipse, the lunar photosphere is **not** limb-darkened in a way
that fights you -- the disk is roughly flat with mare detail -- so the
limb-darkening fit of the solar case generally should not be used. Verify: fit u
and check whether it lands near 0. If it does, drop the model and normalise on the
disk median directly.

**Tone.** Totality images are dark. Do **not** apply the solar "anchor at 0.52"
rule blindly -- it will push a dim red disk to grey. Anchor on measured disk
brightness, keep saturation high, and expect to lift 1-3 stops. Preserve the red:
never apply per-channel stretch (that neutralises colour); scale luminance only.

**Composition.** The star field is fixed, so if the camera was static you can keep
it. If it moved, either crop to the disk or plate-align on stars using the same
limb-fit centre, offsetting the field. Plate alignment is worth it when stars are
visible.

## Sun time-lapse (not an eclipse)

No moon to fit, so skip `detect_moon` and the coverage machinery entirely. The disk
is a plain bright circle.

**Detection.** Same limb fit. Sunspots survive the fit and slightly bias it -- the
limb circle is dominated by the edge, so this is minor, but reject outliers with
RANSAC inliers rather than fitting all boundary points.

**Exposure.** Two modes:

- *Auto-exposure camera*: EXIF `t/N^2` is wrong because the camera compensated for
  atmospheric dimming. Use measured disk brightness with a smooth temporal median
  (window 5) -- flicker here is smooth drift, not per-frame noise.
- *Manual exposure*: EXIF works as in the solar eclipse case.

**Atmospheric scintillation** adds real high-frequency noise near the horizon. Do
not add "texture" local contrast to a low-elevation sun; you will amplify noise.
Gate the local-contrast amount on measured SNR, or set texture to 0 when the disk's
HF energy is dominated by noise (compare HF std inside the disk to HF std in a ring
of sky).

**Motion.** Sun time-lapses usually want the *opposite* of this skill's synthetic
track: the Sun's real path across the frame is the content (it shows the diurnal
arc). Two acceptable expressions:

- keep the real path, but smooth it (global monotone fit, never per-frame easing)
- synthesise a clean path (this skill's default) and state plainly that the motion
  is representational

Ask which one is wanted. For an eclipse the real positions are noise; for a
time-lapse they are data.

## Moon time-lapse (no eclipse)

Simplest case, and a good first target. The Moon at night is high contrast.

**Detection.** Bright-limb circle fit works. Watch for **clipping**: at longer
exposures the Moon saturates to flat white, and a saturated disk has no limb
gradient and no detail -- halving the exposure is the only fix, not tone mapping.
Flag frames where more than ~2 % of the disk is at max as unusable for detail.

**Phase handling.** A crescent Moon has the identical centroid problem as a
crescent Sun (trap 1) and the identical ill-conditioned-silhouette problem (trap 5).
Reuse the limb fit and the forward model unchanged; the Moon's dark side is the
"moon disk" in that model, so `detect_moon` semantics map directly.

**Exposure across phases.** Surface brightness of the lunar disk barely changes with
phase, but the *average* brightness of the frame changes enormously. This is why
matching frame brightness fails and surface brightness does not. Same lesson as the
solar case, different cause.

## Generalisation checklist

For any new subject, establish these five facts **before** writing code:

1. **Polarity**: is the disk brighter or darker than its surround?
2. **Shape prior**: is the angular size constant, or does it drift?
3. **Dominant failure at extreme phase**: what becomes unmeasurable, and when?
4. **Is EXIF exposure trustworthy** (manual) or compensated (auto)?
5. **Is the real motion data or noise?**

The answers determine which traps in `traps.md` apply and which can be skipped.
Write them down at the top of the project before the first render.
