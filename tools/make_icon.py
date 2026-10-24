"""Draws ai_renamer/assets/icon.ico (a document with an AI sparkle). Run once; the .ico is committed."""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "ai_renamer" / "assets" / "icon.ico"
S = 1024  # draw large, downscale for crisp small sizes


def sparkle(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill) -> None:
    k = r * 0.22
    d.polygon([(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
               (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k)], fill=fill)


def main() -> None:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # Background: rounded square with a vertical indigo -> violet gradient.
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    top, bottom = (79, 70, 229), (147, 51, 234)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=220, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    # Document with folded corner.
    x0, y0, x1, y1, fold = 270, 200, 700, 820, 140
    d.polygon([(x0, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1), (x0, y1)], fill=(255, 255, 255, 255))
    d.polygon([(x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold)], fill=(199, 210, 254, 255))
    # Text lines; the first is highlighted like a "new name".
    d.rounded_rectangle([x0 + 60, 380, x1 - 70, 430], radius=25, fill=(250, 204, 21, 255))
    for y in (500, 580, 660):
        d.rounded_rectangle([x0 + 60, y, x1 - 110 + (y % 3) * 20, y + 34], radius=17, fill=(196, 181, 253, 255))
    # Sparkles.
    sparkle(d, 760, 640, 170, (250, 204, 21, 255))
    sparkle(d, 860, 430, 80, (255, 255, 255, 255))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    img.resize((256, 256), Image.LANCZOS).save(OUT.with_suffix(".png"))
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
