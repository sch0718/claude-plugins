import json
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = "/System/Library/Fonts/AppleSDGothicNeo.ttc"


def font_at(size):
    for index in (6, 4, 0):
        try:
            return ImageFont.truetype(FONT, size, index=index)
        except OSError:
            continue
    raise OSError("자막 글꼴을 열 수 없다.")


def wrap(text, fits):
    lines = []
    line = ""
    for word in text.split():
        part = f"{line} {word}" if line else word
        if fits(part):
            line = part
            continue
        if line:
            lines.append(line)
        line = ""
        for char in word:
            if line and not fits(line + char):
                lines.append(line)
                line = ""
            line += char
    if line:
        lines.append(line)
    return lines


def draw_caption(job, layout):
    width, height = layout["w"], layout["h"]
    max_lines = layout["maxLines"]
    max_chars = layout["maxChars"] if width < height else float("inf")
    for size in range(layout["size"], layout["minSize"] - 1, -2):
        font = font_at(size)

        def fits(line):
            return len(line) <= max_chars and font.getlength(line) <= layout["maxWidth"]

        lines = wrap(job["text"], fits)
        if len(lines) <= layout["preferredLines"]:
            break
    if len(lines) > layout["preferredLines"]:
        print(f"자막이 {layout['preferredLines']}줄을 넘는다. {job['text'][:20]}", file=sys.stderr)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while not fits(lines[-1] + "…"):
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"

    # 글자 띠만 저장한다. 빌드에서 화면의 bottom 좌표에 아래 끝을 맞춘다.
    if not 0 <= layout["bottom"] < height:
        raise ValueError("자막 위치가 화면 밖이다.")
    band = max_lines * (layout["size"] + 12) + 9
    image = Image.new("RGBA", (width, band))
    if lines:
        draw = ImageDraw.Draw(image)
        line_height = size + 12
        bottom = band - 1
        top = bottom - len(lines) * line_height
        half_width = max(font.getlength(line) for line in lines) / 2 + 28
        draw.rounded_rectangle(
            (width / 2 - half_width, top - 4, width / 2 + half_width, bottom),
            radius=22, fill=(0, 0, 0, 115),
        )
        for index, line in enumerate(lines):
            draw.text(
                (width / 2, top + 7 + index * line_height), line,
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
    layout = json.loads(sys.argv[2])
    for job in json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")):
        draw_caption(job, layout)
