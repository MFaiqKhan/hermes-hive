"""Render a captured terminal step as a PNG that looks like a terminal window.

    python shot.py step.txt out.png
step.txt: first line is the window title; lines starting with "$ " are commands, "> " lines are what the user
typed into the chat, everything else is output. Hive's own hint lines are highlighted.
"""
import re
import sys
import textwrap

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/TTF/DejaVuSansMono.ttf"
BOLD = "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf"
SIZE, PAD, COLS, MAX_LINES = 17, 22, 112, 46
BG, BAR, FG, DIM = (24, 26, 33), (40, 43, 54), (220, 223, 228), (140, 146, 160)
GREEN, CYAN, YELLOW, PINK, RED = (126, 214, 135), (110, 200, 230), (240, 205, 110), (220, 150, 230), (240, 120, 120)
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def color(line: str):
    if line.startswith("$ "):
        return GREEN, True
    if line.startswith("> "):
        return PINK, True
    if line.startswith("Hermes: "):
        return FG, True
    if line.startswith("▶ terminal:"):
        return DIM, False
    if "Hive: this error has been fixed before" in line or line.startswith("── Hive"):
        return CYAN, True
    if "Suggested next step" in line:
        return YELLOW, True
    if re.search(r"\b(published|enabled|Installed|Validation passed|synced \d+ records|OK:)", line):
        return GREEN, False
    if re.search(r"error|Error|corrupt|missing", line):
        return RED, False
    if line.startswith(("#", "   (")):
        return DIM, False
    return FG, False


def main(src: str, dst: str) -> None:
    raw = ANSI.sub("", open(src, encoding="utf-8", errors="replace").read()).splitlines()
    title, body = (raw[0] if raw else "terminal"), raw[1:]
    lines = []
    for ln in body:
        ln = ln.replace("\t", "    ").rstrip()
        wrapped = textwrap.wrap(ln, COLS, subsequent_indent="    ", drop_whitespace=False) or [""]
        lines.extend(wrapped)
    if len(lines) > MAX_LINES:
        lines = lines[: MAX_LINES - 1] + [f"   ({len(lines) - MAX_LINES + 1} more lines)"]
    font, bold = ImageFont.truetype(FONT, SIZE), ImageFont.truetype(BOLD, SIZE)
    cw = font.getbbox("M")[2]
    lh = int(SIZE * 1.45)
    width = PAD * 2 + cw * max([COLS // 1, 60]) if lines else 900
    width = PAD * 2 + int(cw * 1.06 * min(COLS, max(60, max((len(x) for x in lines), default=60)))) + cw * 2
    height = 44 + PAD + lh * len(lines) + PAD
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, 34], fill=BAR)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([14 + i * 22, 11, 26 + i * 22, 23], fill=c)
    tw = bold.getbbox(title)[2]
    d.text(((width - tw) // 2, 8), title, font=bold, fill=FG)
    y = 44 + PAD // 2
    for ln in lines:
        col, is_bold = color(ln)
        d.text((PAD, y), ln, font=bold if is_bold else font, fill=col)
        y += lh
    img.save(dst)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
