"""Solar disk detection + tone mapping primitives for eclipse frame sequences.

Design notes (why not just threshold+centroid):
  * At deep partial eclipse only a crescent of photosphere remains. The intensity
    centroid of that crescent sits well inside the moon, i.e. off the true disk
    centre. Centre must come from FITTING THE LIMB CIRCLE.
  * The moon's terminator is also an edge in the mask, but it is not on the solar
    limb and does not obey the radius prior -> RANSAC + radius prior rejects it.
  * Radius is constant for a fixed zoom, so it is a strong prior; we estimate it
    once from clean full-disk frames and then refit centre with R fixed.
  * Sun partially outside the frame -> arc-only fit, still solvable with R fixed.
"""
import numpy as np

# ---------------------------------------------------------------- preprocessing
def to_lum_linear(img):
    """img: (H,W,3) float32 linear RGB -> luminance."""
    return (0.2126*img[...,0] + 0.7152*img[...,1] + 0.0722*img[...,2]).astype(np.float32)

def downsample(lum, factor):
    h, w = lum.shape
    h2, w2 = h//factor*factor, w//factor*factor
    return lum[:h2,:w2].reshape(h2//factor, factor, w2//factor, factor).mean(axis=(1,3))

# ---------------------------------------------------------------- masking
def sun_mask(lum, k=8.0, peak_frac=0.30):
    """Threshold for "this pixel is Sun", robust to how much Sun there is.

    A pure noise-based threshold (median + k*MAD) fails at deep crescent on a
    dark frame: with only a thin sliver lit, the noise dominates the MAD and the
    threshold can land ABOVE the brightest real pixel, so the mask comes back
    empty. So the noise estimate is capped by a fraction of the actual peak --
    the Sun is by construction the brightest thing in the frame.
    """
    med = float(np.median(lum))
    mad = float(np.median(np.abs(lum-med))) + 1e-9
    sigma = mad*1.4826
    peak = float(np.percentile(lum, 99.9))
    if peak <= med + 4*sigma:
        return np.zeros(lum.shape, bool)      # nothing above the noise floor
    thr = med + k*sigma
    thr = min(thr, med + peak_frac*(peak-med))
    return lum > thr

def largest_component(mask):
    import cv2
    n, lab, stats, cent = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 1:
        return None, None
    areas = stats[1:, cv2.CC_STAT_AREA]
    i = int(np.argmax(areas))+1
    return (lab == i), stats[i]

# ---------------------------------------------------------------- circle fitting
def kasa_fit(pts):
    """Algebraic circle fit. pts: (N,2) [x,y]. Returns (cx,cy,r)."""
    x, y = pts[:,0], pts[:,1]
    A = np.c_[2*x, 2*y, np.ones(len(x))]
    b = x*x + y*y
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy = sol[0], sol[1]
    r = np.sqrt(max(sol[2] + cx*cx + cy*cy, 1e-12))
    return cx, cy, r

def fit_center_fixed_r(pts, r, c0=None, iters=15):
    """Gauss-Newton for centre only, radius fixed."""
    c = np.array(c0 if c0 is not None else pts.mean(axis=0), dtype=np.float64)
    for _ in range(iters):
        d = pts - c
        dist = np.linalg.norm(d, axis=1) + 1e-12
        res = dist - r
        J = -d/dist[:,None]
        H = J.T@J + 1e-6*np.eye(2)
        g = J.T@res
        step = np.linalg.solve(H, g)
        c = c - step
        if np.linalg.norm(step) < 1e-4: break
    return c[0], c[1]

def ransac_circle(pts, r_prior=None, r_tol=0.06, iters=400, tol=1.5, rng=None):
    """Robust circle fit. Returns (cx,cy,r,inlier_mask) or None."""
    rng = rng or np.random.default_rng(0)
    n = len(pts)
    if n < 12: return None
    best = None; best_score = -1
    for _ in range(iters):
        idx = rng.choice(n, 3, replace=False)
        p = pts[idx]
        try:
            cx, cy, r = kasa_fit(p)
        except np.linalg.LinAlgError:
            continue
        if not np.isfinite(r) or r <= 2: continue
        if r_prior is not None and abs(r-r_prior) > r_tol*r_prior: continue
        dist = np.abs(np.linalg.norm(pts-np.array([cx,cy]), axis=1) - r)
        inl = dist < tol
        score = int(inl.sum())
        if score > best_score:
            best_score, best = score, (cx, cy, r, inl)
    if best is None: return None
    cx, cy, r, inl = best
    # refit on inliers
    if inl.sum() >= 12:
        cx, cy, r = kasa_fit(pts[inl])
    return cx, cy, r, inl

# ---------------------------------------------------------------- main entry
def detect_sun(lum_full, r_prior=None, ds=4, k=25.0, verbose=False):
    """Return dict(cx,cy,r,clipped,coverage,n_pix) in FULL-resolution coords."""
    lum = downsample(lum_full, ds)
    m = sun_mask(lum, k=k)
    comp, stats = largest_component(m)
    if comp is None:
        return None
    ys, xs = np.nonzero(comp)
    if len(xs) < 20:
        return None

    # boundary points of the component
    import cv2
    cnts, _ = cv2.findContours(comp.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    cnt = max(cnts, key=cv2.contourArea)
    bp = cnt.reshape(-1,2).astype(np.float64)          # x,y in ds coords

    rp = None if r_prior is None else r_prior/ds
    fit = ransac_circle(bp, r_prior=rp)
    if fit is None:
        cx, cy, r = kasa_fit(bp)
        inl = np.ones(len(bp), bool)
    else:
        cx, cy, r, inl = fit

    H, W = lum.shape
    # clipped if the component touches the frame border
    touch = (xs.min() <= 1) or (ys.min() <= 1) or (xs.max() >= W-2) or (ys.max() >= H-2)
    coverage = float(inl.sum())/max(len(bp),1)

    if touch and rp is not None:
        # arc-only: keep radius fixed, solve centre geometrically
        pts = bp[inl] if inl.sum() >= 12 else bp
        cx, cy = fit_center_fixed_r(pts, rp, c0=(cx, cy))
        r = rp

    off = (ds-1)/2.0   # ds-block centre -> full-res pixel-centre coordinate
    return dict(cx=cx*ds+off, cy=cy*ds+off, r=r*ds, clipped=bool(touch),
                coverage=coverage, n_pix=int(comp.sum()),
                area_equiv_r=float(np.sqrt(comp.sum()/np.pi))*ds)

# ---------------------------------------------------------------- tone mapping
def tone_map_patch(lin, target_peak=1.0, shoulder=1.0, gamma=1/2.2):
    """Linear patch -> display-referred float 0..1 with a soft highlight shoulder.

    Goal: keep limb darkening and spot detail visible, never clip to flat white.
    """
    x = np.maximum(lin, 0.0) / max(target_peak, 1e-9)
    # filmic-ish shoulder: identity below knee, compresses above
    knee = 0.7*shoulder
    y = np.where(x <= knee, x,
                 knee + (1.0-knee)*(1.0 - np.exp(-(x-knee)/(1.0-knee))))
    y = np.clip(y, 0, 1)**gamma
    return y

# ------------------------------------------------- limb-darkening photometry
def limb_profile_samples(lin, cx, cy, r, ds=3, keep_frac=0.25):
    """Collect (mu, luminance) samples of the visible photosphere only.

    mu = sqrt(1 - (rho/R)^2) is the cosine of the heliocentric angle; surface
    brightness falls off towards the limb as roughly L(mu) = 1 - u*(1-mu^p).

    Samples are taken on a downsampled grid, restricted to the disk and above a
    brightness floor so that the moon silhouette (which is essentially black)
    does not enter the fit. Returns (mu, lum) arrays.
    """
    h, w, _ = lin.shape
    lum = to_lum_linear(lin)
    x0, x1 = int(max(0, cx-r)), int(min(w, cx+r))
    y0, y1 = int(max(0, cy-r)), int(min(h, cy+r))
    if x1-x0 < 16 or y1-y0 < 16: return None
    sub = lum[y0:y1, x0:x1]
    step = max(1, ds)
    sub = sub[::step, ::step]
    yy, xx = np.mgrid[y0:y1:step, x0:x1:step]
    rho = np.hypot(xx-cx, yy-cy)
    inside = rho < 0.97*r
    if inside.sum() < 50: return None
    vals = sub[inside]; rr = rho[inside]/r
    if vals.max() <= 0: return None
    photo = vals > keep_frac*vals.max()          # drop moon silhouette
    if photo.sum() < 40: return None
    mu = np.sqrt(np.clip(1-rr[photo]**2, 0, 1))
    return mu, vals[photo]

def fit_limb_darkening(samples, us=None, p=1.0):
    """Pick one limb-darkening coefficient for the whole sequence.

    Per-frame exposure is a nuisance parameter: for a given u we solve each frame
    analytically (A_i = median of I/L_u(mu)) and score the residual. Because the
    model is divided out, the resulting A_i is independent of eclipse phase --
    that is what kills the flicker at deep crescent.
    Returns (u_best, [A_i], rms).
    """
    if us is None: us = np.arange(0.0, 0.96, 0.02)
    best = None
    for u in us:
        amps, sq = [], 0.0; n = 0
        for s in samples:
            if s is None: amps.append(None); continue
            mu, I = s
            L = 1.0 - u*(1.0 - mu**p)
            L = np.maximum(L, 1e-3)
            A = float(np.median(I/L))
            amps.append(A)
            pred = A*L
            sq += float(np.sum((I-pred)**2)); n += len(I)
        if n == 0: continue
        rms = float(np.sqrt(sq/n))
        if best is None or rms < best[2]:
            best = (float(u), amps, rms)
    return best

# ------------------------------------------------- moon silhouette geometry
def detect_moon(lin, cx, cy, r, ds=4, rr_prior=1.0, rr_span=0.08,
                seed=None, span_d=1.6, step_d=0.08):
    """Locate the moon by forward-model matching instead of contour fitting.

    A thin crescent has almost no limb to fit, and contour fits return nonsense
    (e.g. r=0.19R for a 2% bite). So we do the inverse: for a grid of candidate
    moon offsets, synthesise the predicted bright mask and keep the offset whose
    prediction best matches what the sensor saw. The model is

        bright = inside_solar_disk  AND  outside_moon_disk

    which is exactly the physics, and it stays well conditioned at every phase
    because the *area* mismatch, not the edge, drives the score.
    """
    lum = to_lum_linear(lin)
    h, w = lum.shape
    x0, x1 = int(max(0, cx-r)), int(min(w, cx+r))
    y0, y1 = int(max(0, cy-r)), int(min(h, cy+r))
    if x1-x0 < 24 or y1-y0 < 24: return None
    sub = lum[y0:y1:ds, x0:x1:ds]
    yy, xx = np.mgrid[y0:y1:ds, x0:x1:ds].astype(np.float32)
    R = float(r)
    rho = np.hypot(xx-cx, yy-cy)
    inside = rho < 0.96*R
    px = int(inside.sum())
    if px < 200: return None
    vals = sub[inside]
    hi = float(np.percentile(vals, 95))
    if hi <= 0: return None
    obs = vals > 0.45*hi
    frac_bright = float(obs.mean())
    if frac_bright > 0.985:                       # no bite at all
        return dict(cx=None, cy=None, r=None, frac=0.0, bright_frac=1.0,
                    ok=False, score=1.0)

    sxc, syc = cx/1.0, cy/1.0
    iy, ix = np.nonzero(inside)
    ys = yy[iy, ix]; xs = xx[iy, ix]
    obs_f = obs.astype(np.float32)

    def score(dx, dy, rr):
        mx = sxc + dx*R; my = syc + dy*R
        d2 = (xs-mx)**2 + (ys-my)**2
        pred = d2 > (rr*R)**2
        inter = np.count_nonzero(pred & (obs_f > 0))
        union = np.count_nonzero(pred | (obs_f > 0))
        return inter/union if union else 0.0

    # A seeded search is far cheaper and far more reliable when neighbours
    # already bound where the moon must be: the eclipse only advances, so a
    # global grid can lock onto a locally-better but physically impossible fit.
    # NOTE: these grid arrays must NOT be called xs/ys -- those names already
    # hold the pixel coordinates inside this function, and shadowing them
    # silently produced a shape mismatch on the first call.
    if seed is not None:
        sdx, sdy = float(seed[0]), float(seed[1])
        grid_x = np.arange(sdx-span_d, sdx+span_d+1e-9, step_d)
        grid_y = np.arange(sdy-span_d, sdy+span_d+1e-9, step_d)
    else:
        sdx = sdy = 0.0
        grid_x = np.arange(-1.6, 1.61, 0.08)
        grid_y = grid_x
    best = None
    for rr in np.arange(rr_prior-rr_span, rr_prior+rr_span+1e-9, 0.04):
        for dx in grid_x:
            for dy in grid_y:
                sc = score(dx, dy, rr)
                if best is None or sc > best[0]:
                    best = (sc, dx, dy, rr)
    sc0, dx0, dy0, rr0 = best
    # local refinement
    for rr in np.arange(rr0-0.04, rr0+0.041, 0.02):
        for dx in np.arange(dx0-0.08, dx0+0.081, 0.02):
            for dy in np.arange(dy0-0.08, dy0+0.081, 0.02):
                sc = score(dx, dy, rr)
                if sc > best[0]: best = (sc, dx, dy, rr)
    sc, dx, dy, rr = best
    return dict(cx=sxc+dx*R, cy=syc+dy*R, r=rr*R,
                bright_frac=float(frac_bright),      # NOT coverage
                frac=float(frac_bright),             # legacy name, bright frac
                ok=True, score=float(sc))


def circle_overlap(R, r, d):
    """Area of intersection of two circles, as a fraction of the solar disk."""
    import math
    if d >= R + r: return 0.0
    if d <= abs(R - r):
        small, big = min(R, r), max(R, r)
        return (small/big)**2 if big > 0 else 0.0
    a1 = R*R*math.acos((d*d + R*R - r*r)/(2*d*R))
    a2 = r*r*math.acos((d*d + r*r - R*R)/(2*d*r))
    a3 = 0.5*math.sqrt(max(0.0, (-d+r+R)*(d+r-R)*(d-r+R)*(d+r+R)))
    return (a1 + a2 - a3)/(math.pi*R*R)

def eclipse_from_moon(sun_cx, sun_cy, sun_r, moon_cx, moon_cy, moon_r):
    """Relative moon offset and size, normalised by the solar radius."""
    dx = (moon_cx-sun_cx)/sun_r
    dy = (moon_cy-sun_cy)/sun_r
    rr = moon_r/sun_r
    d = (dx*dx + dy*dy)**0.5
    return dict(dx=dx, dy=dy, rr=rr,
                cover=circle_overlap(1.0, rr, d))
