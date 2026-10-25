#!/usr/bin/env python3
"""Eclipse shot -> canonical sun sprite -> shots.json (for Remotion).

Stages per file:
  1. decode to LINEAR sRGB float (rawpy for CR3, PIL for PNG/TIF)
  2. detect solar disk (limb circle fit, radius-constrained)
  3. measure photosphere level -> per-frame gain (flicker removal)
  4. crop square patch, resample so disk radius == canonical R
  5. identical tone curve for every frame  -> sprite PNG on pure black
Output: work/sprites/*.png + work/shots.json
"""
import argparse, json, os, sys, glob
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sunlib import detect_sun, to_lum_linear, limb_profile_samples, fit_limb_darkening

RAW_EXT = ('.cr3','.cr2','.nef','.arw','.dng','.raf','.orf','.rw2','.pef')
IMG_EXT = ('.png','.tif','.tiff','.jpg','.jpeg')

def decode_linear(path):
    """-> (H,W,3) float32 linear, in [0, ~1+] ; no auto brightness, camera WB."""
    ext = os.path.splitext(path)[1].lower()
    if ext in RAW_EXT:
        import rawpy
        with rawpy.imread(path) as raw:
            rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True,
                                  output_bps=16, gamma=(1,1),
                                  output_color=rawpy.ColorSpace.sRGB,
                                  highlight_mode=0)  # 0 = clip, keeps linearity honest
            # rawpy exposes camera identity only on some builds/versions; never
            # assume these attributes exist.
            raw_meta = dict(camera=str(getattr(raw, 'camera_manufacturer', '') or ''),
                            model=str(getattr(raw, 'camera_model', '') or ''))
        return rgb.astype(np.float32)/65535.0, raw_meta
    from PIL import Image
    im = Image.open(path)
    arr = np.asarray(im.convert('RGB')).astype(np.float32)/255.0
    # sRGB -> linear
    return np.where(arr<=0.04045, arr/12.92, ((arr+0.055)/1.055)**2.4).astype(np.float32), {}

def srgb_encode(x):
    x = np.clip(x,0,1)
    return np.where(x<=0.0031308, x*12.92, 1.055*np.power(x,1/2.4)-0.055)

def photosphere_level(lin, cx, cy, r, q=90.0):
    """Surface brightness of the *photosphere* only (not the moon, not sky).

    Pixels inside the disk are a mixture of photosphere and moon silhouette.
    Taking a low-percentile of the disk would lock onto the moon, and a very high
    percentile locks onto saturated cores. So: restrict to the disk, drop the
    dark (moon) half, then take p90 of what remains -> stable under any phase.
    Returns (level, sat_frac) where sat_frac is the clipped-highlight fraction.
    """
    h,w,_ = lin.shape
    x0,x1 = int(max(0,cx-r)), int(min(w,cx+r))
    y0,y1 = int(max(0,cy-r)), int(min(h,cy+r))
    sub = lin[y0:y1, x0:x1]
    if sub.size == 0: return None
    yy,xx = np.mgrid[y0:y1, x0:x1]
    inside = np.hypot(xx-cx, yy-cy) < r*0.92
    vals = sub[inside]
    if vals.size < 100: return None
    lum = to_lum_linear(vals.reshape(-1,1,3)).ravel()
    if lum.size < 100: return None
    lo, hi = np.percentile(lum, [40, 99.9])
    if hi - lo < 1e-6: return None
    photo = lum[lum > lo]            # photosphere side of the terminator
    # Median of the photosphere side is the robust "how bright is the surface"
    # statistic: it ignores the dark moon and is not dragged up by clipped pixels
    # unless more than half the visible disk is blown out.
    level = float(np.median(photo))
    sat_frac = float((lum > 0.985).mean())
    return level, sat_frac

