import json
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
WIDTH, HEIGHT = 1920, 260
TEXT_WIDTH = 1600


def font_at(size):
    for index in (6, 4, 0):
        try:
            return ImageFont.truetype(FONT, size, index=index)
        except OSError:
            continue
    raise OSError("자막 글꼴을 열 수 없다.")


def wrap(text, font):
    lines = []
    line = ""
    for word in text.split():
        part = f"{line} {word}" if line else word
        if font.getlength(part) <= TEXT_WIDTH:
            line = part
            continue
        if line:
            lines.append(line)
        line = ""
        for char in word:
            if line and font.getlength(line + char) > TEXT_WIDTH:
                lines.append(line)
                line = ""
            line += char
    if line:
        lines.append(line)
    return lines


def draw_caption(job):
    for size in (52, 50, 48, 46, 44):
        font = font_at(size)
        lines = wrap(job["text"], font)
        if len(lines) <= 2:
            break
    if len(lines) > 2:
        print(f"자막이 두 줄을 넘음: {job['text'][:20]}", file=sys.stderr)
    if len(lines) > 3:
        lines = lines[:3]
        while font.getlength(lines[-1] + "…") > TEXT_WIDTH:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"

    image = Image.new("RGBA", (WIDTH, HEIGHT))
    if lines:
        draw = ImageDraw.Draw(image)
        line_height = size + 12
        bottom = HEIGHT - 60
        top = bottom - len(lines) * line_height
        half_width = max(font.getlength(line) for line in lines) / 2 + 28
        draw.rounded_rectangle(
            (WIDTH / 2 - half_width, top - 4, WIDTH / 2 + half_width, bottom),
            radius=22, fill=(0, 0, 0, 115),
        )
        for index, line in enumerate(lines):
            draw.text(
                (WIDTH / 2, top + 7 + index * line_height), line,
                font=font, anchor="mt", fill=job["color"], stroke_width=3, stroke_fill="black",
            )

    out = Path(job["out"])
    tmp = out.with_suffix(f".{os.getpid()}.tmp.png")
    try:
        image.save(tmp)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    for job in json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")):
        draw_caption(job)
