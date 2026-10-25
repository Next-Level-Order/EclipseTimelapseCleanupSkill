#!/usr/bin/env python3
"""High-resolution eclipse cutouts with correct exposure and tone mapping.

Fixes over the first attempt:
  * RESOLUTION: tiles are full 1080 px with the disk at 30% of the tile, so a
    600 px source disk is downsampled ~1.85x instead of 3.9x.
  * DOUBLE GAMMA: one transfer function only (see tonemap.py).
  * GREY SKY: black point is anchored; empty sky maps to 0, not to 54/255.
  * IMPOSSIBLE GAINS: exposure comes from EXIF physics, E = t / N^2, which is
    exact and phase-independent. The previous photometric solve returned gains
    up to 2294x on thin crescents, where the only visible photosphere hugs the
    limb and the limb-darkening model breaks down.
  * NO MOON FIT: not needed for review tiles, and it was most of the runtime.

Streams: one frame in, one tile out, so progress is visible and memory is flat.
"""
import argparse, glob, json, os, subprocess, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import decode_linear
from sunlib import detect_sun, to_lum_linear
from tonemap import ToneParams, render

# Fitted across the whole sequence by fit_limb_darkening (see sunlib).
U_LIMB = 0.76

def find_exiftool():
    """Locate exiftool: PATH first, then the common userland unpack location."""
    import shutil
    for name in ('exiftool',):
        p = shutil.which(name)
        if p:
            return p, None
    local = os.path.expanduser('~/.local/opt/exiftool')
    exe = os.path.join(local, 'usr/bin/exiftool')
    lib = os.path.join(local, 'usr/share/perl5')
    if os.path.exists(exe):
        return exe, (lib if os.path.isdir(lib) else None)
    raise SystemExit(
        "exiftool not found. Install it (e.g. apt install libimage-exiftool-perl) "
        "or unpack the .deb to ~/.local/opt/exiftool. See README.")


