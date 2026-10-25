"""Tone mapping for eclipse frames.

Two bugs this replaces:

1. DOUBLE GAMMA. The previous code did  y ** (1/2.2)  and then sRGB encoding,
   which is itself ~2.2 gamma. Near-black sky (1.5e-4 linear) came out at 54/255
   grey and the disk lost all contrast. There is now exactly ONE transfer
   function at the end of the chain.

2. NO BLACK POINT. Without anchoring the sky to zero, any lift shows up as a
   milky background. The sky here is genuinely empty (p50 = 1.5e-4), so it must
   map to 0, not to grey.

The controls mirror the sliders David tuned by hand on IMG_3872
(exposure +0.18, contrast +46, highlight +24, dehaze +48, clarity +37,
texture +37), expressed on comparable 0..100 scales so his numbers can be
passed straight through.
"""
import numpy as np
import cv2

def _srgb(x):
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, x*12.92, 1.055*np.power(x, 1/2.4) - 0.055)

def _s_curve(x, amount, pivot=0.5):
    """Contrast around a chosen pivot; amount 0..100 (Lightroom-ish).

    The pivot matters enormously here. Pivoting at mid-grey (0.5) on an eclipse
    disk that already sits around 0.75 display pushes every tone upward and
    clips the surface to flat white. Pivoting at the disk's own level keeps the
    surface centred and expands detail symmetrically about it.
    """
    if amount <= 0:
        return x
    k = amount/100.0*0.9
    d = x - pivot
    return np.clip(pivot + d*(1.0 + k) - k*d**3*1.2, 0.0, None)

def _masked_blur(y, sigma, mask):
    """Gaussian blur that ignores everything outside `mask` (normalised conv).

    A plain blur near the solar limb averages in the black sky, which drags the
    base down and makes the edge look like a huge positive detail signal. That is
    what produced the bright ring: at rho 0.88-0.96, dehaze 80 rendered 118 where
    dehaze 0 rendered 101. Normalising by the blurred mask makes the base the
    local *disk* average, so the edge is no longer an edge.
    """
    m = mask.astype(np.float32)
    if y.ndim == 3 and m.ndim == 2:
        m = m[..., None]                      # broadcast over colour channels
    num = cv2.GaussianBlur(y*m, (0, 0), sigma)
    den = cv2.GaussianBlur(np.broadcast_to(m, y.shape).astype(np.float32), (0, 0), sigma)
    return num/np.maximum(den, 1e-4)

def _feather(mask, sigma):
    """Soft edge for a hard mask, so the detail fade has no visible seam."""
    m = mask.astype(np.float32)
    return np.clip(cv2.GaussianBlur(m, (0, 0), sigma), 0.0, 1.0)

def _add_detail(y, detail):
    """Add local contrast without ringing at the limb.

    Plain unsharp overshoots at a step edge: on the bright side of the solar
    limb the positive lobe has no headroom, so it clips and shows up as a bright
    ring around the Sun. Positive detail is therefore scaled by remaining
    headroom (1-y); negative detail is applied more freely because darkening has
    room and is what actually reveals structure.
    """
    head = np.clip(1.0 - y, 0.0, 1.0)
    pos = np.maximum(detail, 0.0) * (head**1.6)
    neg = np.minimum(detail, 0.0) * (0.35 + 0.65*np.clip(y*2.0, 0.0, 1.0))
    return np.clip(y + pos + neg, 0.0, None)

def _unsharp(img, radius, amount):
    """Local contrast at a given scale (large radius = clarity, small = texture)."""
    if amount <= 0:
        return img
    base = cv2.GaussianBlur(img, (0, 0), radius)
    return img + (amount/100.0)*(img - base)

class ToneParams:
    exposure = 0.18      # stops
    contrast = 46.0      # 0..100
    highlights = 24.0    # 0..100, lifts/compresses the top end
    dehaze = 48.0        # 0..100, black point + micro contrast
    clarity = 37.0       # 0..100, large-radius local contrast
    texture = 37.0       # 0..100, small-radius local contrast
    target_level = 0.52  # display level the anchor lands on (headroom above)
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

