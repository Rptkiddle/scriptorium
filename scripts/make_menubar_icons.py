"""Generate the menubar status icons (outline hub-and-spoke glyph).

Writes src/scriptorium/assets/menubar-{green,amber,red}.png. Outline style, no
fill; colour carries the status (green all-synced, amber index behind Zotero,
red daemon down or Zotero closed). 44x44 px (22 pt at 2x), coloured — not
macOS template images, so they render identically in light and dark menubars.

Usage: uv run python scripts/make_menubar_icons.py
"""

import math
from pathlib import Path

from AppKit import NSBezierPath, NSBitmapImageRep, NSColor, NSImage, NSPNGFileType
from Foundation import NSMakeRect

SIZE = 44.0
HUB_R = 6.5
SPOKE_LEN = 16.0  # hub centre to node centre
NODE_R = 4.0
LINE_W = 2.5
ANGLES = (90, 210, 330)
# the 3-node glyph is taller above its hub than below; drop the hub centre so
# the visual bounding box sits centred in the canvas
CX = SIZE / 2
CY = SIZE / 2 - (SPOKE_LEN + NODE_R - (SPOKE_LEN * 0.5 + NODE_R)) / 2

COLORS = {
    "green": (48 / 255, 176 / 255, 80 / 255),
    "amber": (255 / 255, 159 / 255, 10 / 255),
    "red": (255 / 255, 69 / 255, 58 / 255),
}

ASSETS = Path(__file__).resolve().parents[1] / "src" / "scriptorium" / "assets"


def circle(x: float, y: float, r: float) -> NSBezierPath:
    p = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x - r, y - r, 2 * r, 2 * r))
    p.setLineWidth_(LINE_W)
    return p


def draw(rgb: tuple[float, float, float], out: Path) -> None:
    img = NSImage.alloc().initWithSize_((SIZE, SIZE))
    img.lockFocus()
    NSColor.colorWithSRGBRed_green_blue_alpha_(*rgb, 1.0).set()
    circle(CX, CY, HUB_R).stroke()
    for a in ANGLES:
        dx, dy = math.cos(math.radians(a)), math.sin(math.radians(a))
        spoke = NSBezierPath.bezierPath()
        spoke.moveToPoint_((CX + HUB_R * dx, CY + HUB_R * dy))
        spoke.lineToPoint_((CX + (SPOKE_LEN - NODE_R) * dx, CY + (SPOKE_LEN - NODE_R) * dy))
        spoke.setLineWidth_(LINE_W)
        spoke.stroke()
        circle(CX + SPOKE_LEN * dx, CY + SPOKE_LEN * dy, NODE_R).stroke()
    img.unlockFocus()

    rep = NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
    png = rep.representationUsingType_properties_(NSPNGFileType, None)
    png.writeToFile_atomically_(str(out), True)
    print(f"wrote {out}")


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    for name, rgb in COLORS.items():
        draw(rgb, ASSETS / f"menubar-{name}.png")


if __name__ == "__main__":
    main()