def read_exposure(files):
    et, perllib = find_exiftool()
    env = dict(os.environ)
    if perllib:
        env['PERL5LIB'] = perllib
    out = subprocess.run([et, '-s3', '-T', '-ExposureTime', '-FNumber'] + files,
                         capture_output=True, text=True, env=env, timeout=900).stdout
    rows = []
    for line in out.strip().split('\n'):
        p = line.split('\t')
        try:
            def num(s):
                s = s.strip()
                return float(s.split('/')[0])/float(s.split('/')[1]) if '/' in s else float(s)
            t, N = num(p[0]), num(p[1])
            rows.append((t, N, t/(N*N)))
        except Exception:
            rows.append((None, None, None))
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default='frames', help='directory of RAW/JPEG stills')
    ap.add_argument('--out', default='out/cutouts')
    ap.add_argument('--exclusions', default=None,
                    help='JSON with a "drop" map of filenames to reasons')
    ap.add_argument('--radius-from', default=None,
                    help='earlier meta.json to borrow per-focal-length radii from')
    ap.add_argument('--tile', type=int, default=1080)
    ap.add_argument('--diskfrac', type=float, default=0.30)
    ap.add_argument('--target', type=float, default=0.52)
    ap.add_argument('--dehaze', type=float, default=60.0)
    ap.add_argument('--clarity', type=float, default=70.0)
    ap.add_argument('--texture', type=float, default=50.0)
    ap.add_argument('--contrast', type=float, default=46.0)
    ap.add_argument('--max-residual', type=float, default=2.0)
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()
    import cv2
    from PIL import Image, ImageDraw, ImageFont

    os.makedirs(args.out, exist_ok=True)
    exts = ('*.CR3', '*.CR2', '*.NEF', '*.ARW', '*.DNG', '*.RAF',
            '*.ORF', '*.RW2', '*.TIF', '*.TIFF', '*.PNG', '*.JPG', '*.JPEG')
    files = sorted(p for pat in exts
                   for p in glob.glob(os.path.join(args.dir, pat)))
    # honour reviewed exclusions (data-driven, nothing is deleted on disk)
    if args.exclusions and os.path.exists(args.exclusions):
        drop = set(json.load(open(args.exclusions)).get('drop', {}).keys())
        before = len(files)
        files = [f for f in files
                 if os.path.splitext(os.path.basename(f))[0] not in drop]
        print(f"excluded {before-len(files)} reviewed frames")
    if args.limit: files = files[:args.limit]
    # Radius prior per focal length, from frames whose limb is well constrained.
    # Zoom was changed mid-shoot (250/320/400 mm) but never within a group, so
    # the per-group median is the correct radius for a frame whose own fit is
    # unreliable because the Sun was drifting out of the frame.
    r_prior = {}
    prior_src = args.radius_from
    if prior_src and os.path.exists(prior_src):
        pm = json.load(open(prior_src))
        groups = {}
        for f in pm['frames']:
            if f['status'] != 'ok':
                continue
            if (not f['clipped']) and f['limb_cov'] > 0.85 and f.get('focal'):
                groups.setdefault(round(f['focal']), []).append(f['r'])
        for k, v in groups.items():
            r_prior[k] = float(np.median(v))
        print("radius priors: " + ", ".join(f"{k:.0f}mm={v:.0f}px"
                                           for k, v in sorted(r_prior.items())))

    ex = read_exposure(files)
    E = np.array([e if e else np.nan for _, _, e in ex], float)
    medE = float(np.nanmedian(E))
    print(f"{len(files)} files, EXIF exposure spread "
          f"{np.nanmax(E)/np.nanmin(E):.1f}x, median E={medE:.3e}")

    try:
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 34)
    except Exception:
        font = ImageFont.load_default(size=34)

    meta = []
    for i, path in enumerate(files):
        name = os.path.splitext(os.path.basename(path))[0]
        lin, _ = decode_linear(path)
        h, w, _ = lin.shape
        lum = to_lum_linear(lin)
        det = detect_sun(lum)
        if det is None:
            meta.append(dict(i=i, file=name, status='black',
                             max_lum=float(lum.max())))
            print(f"[{i:3d}] {name} black")
            del lin, lum
            continue

        cx, cy, r = det['cx'], det['cy'], det['r']
        # Trust the group radius over a clipped/low-coverage fit: a bogus r of
        # 1173px on a 500px-radius frame would render the Sun tiny in its tile.
        r_raw = r
        fnum = ex[i][1] if i < len(ex) and ex[i][1] else None
        if fnum and (det['clipped'] or det['coverage'] < 0.60):
            rp = r_prior.get(round(fnum))
            if rp and abs(r-rp)/rp > 0.06:
                r = rp
        e = E[i] if i < len(E) else np.nan
        g_exif = medE/e if (e and not np.isnan(e)) else 1.0

        # crop at the display resolution directly
        Rt = args.diskfrac*args.tile
        half = r*(args.tile/(2*Rt))
        pl = max(0, int(np.ceil(half-cx)))+1; pr = max(0, int(np.ceil(cx+half-w)))+1
        pt = max(0, int(np.ceil(half-cy)))+1; pb = max(0, int(np.ceil(cy+half-h)))+1
        big = cv2.copyMakeBorder(lin, pt, pb, pl, pr, cv2.BORDER_CONSTANT, value=(0,0,0))
        patch = cv2.getRectSubPix(big, (int(round(2*half)), int(round(2*half))),
                                  (cx+pl, cy+pt))
        del big, lin, lum
        patch = cv2.resize(patch, (args.tile, args.tile), interpolation=cv2.INTER_AREA)

        # ---- phase-aware anchor.
        # Anchoring at a constant 1.0 is wrong: EXIF gain only fixes RELATIVE
        # exposure, so the absolute level is arbitrary and the disk came out at
        # 51/255. Anchoring at a per-frame "lit sun median" is also wrong: at
        # deep crescent that median is limb photosphere, which is intrinsically
        # darker, so crescents brighten and the sequence flickers.
        # Correct: estimate the DISK-CENTRE brightness by dividing the observed
        # surface brightness by the limb-darkening law at each pixel's mu.
        gl = cv2.cvtColor(np.clip(patch, 0, 1), cv2.COLOR_RGB2GRAY)
        yy, xx = np.mgrid[0:args.tile, 0:args.tile]
        cc = args.tile/2
        rho = np.hypot(xx-cc, yy-cc)/max(Rt, 1e-6)
        disk = rho < 0.94
        sun = disk & (gl > 0.02*max(float(gl.max()), 1e-6))
        if sun.sum() > 200:
            mu = np.sqrt(np.clip(1-rho**2, 0.04, 1))
            L = np.maximum(1.0-U_LIMB*(1.0-mu), 0.15)
            # disk-centre equivalent brightness of the visible photosphere
            A_est = float(np.median(gl[sun]/L[sun]))
        else:
            A_est = 0.25
        gain = g_exif*1.0
        # anchor: put the estimated disk centre at the requested display level,
        # after the per-frame gain has been applied
        anchor = max(A_est*gain, 1e-9)

        tp = ToneParams(target_level=args.target, dehaze=args.dehaze,
                        clarity=args.clarity, texture=args.texture,
                        contrast=args.contrast)
        # The disk mask stops local-contrast operators from treating the solar
        # limb as an edge and ringing it.
        disk_mask = (rho < 0.99).astype(np.float32)
        img = render(patch, tp, gain=gain, anchor=anchor, disk_mask=disk_mask,
                     rho=(rho/0.99).astype(np.float32))

        im = Image.fromarray(img)
        d = ImageDraw.Draw(im)
        d.rectangle([0, args.tile-44, args.tile, args.tile], fill=(0,0,0))
        d.text((10, args.tile-40),
               f"{name}  f={ex[i][1]:.1f}  t=1/{1/ex[i][0]:.0f}"
               f"  g={gain:.2f}", fill=(150,255,150), font=font)
        im.save(os.path.join(args.out, f"{i:03d}_{name}.png"))

        meta.append(dict(i=i, file=name, status='ok', w=w, h=h,
                         cx=cx, cy=cy, r=r, limb_cov=det['coverage'],
                         clipped=det['clipped'], exp_t=ex[i][0], fnum=ex[i][1],
                         gain_exif=g_exif, A_est=A_est, gain=gain,
                         downscale=r/Rt, r_raw=r_raw))
        print(f"[{i:3d}] {name} r={r:6.1f} g={gain:5.2f} A={A_est:.3f} down={r/Rt:4.2f}x")

    json.dump(dict(tile=args.tile, diskfrac=args.diskfrac, target=args.target,
                   frames=meta), open(os.path.join(args.out,'meta.json'),'w'), indent=1)
    ok = [m for m in meta if m['status']=='ok']
    gs = np.array([m['gain'] for m in ok])
    print(f"\nwrote {len(ok)} tiles; gain {gs.min():.2f}..{gs.max():.2f}")

if __name__ == '__main__':
    main()