def make_sprite(lin, cx, cy, r, R_out, gain, margin=1.45, shoulder=0.92):
    """Crop around disk, resample to R_out, apply common tone curve."""
    import cv2
    h,w,_ = lin.shape
    half_src = r*margin
    x0,x1 = cx-half_src, cx+half_src
    y0,y1 = cy-half_src, cy+half_src
    # pad so out-of-frame sun still yields a full square (black fill)
    pad = int(np.ceil(max(0,-x0,-y0,x1-w,y1-h)))+2
    big = np.zeros((h+2*pad, w+2*pad, 3), np.float32)
    big[pad:pad+h, pad:pad+w] = lin
    x0+=pad; x1+=pad; y0+=pad; y1+=pad; cx+=pad; cy+=pad
    patch = cv2.getRectSubPix(big, (int(round(2*half_src)), int(round(2*half_src))),
                             (float(cx), float(cy)), patch=None)
    side = patch.shape[0]
    scale = (R_out*margin*2)/side
    out_side = int(round(R_out*margin*2))
    patch = cv2.resize(patch, (out_side,out_side), interpolation=cv2.INTER_AREA)
    lin_out = patch.astype(np.float32)*gain
    # tone curve: normalize photosphere to 1.0 then filmic shoulder
    x = np.maximum(lin_out,0)/max(shoulder,1e-9)
    knee = 0.72
    y = np.where(x<=knee, x, knee+(1-knee)*(1-np.exp(-(x-knee)/(1-knee))))
    y = np.power(np.clip(y,0,1), 1/2.2)
    return (srgb_encode(y)*255).astype(np.uint8), out_side

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', required=True)
    ap.add_argument('--out', default='work')
    ap.add_argument('--radius', type=float, default=0.0625,
                    help='disk radius as fraction of canvas height')
    ap.add_argument('--canvas-h', type=int, default=2160)
    ap.add_argument('--max-gain', type=float, default=8.0)
    ap.add_argument('--list', action='store_true', help='only list metadata + timestamps')
    args = ap.parse_args()

    files = sorted([p for p in glob.glob(os.path.join(args.dir,'*'))
                    if os.path.splitext(p)[1].lower() in RAW_EXT+IMG_EXT])
    if not files:
        print(f"no supported images in {args.dir}"); return 1

    if args.list: return list_only(files)

    # ---------------- pass 1: decode once, cache luminance for detection
    lums = {}
    rows = []
    for i,path in enumerate(files):
        lin, meta = decode_linear(path)
        h,w,_ = lin.shape
        lums[i] = (to_lum_linear(lin), (h,w))
        rows.append(dict(i=i, file=os.path.basename(path), w=w, h=h,
                         portrait=bool(h>w), **meta, status='pending'))

    # ---------------- pass 2: detect; first without prior
    dets = {}
    for r in rows:
        det = detect_sun(lums[r['i']][0], r_prior=None)
        dets[r['i']] = det
        if det: r.update(cx=det['cx'], cy=det['cy'], r=det['r'],
                         clipped=det['clipped'], coverage=det['coverage'])

    # robust radius from frames that are NOT clipped and have good limb coverage
    good = [d['r'] for d in dets.values()
            if d and not d['clipped'] and d['coverage'] > 0.85]
    if len(good) < 3:
        good = [d['r'] for d in dets.values() if d and not d['clipped']]
    if not good:
        print("no clean full-disk frame found; cannot establish radius prior"); return 2
    r_prior = float(np.median(good))
    print(f"radius prior from {len(good)} clean frames: {r_prior:.2f}px\n")

    # ---------------- pass 3: re-detect clipped / low-coverage frames with prior
    for r in rows:
        d0 = dets[r['i']]
        need = d0 is None or d0['clipped'] or d0['coverage'] < 0.80
        if not need:
            continue
        d1 = detect_sun(lums[r['i']][0], r_prior=r_prior)
        if d1 and (d0 is None or abs(d1['r']-r_prior)/r_prior < 0.10):
            dets[r['i']] = d1
            r.update(cx=d1['cx'], cy=d1['cy'], r=d1['r'],
                     clipped=d1['clipped'], coverage=d1['coverage'])

    for r in rows:
        d = dets[r['i']]
        if d is None:
            r['status']='no_sun'
            print(f"[{r['i']:03d}] {r['file']:<30} NO SUN"); continue
        r['status']='ok'; r['r_rel'] = d['r']/r_prior
        print(f"[{r['i']:03d}] {r['file']:<30} c=({d['cx']:7.1f},{d['cy']:7.1f}) "
              f"r={d['r']:6.1f} ({r['r_rel']:.3f}x) clip={int(d['clipped'])} "
              f"cov={d['coverage']:.2f}")

    ok = [r for r in rows if r['status']=='ok']
    if not ok: print("nothing usable"); return 2

    # ---------------- exposure solve (limb-darkening model, whole sequence)
    # Measuring "brightness of the disk" per frame is wrong: at deep crescent the
    # surviving photosphere lies near the limb, which is intrinsically darker.
    # Fitting one limb-darkening law across all frames yields an exposure per
    # frame that is independent of eclipse phase -> true flicker removal.
    samples = {}
    for r in rows:
        if r['status'] != 'ok':
            continue
        d = dets[r['i']]
        lin, _ = decode_linear(os.path.join(args.dir, r['file']))
        samples[r['i']] = limb_profile_samples(lin, d['cx'], d['cy'], d['r'])
        pho = photosphere_level(lin, d['cx'], d['cy'], d['r'])
        if pho:
            r['pho_level'], r['sat_frac'] = pho

    idx_ok = [r['i'] for r in rows if r['status'] == 'ok']
    ordered = [samples.get(i) for i in idx_ok]
    u_best, amps, rms = fit_limb_darkening(ordered)
    print(f"\nlimb-darkening u={u_best:.2f}  model rms={rms:.5f} "
          f"({len([a for a in amps if a])}/{len(idx_ok)} frames)")

    for i, a in zip(idx_ok, amps):
        rows[i]['exposure'] = a
    exps = np.array([a for a in amps if a], dtype=float)
    ref = float(np.median(exps))
    print(f"exposure A: {exps.min():.5f}..{exps.max():.5f} "
          f"(spread {20*np.log10(exps.max()/exps.min()):.1f} dB)")

    # per-frame gain to normalise exposure; median-filter to remove fit noise
    # gain = 1/A : maps every frame onto the SAME absolute scale where the
    # photosphere at disk centre is 1.0. Using ref/A instead would preserve the
    # per-shot exposure ordering -- exactly the flicker we are removing.
    gs = {i: (1.0/a if a else None) for i, a in zip(idx_ok, amps)}
    for pos, i in enumerate(idx_ok):
        nb = [gs.get(idx_ok[k]) for k in range(max(0,pos-1), min(len(idx_ok), pos+2))]
        nb = [x for x in nb if x]
        rows[i]['gain'] = float(np.median(nb)) if nb else 1.0
        rows[i]['gain_smooth'] = rows[i]['gain']

    # ---------------- sprites
    os.makedirs(os.path.join(args.out,'sprites'), exist_ok=True)
    R_out = args.radius*args.canvas_h
    for r in rows:
        if r['status']!='ok':
            r['sprite']=None; continue
        d = dets[r['i']]
        lin,_ = decode_linear(os.path.join(args.dir, r['file']))
        png, side = make_sprite(lin, d['cx'], d['cy'], d['r'], R_out,
                                r['gain_smooth'], shoulder=1.0)
        name = f"sun_{r['i']:03d}.png"
        from PIL import Image
        Image.fromarray(png).save(os.path.join(args.out,'sprites',name))
        r['sprite'] = f"sprites/{name}"; r['sprite_side']=side

    meta = dict(canvas_h=args.canvas_h, canvas_w=int(round(args.canvas_h*16/9)),
                R_out=R_out, r_prior=r_prior, exposure_ref=ref,
                limb_u=u_best, limb_rms=rms,
                sprite_margin=1.45, n_files=len(files), n_ok=len(ok),
                shots=rows)
    out_json = os.path.join(args.out,'shots.json')
    with open(out_json,'w') as f:
        json.dump(meta, f, indent=1)
    print(f"\nwrote {out_json} ({len(ok)}/{len(files)} usable)")
    return 0