def render(lin, params: ToneParams, gain=1.0, clip_black=True, anchor=None,
           disk_mask=None, rho=None):
    """Linear patch -> uint8 display image. Exactly one transfer function.

    `anchor` is the linear value that should land at display level
    `params.target_level` (default 0.78). Anchoring to the measured photosphere
    instead of hard-coding 1.0 is essential here: the Sun's disk occupies only
    ~0.57..0.97 linear, so a naive x/max mapping squeezes every tone into a
    narrow bright band and the surface looks blown out.
    """
    x = np.asarray(lin, np.float32)*gain*(2.0**params.exposure)

    if anchor is None:
        anchor = float(np.percentile(x, 60))
    anchor = max(anchor, 1e-9)
    # solve scale so the anchor lands exactly on the requested display level
    target_lin = max(params.target_level, 1e-4)**2.2
    x = x*(target_lin/anchor)

    # --- highlight roll-off applied in LINEAR, before any gamma
    h = params.highlights/100.0
    knee = 0.65
    over = x > knee
    if over.any():
        span = max(1e-6, 1.0 - knee)
        x = np.where(over, knee + span*(1.0 - np.exp(-(x-knee)/span*(1.0+0.8*h))), x)

    # --- display transfer: the ONE transfer function in the chain.
    # It must be sRGB *or* a plain power, never both. An earlier version did
    # x**(1/2.2) and then sRGB-encoded the result, i.e. gamma applied twice;
    # that lifted the empty sky to ~58/255 grey and drove the disk to 245/255.
    y = _srgb(np.clip(x, 0.0, None))

    # --- dehaze.
    # A black-point pull alone is a no-op here (the sky is already 0), which is
    # why the first attempt "did nothing". Real dehaze does two things: it
    # removes the veiling pedestal AND adds medium-scale local contrast.
    if params.dehaze > 0:
        d = params.dehaze/100.0
        lo = float(np.percentile(y, 2.0))
        if lo > 0.004:
            y = np.clip((y - d*lo*1.5)/max(1e-6, 1.0 - d*lo*1.5), 0.0, None)
        # medium-radius local contrast at a radius that resolves surface
        # structure rather than the disk edge, and headroom-safe so it cannot
        # ring the limb
        sigma = max(3.0, 0.012*y.shape[0])
        if disk_mask is not None:
            base = _masked_blur(y, sigma, disk_mask)
            fade = _feather(disk_mask, sigma*0.8)
            if y.ndim == 3 and fade.ndim == 2:
                fade = fade[..., None]
        else:
            base = cv2.GaussianBlur(y, (0, 0), sigma)
            fade = 1.0
        if rho is not None:
            tt = np.clip((0.955 - rho)/0.075, 0.0, 1.0)
            taper_d = (tt*tt*(3-2*tt)).astype(np.float32)
            if y.ndim == 3 and taper_d.ndim == 2:
                taper_d = taper_d[..., None]
        else:
            taper_d = 1.0
        y = _add_detail(y, d*1.35*(y - base)*fade*taper_d)

    # --- local contrast at two scales, luminance only so colour stays clean
    if params.clarity > 0 or params.texture > 0:
        yy_ = np.clip(y, 0, 1).astype(np.float32)
        lum = (cv2.cvtColor(yy_, cv2.COLOR_RGB2GRAY) if yy_.ndim == 3 else yy_)
        detail = np.zeros_like(lum)
        # Lightroom's clarity/texture are far stronger than a mild unsharp pass;
        # the numbers David tuned need real multipliers to reproduce the look.
        # Radial taper: local contrast is welcome inside the disk but must fade
        # to nothing at the limb, otherwise the last few pixels before the edge
        # get lifted and read as a halo. rho is in units of the solar radius.
        if rho is not None:
            t = np.clip((0.955 - rho)/0.075, 0.0, 1.0)
            taper = (t*t*(3-2*t)).astype(np.float32)
        else:
            taper = 1.0
        if params.clarity > 0:
            sig = max(3.0, 0.022*lum.shape[0])
            base = (_masked_blur(lum, sig, disk_mask) if disk_mask is not None
                    else cv2.GaussianBlur(lum, (0, 0), sig))
            detail += (params.clarity/100.0*2.6)*(lum - base)
        if params.texture > 0:
            base = (_masked_blur(lum, 1.5, disk_mask) if disk_mask is not None
                    else cv2.GaussianBlur(lum, (0, 0), 1.5))
            detail += (params.texture/100.0*2.2)*(lum - base)
        if disk_mask is not None:
            detail *= _feather(disk_mask, 1.5)   # detail is 2-D luminance
        detail *= taper
        y = _add_detail(y, detail[..., None])

    # --- global contrast last, pivoted at the surface level so the disk is
    #     expanded about itself instead of being pushed into the top end
    y = _s_curve(y, params.contrast, pivot=float(np.clip(params.target_level, 0.15, 0.85)))

    if clip_black:
        y[y < (2.0/255.0)] = 0.0

    # already display-referred: do NOT encode again
    return (np.clip(y, 0, 1)*255).astype(np.uint8)
