#!/usr/bin/env python3
"""Contact sheets of the cutout tiles, for visual review."""
import argparse, glob, json, os
from PIL import Image, ImageDraw, ImageFont

def font(sz):
    try: return ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', sz)
    except Exception: return ImageFont.load_default(size=sz)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default='out/cutouts')
    ap.add_argument('--out', default='out/cutouts/sheet')
    ap.add_argument('--cols', type=int, default=5)
    ap.add_argument('--rows', type=int, default=6)
    ap.add_argument('--cell', type=int, default=256)
    args = ap.parse_args()
    files = sorted(glob.glob(os.path.join(args.dir, '*.png')))
    per = args.cols*args.rows
    os.makedirs(args.out, exist_ok=True)
    n = 0
    for s in range(0, len(files), per):
        chunk = files[s:s+per]
        sheet = Image.new('RGB', (args.cols*args.cell, args.rows*args.cell), (12,12,12))
        d = ImageDraw.Draw(sheet)
        for k, f in enumerate(chunk):
            r, c = divmod(k, args.cols)
            im = Image.open(f).convert('RGB').resize((args.cell, args.cell), Image.LANCZOS)
            sheet.paste(im, (c*args.cell, r*args.cell))
            d.rectangle([c*args.cell, r*args.cell, (c+1)*args.cell-1, (r+1)*args.cell-1],
                        outline=(45,45,45))
        idx = s//per + 1
        p = os.path.join(args.out, f'sheet_{idx:02d}.png')
        sheet.save(p)
        print(f"{p}  ({len(chunk)} tiles, {chunk[0].split('/')[-1]} .. {chunk[-1].split('/')[-1]})")
        n += 1
    print(f"\n{n} sheets from {len(files)} tiles")

if __name__ == '__main__':
    main()