def list_only(files):
    """Print EXIF timestamps so shot spacing is known before any rendering."""
    import subprocess, datetime
    import shutil
    et = shutil.which('exiftool') or os.path.expanduser(
        '~/.local/opt/exiftool/usr/bin/exiftool')
    env = dict(os.environ)
    lib = os.path.expanduser('~/.local/opt/exiftool/usr/share/perl5')
    if os.path.isdir(lib):
        env['PERL5LIB'] = lib
    tags = ['-DateTimeOriginal','-SubSecTimeOriginal','-ExposureTime','-ISO','-FNumber',
            '-FocalLength','-Orientation','-ImageWidth','-ImageHeight']
    prev = None
    for p in files:
        try:
            out = subprocess.run([et,'-s3','-T']+tags+[p], capture_output=True,
                                 text=True, env=env, timeout=60).stdout.strip()
            f = out.split('\t')
        except Exception as e:
            print(f"{os.path.basename(p)}: exiftool failed {e}"); continue
        dto = f[0] if len(f)>0 else ''
        t = None
        try:
            t = datetime.datetime.strptime(dto, '%Y:%m:%d %H:%M:%S')
        except Exception:
            pass
        gap = f"{(t-prev).total_seconds():+7.1f}s" if (t and prev) else '      -'
        if t: prev = t
        print(f"{os.path.basename(p):<32} {dto:<20} {gap} "
              f"exp={f[2] if len(f)>2 else '':<9} iso={f[3] if len(f)>3 else '':<5} "
              f"f={f[4] if len(f)>4 else '':<5} fl={f[5] if len(f)>5 else '':<6} "
              f"orient={f[6] if len(f)>6 else ''}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
