"""
Resize and re-encode images. General-purpose; not specific to any one doc.

Written because phone screenshots land at full sensor resolution (~1170x2532,
~350 KB each) and get embedded into generated HTML as base64, where they cost
roughly 1.34x their file size. Downscaling to the width they're actually
displayed at cuts page weight by an order of magnitude with no visible loss.

    # overwrite in place, cap the long edge at 1000px
    python tools/resize_image.py docs/images/*.jpg --max 1000

    # write elsewhere, tune quality
    python tools/resize_image.py in.jpg -o out.jpg --max 800 --quality 80

    # see what would happen, change nothing
    python tools/resize_image.py docs/images/*.jpg --max 1000 --dry-run

Only downscales -- an image already within the limit is re-encoded only if
that actually makes it smaller, so running this repeatedly is safe and
won't degrade an image through successive re-compression.
"""
import argparse
import io
import os
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required:  mamba install -n myfirstagent -c conda-forge pillow")

DEFAULT_MAX = 1000
DEFAULT_QUALITY = 82


def human(n):
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / (1024 * 1024):.1f} MB"


def resize_one(path, out_path, max_edge, quality, dry_run=False):
    """Returns (before_bytes, after_bytes, note)."""
    before = os.path.getsize(path)
    with Image.open(path) as img:
        img.load()
        w, h = img.size
        scale = min(1.0, max_edge / max(w, h))
        new_size = (round(w * scale), round(h * scale))

        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        if scale < 1.0:
            img = img.resize(new_size, Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        data = buf.getvalue()

    # never make a file bigger, and never silently re-compress for no gain
    if len(data) >= before and out_path == path:
        return before, before, f"{w}x{h} unchanged (already optimal)"

    note = f"{w}x{h} -> {new_size[0]}x{new_size[1]}"
    if not dry_run:
        with open(out_path, "wb") as fh:
            fh.write(data)
    return before, len(data), note


def main():
    ap = argparse.ArgumentParser(description="Downscale and re-encode images as JPEG.")
    ap.add_argument("paths", nargs="+", help="image files to process")
    ap.add_argument("-o", "--output", help="output path (single input only; default: in place)")
    ap.add_argument("--max", type=int, default=DEFAULT_MAX,
                    help=f"max long-edge pixels (default {DEFAULT_MAX})")
    ap.add_argument("--quality", type=int, default=DEFAULT_QUALITY,
                    help=f"JPEG quality 1-95 (default {DEFAULT_QUALITY})")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    if args.output and len(args.paths) > 1:
        sys.exit("--output only works with a single input file")

    total_before = total_after = 0
    for path in args.paths:
        if not os.path.isfile(path):
            print(f"  skip  {path} (not a file)")
            continue
        out = args.output or path
        before, after, note = resize_one(path, out, args.max, args.quality, args.dry_run)
        total_before += before
        total_after += after
        pct = (1 - after / before) * 100 if before else 0
        print(f"  {os.path.basename(path):<28} {human(before):>9} -> {human(after):>9} "
              f"({pct:+.0f}%)  {note}")

    if len(args.paths) > 1 and total_before:
        pct = (1 - total_after / total_before) * 100
        print(f"  {'TOTAL':<28} {human(total_before):>9} -> {human(total_after):>9} ({pct:+.0f}%)")
    if args.dry_run:
        print("\n(dry run -- nothing written)")


if __name__ == "__main__":
    main()
