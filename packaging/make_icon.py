"""Draw AirShare's icon (a blue dot) as .ico or .png, depending on the file name given."""
import sys
from PIL import Image, ImageDraw

out = sys.argv[1]
im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
ImageDraw.Draw(im).ellipse((24, 24, 232, 232), fill=(59, 130, 246, 255), outline=(255, 255, 255, 220), width=12)
if out.endswith(".ico"):
    im.save(out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
else:
    im.save(out)
