"""Draws the Minideck icon (a monitor with a cursor) and writes icon.ico + icon.png."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024  # drawn large, then downscaled for smooth edges
HERE = Path(__file__).parent


def draw():
    # background: rounded square with a diagonal blue -> teal gradient
    grad = Image.new("RGB", (S, S))
    px = grad.load()
    top, bottom = (37, 99, 235), (13, 180, 170)
    for y in range(S):
        for x in range(S):
            t = (x + y) / (2 * S)
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(top, bottom))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((32, 32, S - 32, S - 32), radius=220, fill=255)
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    icon.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(icon)
    white = (255, 255, 255, 255)
    # monitor: frame, stand, base
    d.rounded_rectangle((190, 250, 834, 670), radius=56, outline=white, width=56)
    d.rectangle((476, 670, 548, 760), fill=white)
    d.rounded_rectangle((350, 750, 674, 806), radius=28, fill=white)
    # cursor arrow on the screen
    ox, oy, k = 430, 345, 2.3
    arrow = [(0, 0), (0, 100), (24, 78), (42, 118), (62, 109), (44, 70), (76, 70)]
    d.polygon([(ox + x * k, oy + y * k) for x, y in arrow], fill=white)
    return icon


if __name__ == "__main__":
    icon = draw()
    icon.resize((256, 256), Image.LANCZOS).save(HERE / "icon.png")
    icon.save(HERE / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    print("written", HERE / "icon.ico")
