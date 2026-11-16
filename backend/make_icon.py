"""Generate the Argona F&O app icon (1024px PNG) matching the dashboard look:
dark rounded square, soft glow, a rising green equity line with candlesticks."""
from PIL import Image, ImageDraw
import math

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# rounded-square background with a subtle vertical gradient
radius = int(S * 0.22)
bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
bgd = ImageDraw.Draw(bg)
for y in range(S):
    t = y / S
    r = int(10 + 6 * (1 - t)); g = int(16 + 10 * (1 - t)); b = int(26 + 16 * (1 - t))
    bgd.line([(0, y), (S, y)], fill=(r, g, b, 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S, S], radius=radius, fill=255)
img.paste(bg, (0, 0), mask)
d = ImageDraw.Draw(img)

# faint inner border
d.rounded_rectangle([6, 6, S - 6, S - 6], radius=radius - 4,
                    outline=(60, 90, 140, 90), width=3)

# soft green glow behind the curve
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
pad = int(S * 0.16)
pts = [
    (pad, S * 0.70), (S * 0.33, S * 0.60), (S * 0.46, S * 0.66),
    (S * 0.60, S * 0.44), (S * 0.74, S * 0.50), (S - pad, S * 0.30),
]
for w, a in [(46, 40), (30, 70), (16, 120)]:
    gd.line(pts, fill=(74, 222, 128, a), width=w, joint="curve")
try:
    glow = glow.filter(__import__("PIL.ImageFilter", fromlist=["GaussianBlur"]).GaussianBlur(10))
except Exception:
    pass
img = Image.alpha_composite(img, glow)
d = ImageDraw.Draw(img)

# candlesticks under the line
def candle(cx, top, bot, up):
    col = (74, 222, 128, 255) if up else (248, 113, 113, 255)
    bw = int(S * 0.035)
    d.line([(cx, top - S * 0.05), (cx, bot + S * 0.05)], fill=col, width=5)
    d.rounded_rectangle([cx - bw, top, cx + bw, bot], radius=6, fill=col)

candle(S * 0.30, S * 0.58, S * 0.70, True)
candle(S * 0.46, S * 0.60, S * 0.69, False)
candle(S * 0.62, S * 0.42, S * 0.56, True)

# the crisp equity line + end node
d.line(pts, fill=(130, 240, 170, 255), width=14, joint="curve")
ex, ey = pts[-1]
d.ellipse([ex - 20, ey - 20, ex + 20, ey + 20], fill=(180, 255, 205, 255))

# up-arrow head at the end
d.polygon([(ex + 2, ey - 2), (ex - 40, ey + 6), (ex - 6, ey + 42)], fill=(74, 222, 128, 255))

img.save("/Users/krishnagoyal/ai-trading-os/backend/argona_icon.png")
print("icon png written")
