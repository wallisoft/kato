"""Juska's app icon, from the committed artwork in packaging/art.

  make_icon.py out.ico          the Windows icon (chevron at 16-64 px, full art at 128-256)
  make_icon.py out.png [size]   a PNG at that size (default 512): chevron at 64 px and
                                below, full artwork above. Used for the Linux menu icon
                                and the Store tiles.

The tray icon is separate: a coloured status dot drawn by the app itself."""
import shutil
import sys
from pathlib import Path

from PIL import Image

art = Path(__file__).resolve().parent / "art"
out = sys.argv[1]
if out.endswith(".ico"):
    shutil.copy(art / "juska.ico", out)
else:
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 512
    src = art / ("juska-small-512.png" if size <= 64 else "juska-512.png")
    Image.open(src).resize((size, size), Image.LANCZOS).save(out)
