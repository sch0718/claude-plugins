import os
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def texture(name, w, h):
    rng = random.Random(42)
    # 움직이는 방향으로 같은 무늬를 이어 붙인다.
    tile = Image.new("RGBA", (w + 64 if name == "snow" else w, h))
    draw = ImageDraw.Draw(tile)
    if name == "fog":
        for _ in range(52):
            x, y = rng.randrange(w), rng.randrange(h)
            rx, ry = rng.randint(50, 220), rng.randint(16, 65)
            color = (196, 207, 224, rng.randint(20, 85))
            for offset in (-w, 0, w):
                xx = x + offset
                draw.ellipse((xx - rx, y - ry, xx + rx, y + ry), fill=color)
        # 흐림 필터도 경계 너머의 무늬를 읽게 한다.
        tiled = Image.new("RGBA", (w * 3, h))
        for i in range(3):
            tiled.paste(tile, (i * w, 0))
        tile = tiled.filter(ImageFilter.GaussianBlur(30)).crop((w, 0, w * 2, h))
    else:
        count = w * h // (3600 if name == "rain" else 4900)
        for _ in range(count):
            x, y = rng.randrange(tile.width), rng.randrange(h)
            if name == "rain":
                length = rng.randint(16, 62)
                color = (170, 198, 231, rng.randint(35, 135))
                width = rng.randint(1, 2)
                for yy in (y - h, y):
                    draw.line((x, yy, x - length // 5, yy + length), fill=color, width=width)
            else:
                radius = rng.randint(1, 4)
                color = (242, 247, 255, rng.randint(55, 200))
                for yy in (y - h, y, y + h):
                    draw.ellipse((x - radius, yy - radius, x + radius, yy + radius), fill=color)

    dx, dy = (w, 0) if name == "fog" else (0, h)
    image = Image.new("RGBA", (tile.width + dx, h + dy))
    image.paste(tile, (0, 0))
    image.paste(tile, (dx, dy))
    return image


if __name__ == "__main__":
    name, w, h, out = sys.argv[1:]
    if name not in ("fog", "rain", "snow"):
        raise ValueError("모르는 화면 효과다.")
    dest = Path(out)
    tmp = dest.with_suffix(f".{os.getpid()}.tmp.png")
    try:
        texture(name, int(w), int(h)).save(tmp)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
