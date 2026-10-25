#!/usr/bin/env python3
"""Final 4K video: real eclipse cutouts on a fully synthetic, continuous track.

Design decisions and why:

* MOTION IS SYNTHETIC. Where the Sun sat inside each source frame is just where
  the camera happened to be pointed; it carries no useful information and jitters
  from shot to shot. Using it produced the "rubberbanding" that David rejected.
  The track here is generated: one smooth, continuous, monotone move from
  top-left to bottom-right, with gentle easing only at the very start and end.
  No stops, no per-keyframe easing.

* CONTENT IS REAL. Each frame is the actual processed cutout for that moment of
  the eclipse, so the moon's advance is genuine. Nothing is drawn or invented.

* DISK IS PIXEL-NATIVE. Tiles are 2160 px with the disk at 648 px radius. They
  are composited at 1:1 into the 3840x2160 canvas, so the Sun is never resampled
  and its detail is exactly what came off the sensor.

* SHORT CROSS-DISSOLVE. With 152 stills over the runtime each one holds only a
  few frames. A dissolve confined to the tail of each slot smooths the moon's
  step without turning the Sun into a permanent double exposure.

* LABEL STRIP REMOVED. The review tiles carry a caption in the bottom 44 px.
  That band lies well outside the solar disk (which spans y 432-1728), so it is
  blanked to pure black -- lossless, no resampling of any real pixel.
"""
import argparse, json, os, subprocess, sys
import numpy as np
from PIL import Image

def load_tile(path, size, no_label_rows=44):
    im = Image.open(path).convert('RGB')
    a = np.asarray(im)
    if a.shape[0] != size:
        im = im.resize((size, size), Image.LANCZOS)
        a = np.asarray(im)
    # blank the caption band only; it never overlaps the disk
    if no_label_rows:
        a = a.copy()
        a[-no_label_rows:, :, :] = 0
    return a

def smoothstep(t):
    t = np.clip(t, 0.0, 1.0)
    return t*t*(3-2*t)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--meta', default='out/cutouts/meta.json')
    ap.add_argument('--tiles', default='out/cutouts')
    ap.add_argument('--excl', default=None,
                    help='JSON with a "drop" map; optional')
    ap.add_argument('--out', default='out/partial-eclipse-4k.mp4')
    ap.add_argument('--w', type=int, default=3840)
    ap.add_argument('--h', type=int, default=2160)
    ap.add_argument('--fps', type=int, default=30)
    ap.add_argument('--seconds', type=float, default=25.0)
    ap.add_argument('--crf', type=int, default=12)
    ap.add_argument('--preset', default='slow')
    ap.add_argument('--dissolve', type=float, default=0.40,
                    help='fraction of each slot spent dissolving to the next frame')
    ap.add_argument('--ease', type=float, default=0.18,
                    help='fraction of the run used to ease in and out')
    ap.add_argument('--start', default='0.215,0.375')
    ap.add_argument('--end', default='0.785,0.625')
    ap.add_argument('--scale', type=float, default=1.0,
                    help='tile scale; 1.0 keeps the disk pixel-native')
    ap.add_argument('--test-frames', type=int, default=0)
    args = ap.parse_args()

    ex = set()
    if args.excl and os.path.exists(args.excl):
        ex = set(json.load(open(args.excl)).get('drop', {}))
    meta = json.load(open(args.meta))
    frames = [f for f in meta['frames']
              if f['status'] == 'ok' and f['file'] not in ex]
    frames.sort(key=lambda f: f['file'])
    paths = []
    for f in frames:
        p = os.path.join(args.tiles, f"{f['i']:03d}_{f['file']}.png")
        if os.path.exists(p):
            paths.append(p)
    N = len(paths)
    if N < 2:
        print(f"need at least 2 tiles, found {N}"); return 2

    n_out = args.test_frames or int(round(args.seconds*args.fps))
    W, H = args.w, args.h
    R = meta['tile']*meta['diskfrac']*args.scale
    sx, sy = [float(v) for v in args.start.split(',')]
    ex_, ey_ = [float(v) for v in args.end.split(',')]
    print(f"{N} tiles -> {n_out} frames ({n_out/args.fps:.2f}s @ {args.fps}fps), "
          f"disk r={R:.0f}px, canvas {W}x{H}")

    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ff, '-hide_banner', '-loglevel', 'error', '-y',
           '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{W}x{H}',
           '-r', str(args.fps), '-i', '-',
           '-c:v', 'libx264', '-preset', args.preset, '-crf', str(args.crf),
           '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
           '-metadata', 'title=Partial eclipse composite',
           args.out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    cache = {}
    def tile(i):
        i = max(0, min(N-1, i))
        if i not in cache:
            if len(cache) > 6:
                cache.pop(next(iter(cache)))
            cache[i] = load_tile(paths[i], meta['tile'])
        return cache[i]

    tile_px = int(round(meta['tile']*args.scale))

    # ---- motion -----------------------------------------------------------
    # Pure synthetic track: constant speed through the middle, brief soft
    # ramps at the two ends only. No per-keyframe easing (that caused the
    # rubberbanding: velocity fell to zero at every shot boundary).
    ease = max(0.0, min(0.45, args.ease))
    def eased(u):
        if ease <= 0.0:
            return u
        v = 1.0/(1.0 - ease)                 # plateau speed, total area == 1
        if u < ease:                          # quadratic ramp up
            a = u/ease
            return 0.5*v*ease*a*a
        if u > 1.0 - ease:                    # quadratic ramp down
            b = (1.0 - u)/ease
            return 1.0 - 0.5*v*ease*b*b
        return 0.5*v*ease + v*(u - ease)

    for k in range(n_out):
        u = k/(n_out-1) if n_out > 1 else 0.0
        ue = eased(u)
        cx = (sx + (ex_-sx)*ue)*W
        cy = (sy + (ey_-sy)*ue)*H

        t = u*(N-1)
        i0 = int(np.floor(t))
        frac = t - i0
        i1 = min(N-1, i0+1)
        if args.dissolve > 0 and frac > 1.0-args.dissolve:
            a = smoothstep((frac-(1.0-args.dissolve))/args.dissolve)
        else:
            a = 0.0

        canvas = np.zeros((H, W, 3), np.uint8)
        sz = tile_px
        x0 = int(round(cx - sz/2)); y0 = int(round(cy - sz/2))
        x1, y1 = x0+sz, y0+sz
        sx0, sy0 = max(0, -x0), max(0, -y0)
        dx0, dy0 = max(0, x0), max(0, y0)
        dx1, dy1 = min(W, x1), min(H, y1)
        cw, ch = dx1-dx0, dy1-dy0
        if cw > 0 and ch > 0:
            src0 = tile(i0)[sy0:sy0+ch, sx0:sx0+cw].astype(np.float32)
            if a > 0:
                src1 = tile(i1)[sy0:sy0+ch, sx0:sx0+cw].astype(np.float32)
                # dissolve toward the next frame's content but keep the
                # composite additive over black, so no seam or darkening
                blended = src0*(1-a) + src1*a
                canvas[dy0:dy1, dx0:dx1] = np.clip(blended, 0, 255).astype(np.uint8)
            else:
                canvas[dy0:dy1, dx0:dx1] = src0.astype(np.uint8)
        proc.stdin.write(canvas.tobytes())
        if (k+1) % 60 == 0:
            print(f"  frame {k+1}/{n_out}", flush=True)

    proc.stdin.close()
    rc = proc.wait()
    print(f"ffmpeg exit {rc} -> {args.out}")
    return rc

if __name__ == '__main__':
    sys.exit(main())
