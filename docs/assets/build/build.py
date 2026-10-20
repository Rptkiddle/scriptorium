"""Build the README artwork: banners, divider and the six skill figures.

Usage: python -I build.py BLACKLETTER.ttf OUT_DIR TEST_DIR ROMAN_VARIABLE.ttf
(build.sh downloads the fonts and runs this.) Writes banner-light.svg,
banner-dark.svg, divider.svg and dog-<skill>.svg to OUT_DIR, and
inspection SVGs to TEST_DIR. The lettering is outlined from
UnifrakturMaguntia and EB Garamond (SIL Open Font License).
"""
import math
import sys
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont

FONT_PATH, OUT_DIR, TEST_DIR, ROMAN_FONT = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
OUT_DIR.mkdir(parents=True, exist_ok=True)
TEST_DIR.mkdir(parents=True, exist_ok=True)

# Palette
LAPIS = "#1f4e9c"
VERMILION = "#b8321f"
VERDIGRIS = "#3f8f7a"
GOLD = "#d4a62a"
GOLD_DARK = "#8a6a12"
GOLD_LIGHT = "#f3d875"
INK = "#2b1d14"
VELLUM = "#f4ead5"
VELLUM_DARK = "#e3d3ae"
DARK_GROUND = "#121d35"
WHITE = "#fbf7ee"


def fmt(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------- geometry
def catmull_rom(points, samples_per_seg=40):
    """Sample a centripetal-ish Catmull-Rom spline through the points."""
    pts = [points[0]] + list(points) + [points[-1]]
    out = []
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        for k in range(samples_per_seg):
            t = k / samples_per_seg
            t2, t3 = t * t, t * t * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t
                       + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                       + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t
                       + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                       + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append((x, y))
    out.append(points[-1])
    return out


def arc_length_param(samples):
    acc = [0.0]
    for a, b in zip(samples, samples[1:]):
        acc.append(acc[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    total = acc[-1]
    return [a / total for a in acc]


def variable_stroke(samples, width_fn, cap_steps=14, cap="round", fork_depth=6):
    """Closed outline polygon for a centreline with half-width width_fn(s)."""
    s_vals = arc_length_param(samples)
    n = len(samples)
    left, right = [], []
    for i in range(n):
        a = samples[max(i - 1, 0)]
        b = samples[min(i + 1, n - 1)]
        tx, ty = b[0] - a[0], b[1] - a[1]
        L = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / L, tx / L
        w = width_fn(s_vals[i])
        px, py = samples[i]
        left.append((px + nx * w, py + ny * w))
        right.append((px - nx * w, py - ny * w))

    def cap(idx, direction):
        a = samples[max(idx - 1, 0)]
        b = samples[min(idx + 1, n - 1)]
        tx, ty = b[0] - a[0], b[1] - a[1]
        L = math.hypot(tx, ty) or 1.0
        tx, ty = tx / L * direction, ty / L * direction
        nx, ny = -ty, tx
        w = width_fn(s_vals[idx])
        px, py = samples[idx]
        if cap == "flat":
            return []
        if cap == "fork":
            # swallowtail: the end edge is notched back along the stroke
            return [(px - tx * fork_depth, py - ty * fork_depth)]
        pts = []
        for k in range(1, cap_steps):
            ang = math.pi * k / cap_steps
            # sweep from -normal side through tangent to +normal side
            cx = px + (-nx) * w * math.cos(ang) + tx * w * math.sin(ang)
            cy = py + (-ny) * w * math.cos(ang) + ty * w * math.sin(ang)
            pts.append((cx, cy))
        return pts

    # order: left side forward, end cap, right side backward, start cap
    poly = left + cap(n - 1, +1)[::-1] + right[::-1] + cap(0, -1)[::-1]
    return poly


def poly_path(poly):
    d = "M" + " L".join(f"{fmt(x)} {fmt(y)}" for x, y in poly) + " Z"
    return d


# ---------------------------------------------------------------- font word
def word_paths(font, text, scale, x0, baseline, fill):
    gs = font.getGlyphSet()
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    x = x0
    parts = []
    for ch in text:
        gname = cmap[ord(ch)]
        pen = SVGPathPen(gs)
        gs[gname].draw(pen)
        d = pen.getCommands()
        parts.append(
            f'<path fill="{fill}" transform="translate({fmt(x)} {fmt(baseline)}) '
            f'scale({scale:.5f} {-scale:.5f})" d="{d}"/>')
        x += hmtx[gname][0] * scale
    return "\n".join(parts), x


# ---------------------------------------------------------------- initial S
GOLD_MOTTLE_LIGHT = "#f0d26a"
GOLD_MOTTLE_DARK = "#b48a1c"
ULTRAMARINE = "#22357a"
ROMAN_S = {}  # filled in main(): {"d": path data, "bounds": (x0, y0, x1, y1)}


def load_roman_s(font_path, wght=800):
    """Outline the capital S of a variable Roman font at a heavy weight."""
    from fontTools.varLib import instancer
    from fontTools.pens.boundsPen import BoundsPen
    font = TTFont(font_path)
    inst = instancer.instantiateVariableFont(font, {"wght": wght})
    gs = inst.getGlyphSet()
    name = inst.getBestCmap()[ord("S")]
    pen = SVGPathPen(gs)
    gs[name].draw(pen)
    bp = BoundsPen(gs)
    gs[name].draw(bp)
    return {"d": pen.getCommands(), "bounds": bp.bounds}


def lcg(seed):
    state = seed

    def rnd():
        nonlocal state
        state = (state * 1103515245 + 12345) % 2147483648
        return state / 2147483648
    return rnd


def white_leaf(cx, cy, ang, length=13, width=5):
    d = (f"M0 0 Q {fmt(width / 2)} {fmt(-length * 0.4)} 0 {fmt(-length)} "
         f"Q {fmt(-width / 2)} {fmt(-length * 0.4)} 0 0 Z")
    return (f'<g transform="translate({fmt(cx)} {fmt(cy)}) rotate({fmt(ang)})">'
            f'<path d="{d}" fill="{WHITE}"/>'
            f'<path d="M0 -1.5 L0 {fmt(-length + 2)}" stroke="{ULTRAMARINE}" stroke-width="0.7" fill="none"/></g>')


def white_flower(cx, cy, r=3.6, centre=GOLD):
    out = []
    for k in range(5):
        a = -math.pi / 2 + k * 2 * math.pi / 5
        ex, ey = cx + r * 1.05 * math.cos(a), cy + r * 1.05 * math.sin(a)
        out.append(f'<ellipse cx="{fmt(ex)}" cy="{fmt(ey)}" rx="{r}" ry="{r * 0.75}" '
                   f'transform="rotate({fmt(math.degrees(a))} {fmt(ex)} {fmt(ey)})" fill="{WHITE}" stroke="{ULTRAMARINE}" stroke-width="0.5"/>')
    out.append(f'<circle cx="{fmt(cx)}" cy="{fmt(cy)}" r="1.8" fill="{centre}"/>')
    return "".join(out)


PEN_RND = lcg(11)


def penwork_stem(path_pts, leaves, flowers, buds=()):
    """A thin white stem (list of points) with leaves (t, side), flowers (t) and buds (t, side)."""
    def at(t):
        i = min(int(t * (len(path_pts) - 1)), len(path_pts) - 2)
        f = t * (len(path_pts) - 1) - i
        a, b = path_pts[i], path_pts[i + 1]
        px, py = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        tx, ty = b[0] - a[0], b[1] - a[1]
        L = math.hypot(tx, ty) or 1
        return px, py, tx / L, ty / L

    out = ['<path d="M' + " L".join(f"{fmt(a)} {fmt(b)}" for a, b in path_pts) +
           f'" fill="none" stroke="{WHITE}" stroke-width="1.4" stroke-linecap="round"/>']
    for t, side in leaves:
        px, py, tx, ty = at(t)
        ang = math.degrees(math.atan2(ty, tx)) + 90 + side * (40 + 30 * PEN_RND())
        length = 10 + 6 * PEN_RND()
        out.append(white_leaf(px, py, ang, length, length * 0.4))
        if PEN_RND() < 0.35:
            out.append(white_leaf(px, py, ang + side * 35, length * 0.75, length * 0.3))
    for t in flowers:
        px, py, tx, ty = at(t)
        out.append(white_flower(px, py))
    for t, side in buds:
        px, py, tx, ty = at(t)
        nx, ny = -ty, tx
        out.append(f'<path d="M {fmt(px)} {fmt(py)} L {fmt(px + nx * side * 5)} {fmt(py + ny * side * 5)}" stroke="{WHITE}" stroke-width="1.1" stroke-linecap="round"/>')
        out.append(f'<circle cx="{fmt(px + nx * side * 6)}" cy="{fmt(py + ny * side * 6)}" r="1.6" fill="{WHITE}"/>')
    return "".join(out)


def bezier_pts(p0, p1, p2, p3, steps=40):
    out = []
    for k in range(steps + 1):
        t = k / steps
        x = (1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t ** 2 * p2[0] + t ** 3 * p3[0]
        y = (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t ** 2 * p2[1] + t ** 3 * p3[1]
        out.append((x, y))
    return out


def alternating(count, start=0.08, end=0.9, first=1):
    return [(start + (end - start) * k / (count - 1), first if k % 2 == 0 else -first) for k in range(count)]


def corner_flourish(cx, cy, sx, sy):
    """Small white sprig with a curl and a trefoil, mirrored by sx, sy into each corner."""
    return (f'<g transform="translate({cx} {cy}) scale({sx} {sy})">'
            f'<path d="M 2 2 C 6 6 9 10 11 15 M 2 2 C 6 3 10 3 15 2" fill="none" stroke="{WHITE}" stroke-width="1.2" stroke-linecap="round"/>'
            f'<path d="M 7 8 C 12 6 14 8 12 12 C 10 14 8 12 9 10" fill="none" stroke="{WHITE}" stroke-width="1" stroke-linecap="round"/>'
            + white_leaf(11, 15, 200, 8, 3.2) + white_leaf(15, 2, 110, 8, 3.2) +
            f'<g fill="{WHITE}"><circle cx="17" cy="17" r="1.3"/><circle cx="14" cy="18.5" r="1.1"/><circle cx="18.5" cy="14" r="1.1"/></g>'
            f'</g>')


def lombardic_s(prefix, x, y, size, dark=False):
    """Gold Roman capital S (EB Garamond, weight 800) on an ultramarine square with white
    penwork foliage, after a 15th-century humanistic illuminated initial (user's reference)."""
    u = size / 200.0
    x0, y0, x1, y1 = ROMAN_S["bounds"]
    gh = y1 - y0
    s = 171 / gh                      # the S fills about 89% of the square, as in the reference
    sx = s * 1.06                     # the reference S is a little rounder than the type
    gw = (x1 - x0) * sx
    tx = 100 - gw / 2 - x0 * sx
    ty = 14 + 171 + y0 * s            # baseline so the glyph top sits at y = 14
    letter_tf = f"translate({fmt(tx)} {fmt(ty)}) scale({sx:.5f} {-s:.5f})"
    body = ROMAN_S["d"]

    # ---- penwork: stems with leaves, buds and five-petalled flowers (behind the letter)
    stems = []
    left = bezier_pts((22, 184), (14, 140), (26, 90), (32, 40))
    stems.append(penwork_stem(left, alternating(11, 0.06, 0.92, 1), [0.2, 0.5, 0.8], [(0.35, -1), (0.65, 1)]))
    left_low = bezier_pts((24, 150), (40, 148), (60, 146), (84, 140))
    stems.append(penwork_stem(left_low, alternating(5, 0.15, 0.8, -1), [0.97], [(0.5, 1)]))
    left_top = bezier_pts((30, 60), (44, 40), (62, 26), (96, 22))
    stems.append(penwork_stem(left_top, alternating(6, 0.12, 0.85, 1), [0.97], [(0.4, -1)]))
    right = bezier_pts((178, 16), (186, 60), (174, 130), (176, 184))
    stems.append(penwork_stem(right, alternating(11, 0.06, 0.92, -1), [0.22, 0.52, 0.82], [(0.37, 1), (0.67, -1)]))
    right_up = bezier_pts((176, 52), (160, 50), (140, 56), (118, 62))
    stems.append(penwork_stem(right_up, alternating(5, 0.15, 0.8, 1), [0.97], [(0.5, -1)]))
    right_low = bezier_pts((172, 150), (158, 164), (136, 178), (108, 182))
    stems.append(penwork_stem(right_low, alternating(6, 0.12, 0.85, -1), [0.97], [(0.4, 1)]))
    bottom = bezier_pts((30, 186), (50, 178), (70, 180), (88, 184))
    stems.append(penwork_stem(bottom, alternating(4, 0.2, 0.8, 1), [0.5]))
    top = bezier_pts((112, 18), (130, 14), (150, 16), (166, 24))
    stems.append(penwork_stem(top, alternating(4, 0.2, 0.8, -1), [0.5]))
    mid_l = bezier_pts((28, 112), (40, 106), (52, 104), (62, 108))
    stems.append(penwork_stem(mid_l, alternating(3, 0.3, 0.8, 1), [0.97]))
    mid_r = bezier_pts((174, 100), (162, 96), (152, 92), (146, 86))
    stems.append(penwork_stem(mid_r, alternating(3, 0.3, 0.8, -1), [0.97]))
    penwork = "".join(stems)

    rnd = lcg(7)
    dots = []
    for _ in range(300):
        dx, dy = 11 + rnd() * 178, 11 + rnd() * 178
        dots.append(f'<circle cx="{fmt(dx)}" cy="{fmt(dy)}" r="0.95"/>')
    dot_field = f'<g fill="{WHITE}" fill-opacity="0.9">{"".join(dots)}</g>'

    corners = (corner_flourish(12, 12, 1, 1) + corner_flourish(188, 12, -1, 1)
               + corner_flourish(12, 188, 1, -1) + corner_flourish(188, 188, -1, -1))

    def gold_sprig(cx, cy, ang):
        leaf = "M0 0 Q 3.2 -4 0 -11 Q -3.2 -4 0 0 Z"
        return (f'<g transform="translate({cx} {cy}) rotate({ang})" stroke="{INK}" stroke-width="0.8" stroke-linejoin="round">'
                f'<path d="M 0 0 L 0 -8 M 0 -4 L -5 -9 M 0 -4 L 5 -9" fill="none"/>'
                f'<path d="{leaf}" transform="translate(0 -8)" fill="{GOLD}"/>'
                f'<path d="{leaf}" transform="translate(-5 -9) rotate(-50)" fill="{GOLD}"/>'
                f'<path d="{leaf}" transform="translate(5 -9) rotate(50)" fill="{GOLD}"/>'
                f'</g>')
    accents = gold_sprig(164, 44, 30) + gold_sprig(38, 158, -150)

    patches = [(70, 60, 16, 9, -20, GOLD_MOTTLE_LIGHT), (120, 100, 20, 8, 25, GOLD_MOTTLE_DARK),
               (140, 150, 14, 9, -40, GOLD_MOTTLE_LIGHT), (90, 170, 18, 7, 10, GOLD_MOTTLE_DARK),
               (130, 44, 12, 7, 30, GOLD_MOTTLE_DARK), (60, 120, 10, 7, 0, GOLD_MOTTLE_LIGHT),
               (150, 120, 10, 6, -30, GOLD_MOTTLE_LIGHT), (100, 112, 12, 5, 20, GOLD_MOTTLE_LIGHT)]
    mottle = "".join(f'<ellipse cx="{a}" cy="{b}" rx="{c * 0.8:.1f}" ry="{d * 0.7:.1f}" transform="rotate({e} {a} {b})" fill="{f}" fill-opacity="0.28"/>'
                     for a, b, c, d, e, f in patches)

    edge = (f'  <rect x="-0.5" y="-0.5" width="201" height="201" fill="none" stroke="{VELLUM}" stroke-width="1"/>\n'
            if dark else "")
    g = (f'<g transform="translate({fmt(x)} {fmt(y)}) scale({u:.5f})">\n'
         f'{edge}'
         f'  <rect x="0" y="0" width="200" height="200" fill="{INK}"/>\n'
         f'  <rect x="4" y="4" width="192" height="192" fill="{ULTRAMARINE}"/>\n'
         f'  <rect x="7.5" y="7.5" width="185" height="185" fill="none" stroke="{WHITE}" stroke-width="1" stroke-opacity="0.85"/>\n'
         f'  {dot_field}\n'
         f'  {corners}\n'
         f'  {penwork}\n'
         f'  {accents}\n'
         f'  <clipPath id="{prefix}-letter-clip"><path transform="{letter_tf}" d="{body}"/></clipPath>\n'
         f'  <path transform="{letter_tf}" d="{body}" fill="url(#{prefix}-leaf)" stroke="{INK}" stroke-width="{1.7 / s:.2f}" stroke-linejoin="round"/>\n'
         f'  <g clip-path="url(#{prefix}-letter-clip)">{mottle}</g>\n'
         f'</g>')
    defs = (f'<linearGradient id="{prefix}-leaf" x1="0" y1="0" x2="1" y2="1">\n'
            f'    <stop offset="0" stop-color="#e8c65a"/>\n'
            f'    <stop offset="0.5" stop-color="{GOLD}"/>\n'
            f'    <stop offset="1" stop-color="#c49a22"/>\n'
            f'  </linearGradient>')
    return g, defs


# ---------------------------------------------------------------- vine
def ivy_leaf(cx, cy, ang, h=34, outline=INK, fill=VERDIGRIS, vein=None):
    vein = vein or outline
    """Three-lobed ivy leaf, base at the origin, pointing up (-y), rotated by ang."""
    k = h / 36.0
    d = ("M0 0 C 6 -4 14 -6 16 -14 C 18 -22 10 -23 8 -18 C 10 -26 4 -34 0 -36 "
         "C -4 -34 -10 -26 -8 -18 C -10 -23 -18 -22 -16 -14 C -14 -6 -6 -4 0 0 Z")
    return (f'<g transform="translate({fmt(cx)} {fmt(cy)}) rotate({fmt(ang)}) scale({k:.3f})">'
            f'<path d="{d}" fill="{fill}" stroke="{outline}" stroke-width="{2.2 / k:.2f}" stroke-linejoin="round"/>'
            f'<path d="M0 -4 L0 -30 M0 -14 L 8 -18 M0 -14 L -8 -18" fill="none" stroke="{vein}" '
            f'stroke-width="{1.5 / k:.2f}" stroke-linecap="round"/></g>')


def berry(cx, cy, r, outline):
    return (f'<circle cx="{fmt(cx)}" cy="{fmt(cy)}" r="{r}" fill="{VERMILION}" '
            f'stroke="{outline}" stroke-width="2"/>')


def berry_cluster(cx, cy, outline, r=5.5):
    return (berry(cx, cy - r * 1.1, r, outline) + berry(cx - r, cy + r * 0.7, r, outline)
            + berry(cx + r, cy + r * 0.7, r, outline))


def tendril(cx, cy, r, flip, outline):
    pts = []
    steps = 30
    for k in range(steps + 1):
        t = k / steps
        ang = t * 1.6 * 2 * math.pi
        rr = r * (1 - 0.8 * t)
        pts.append((cx + rr * math.cos(ang) * flip, cy + rr * math.sin(ang)))
    d = "M" + " L".join(f"{fmt(a)} {fmt(b)}" for a, b in pts)
    return f'<path d="{d}" fill="none" stroke="{outline}" stroke-width="2.2" stroke-linecap="round"/>'


def vine(outline, stem_colour):
    """Bar border: down the left margin from the initial, then a wavy spray along the bottom."""
    parts = []
    y0 = 294
    # wavy bottom stem
    xs = list(range(64, 1220, 104))
    stem = "M 40 60 L 40 268 Q 40 294 64 294"
    amp = 7
    for i in range(len(xs) - 1):
        xa, xb = xs[i], xs[i + 1]
        side = -1 if i % 2 == 0 else 1
        stem += f" Q {fmt((xa + xb) / 2)} {fmt(y0 + side * 2 * amp)} {xb} {y0}"
    parts.append(f'<path d="{stem}" fill="none" stroke="{outline}" stroke-width="7" stroke-linecap="round"/>')
    parts.append(f'<path d="{stem}" fill="none" stroke="{stem_colour}" stroke-width="3.5" stroke-linecap="round"/>')
    # elements at the crest of each wave
    for i in range(len(xs) - 1):
        xa, xb = xs[i], xs[i + 1]
        side = -1 if i % 2 == 0 else 1
        cx = (xa + xb) / 2
        cy = y0 + side * amp
        if i % 3 == 1:
            parts.append(berry_cluster(cx + 6, cy + side * 12, outline))
        else:
            ang = -62 if side < 0 else 118
            parts.append(ivy_leaf(cx, cy, ang, 32, outline))
            if i % 3 == 0:
                parts.append(tendril(cx + 38, cy + side * 9, 8, -1 if side < 0 else 1, outline))
    # left margin: alternating leaves and berries
    ys = list(range(96, 270, 46))
    for i, y in enumerate(ys):
        side = 1 if i % 2 == 0 else -1
        if i % 2 == 1:
            parts.append(berry_cluster(40 + side * 13, y, outline, 5))
        else:
            parts.append(ivy_leaf(40 + side * 2, y, -48 * side, 28, outline))
    # finial at the top of the stem
    parts.append(ivy_leaf(40, 64, 0, 30, outline))
    # right margin: the bottom run turns up the right side, mirroring the left
    xr = 1240
    stem_r = f"M {xs[-1]} {y0} Q {xr} {y0} {xr} 268 L {xr} 60"
    parts.append(f'<path d="{stem_r}" fill="none" stroke="{outline}" stroke-width="7" stroke-linecap="round"/>')
    parts.append(f'<path d="{stem_r}" fill="none" stroke="{stem_colour}" stroke-width="3.5" stroke-linecap="round"/>')
    for i, y in enumerate(ys):
        side = -1 if i % 2 == 0 else 1
        if i % 2 == 1:
            parts.append(berry_cluster(xr + side * 13, y, outline, 5))
        else:
            parts.append(ivy_leaf(xr + side * 2, y, -48 * side, 28, outline))
    parts.append(ivy_leaf(xr, 64, 0, 30, outline, GOLD))
    return "\n".join(parts)


# ---------------------------------------------------------------- banner
def banner(font, dark):
    prefix = "sb-dark" if dark else "sb-light"
    ground = DARK_GROUND if dark else VELLUM
    letter = VELLUM if dark else INK
    outline = GOLD if dark else INK
    stem_colour = VERDIGRIS

    block_x, block_y, block_size = 72, 32, 232
    init_g, init_defs = lombardic_s(prefix, block_x, block_y, block_size, dark=dark)

    upm = font["head"].unitsPerEm
    # size the word: cap the width to the room right of the initial
    text = "criptorium"
    _, adv = word_paths(font, text, 1.0, 0, 0, letter)
    target_w = 1176 - (block_x + block_size + 18)
    scale = min(target_w / adv, 215 / upm)
    baseline = block_y + block_size - 48
    word, end_x = word_paths(font, text, scale, block_x + block_size + 18, baseline, letter)

    mottle = ""

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="320" viewBox="0 0 1280 320">
<defs>
  {init_defs}
</defs>
<rect width="1280" height="320" fill="{ground}"/>
{mottle}
<g id="{prefix}-vine">
{vine(outline, stem_colour)}
</g>
{init_g}
<g id="{prefix}-word">
{word}
</g>
</svg>
'''
    return svg, end_x


# ---------------------------------------------------------------- dog
TAN = "#dcb75f"
COAT = "#fbf5e6"
O = INK


def roundel(night=False):
    disc = LAPIS if night else VELLUM
    return (f'<circle cx="100" cy="100" r="96" fill="{disc}" stroke="{GOLD_DARK}" stroke-width="1.5"/>'
            f'<circle cx="100" cy="100" r="92" fill="none" stroke="{GOLD}" stroke-width="5"/>'
            f'<circle cx="100" cy="100" r="88.5" fill="none" stroke="{GOLD_DARK}" stroke-width="1.2"/>')


def ground_line(x1, x2, y=160, tufts=True):
    s = f'<path d="M {x1} {y} L {x2} {y}" stroke="{VERDIGRIS}" stroke-width="3" stroke-linecap="round"/>'
    if tufts:
        s += (f'<path d="M {x1 + 6} {y} l 3 -7 M {x1 + 12} {y} l -2 -6 M {x2 - 8} {y} l -3 -7 M {x2 - 14} {y} l 2 -6" '
              f'stroke="{VERDIGRIS}" stroke-width="2.2" stroke-linecap="round" fill="none"/>')
    return s


def plume(centre, tan_from=0.5, width_scale=1.0):
    """Scalloped tail plume along a centreline (list of points)."""
    samples = catmull_rom(centre, 40)

    def w(s):
        return (3 + 7 * math.sin(math.pi * (0.15 + 0.85 * s)) ** 0.6
                + 1.6 * abs(math.sin(s * math.pi * 7))) * width_scale

    out = (f'<path d="{poly_path(variable_stroke(samples, w))}" fill="{COAT}" stroke="{O}" '
           f'stroke-width="2.6" stroke-linejoin="round"/>')
    tip = samples[int(len(samples) * tan_from):]
    out += (f'<path d="{poly_path(variable_stroke(tip, lambda s: (1.5 + 4.5 * math.sin(math.pi * s) ** 0.5) * width_scale))}" '
            f'fill="{TAN}"/>')
    return out


def paw(x, y, lifted=False, dx=-7, dy=-7):
    if lifted:
        x, y = x + dx, y + dy
    return (f'<path d="M {x - 8} {y - 8} L {x - 8} {y} q 0 7 8 7 q 8 0 8 -7 L {x + 8} {y - 8} Z" '
            f'fill="{COAT}" stroke="{O}" stroke-width="2.6" stroke-linejoin="round"/>')


def body_trot(ox=0, oy=0):
    """Standing/trotting body facing left, in the reference frame of dog-retrieve (head at 60,98)."""
    parts = [plume([(150, 114), (160, 100), (160, 80), (146, 66), (128, 64), (116, 72)])]
    for d in ("M 157 100 C 158 90 154 78 146 72", "M 150 108 C 152 96 148 84 140 78"):
        parts.append(f'<path d="{d}" fill="none" stroke="{O}" stroke-width="1.8" stroke-linecap="round"/>')
    parts.append(paw(94, 153))
    parts.append(paw(122, 153, True))
    parts.append(paw(78, 153, True))
    parts.append(paw(142, 153))
    body = ("M 72 104 C 76 86 96 78 120 80 C 144 82 156 94 156 116 "
            "C 157 132 156 142 154 150 "
            "q -7 7 -14 0 q -7 7 -14 0 q -7 7 -14 0 q -7 7 -14 0 q -7 7 -14 0 q -7 7 -14 0 "
            "C 68 136 70 118 72 104 Z")
    parts.append(f'<path d="{body}" fill="{COAT}" stroke="{O}" stroke-width="3" stroke-linejoin="round"/>')
    parts.append(f'<path d="M 112 80 C 128 78 146 84 154 104 C 155 124 148 140 134 148 '
                 f'C 126 140 120 128 118 112 C 116 100 112 90 112 80 Z" fill="{TAN}"/>')
    for d in ("M 94 82 C 88 102 92 126 86 146", "M 118 80 C 114 102 120 126 114 148",
              "M 140 86 C 138 106 144 128 138 148"):
        parts.append(f'<path d="{d}" fill="none" stroke="{O}" stroke-width="2.2" stroke-linecap="round"/>')
    return f'<g transform="translate({ox} {oy})">' + "".join(parts) + "</g>"


def body_sit(ox=0, oy=0, paws=True):
    """Seated body facing the viewer; head goes at (100, 96) in this frame."""
    parts = [plume([(122, 148), (138, 140), (148, 122), (144, 104), (132, 98)], tan_from=0.55, width_scale=0.9)]
    parts.append(f'<path d="M 146 128 C 146 118 142 108 134 102" fill="none" stroke="{O}" stroke-width="1.8" stroke-linecap="round"/>')
    if paws:
        parts.append(paw(88, 154))
        parts.append(paw(112, 154))
    body = ("M 66 152 C 64 126 76 100 100 98 C 124 100 136 126 134 152 "
            "q -7 7 -14 0 q -7 7 -14 0 q -7 7 -14 0 q -7 7 -14 0 q -5 7 -12 0 Z")
    parts.append(f'<path d="{body}" fill="{COAT}" stroke="{O}" stroke-width="3" stroke-linejoin="round"/>')
    parts.append(f'<path d="M 112 102 C 124 108 134 126 133 150 C 126 146 118 142 116 134 C 114 122 112 112 112 102 Z" fill="{TAN}"/>')
    for d in ("M 80 116 C 76 130 78 142 76 152", "M 118 112 C 120 128 118 142 120 152"):
        parts.append(f'<path d="{d}" fill="none" stroke="{O}" stroke-width="2.2" stroke-linecap="round"/>')
    return f'<g transform="translate({ox} {oy})">' + "".join(parts) + "</g>"


def head(hx, hy, rot=0, spectacles=False, beard=True):
    parts = []

    def ear(sign):
        s = sign
        return (f"M {hx + s * 14} {hy - 20} C {hx + s * 34} {hy - 14} {hx + s * 39} {hy + 10} {hx + s * 35} {hy + 24} "
                f"C {hx + s * 34} {hy + 32} {hx + s * 31} {hy + 38} {hx + s * 25} {hy + 43} "
                f"C {hx + s * 21} {hy + 34} {hx + s * 21} {hy + 26} {hx + s * 22} {hy + 18} "
                f"C {hx + s * 24} {hy + 6} {hx + s * 20} {hy - 6} {hx + s * 14} {hy - 20} Z")
    for s in (-1, 1):
        parts.append(f'<path d="{ear(s)}" fill="{TAN}" stroke="{O}" stroke-width="3" stroke-linejoin="round"/>')
        parts.append(f'<path d="M {hx + s * 26} {hy - 4} C {hx + s * 30} {hy + 10} {hx + s * 30} {hy + 22} {hx + s * 27} {hy + 34}" '
                     f'fill="none" stroke="{O}" stroke-width="2" stroke-linecap="round"/>')
    parts.append(f'<ellipse cx="{hx}" cy="{hy}" rx="26" ry="24" fill="{COAT}" stroke="{O}" stroke-width="3"/>')
    tuft = (f"M {hx - 11} {hy - 20} C {hx - 13} {hy - 32} {hx - 6} {hy - 42} {hx} {hy - 45} "
            f"C {hx + 6} {hy - 42} {hx + 13} {hy - 32} {hx + 11} {hy - 20} Z")
    parts.append(f'<path d="{tuft}" fill="{COAT}" stroke="{O}" stroke-width="2.6" stroke-linejoin="round"/>')
    bx, by = hx, hy - 31
    bow = (f"M {bx} {by} L {bx - 12} {by - 7} Q {bx - 14} {by} {bx - 12} {by + 7} Z "
           f"M {bx} {by} L {bx + 12} {by - 7} Q {bx + 14} {by} {bx + 12} {by + 7} Z")
    parts.append(f'<path d="{bow}" fill="{VERMILION}" stroke="{O}" stroke-width="2" stroke-linejoin="round"/>')
    parts.append(f'<circle cx="{bx}" cy="{by}" r="3" fill="{VERMILION}" stroke="{O}" stroke-width="1.6"/>')
    parts.append(f'<path d="M {hx - 3} {hy - 22} C {hx - 12} {hy - 18} {hx - 18} {hy - 12} {hx - 20} {hy - 4}" fill="none" stroke="{O}" stroke-width="2" stroke-linecap="round"/>')
    parts.append(f'<path d="M {hx + 3} {hy - 22} C {hx + 12} {hy - 18} {hx + 18} {hy - 12} {hx + 20} {hy - 4}" fill="none" stroke="{O}" stroke-width="2" stroke-linecap="round"/>')
    for ex in (hx - 10, hx + 10):
        parts.append(f'<circle cx="{ex}" cy="{hy - 3}" r="5.2" fill="{O}"/>')
        parts.append(f'<circle cx="{ex + 1.8}" cy="{hy - 4.8}" r="1.8" fill="{WHITE}"/>')
    if spectacles:
        # rivet spectacles: two rings joined at a rivet on the bridge
        for ex in (hx - 10, hx + 10):
            parts.append(f'<circle cx="{ex}" cy="{hy - 3}" r="8" fill="none" stroke="{O}" stroke-width="4.4"/>')
            parts.append(f'<circle cx="{ex}" cy="{hy - 3}" r="8" fill="none" stroke="{GOLD}" stroke-width="2.2"/>')
        parts.append(f'<path d="M {hx - 4} {hy - 9} L {hx} {hy - 13} L {hx + 4} {hy - 9}" fill="none" stroke="{O}" stroke-width="4"/>')
        parts.append(f'<path d="M {hx - 4} {hy - 9} L {hx} {hy - 13} L {hx + 4} {hy - 9}" fill="none" stroke="{GOLD}" stroke-width="2"/>')
        parts.append(f'<circle cx="{hx}" cy="{hy - 13}" r="2" fill="{O}"/>')
    parts.append(f'<path d="M {hx - 6.5} {hy + 5} L {hx + 6.5} {hy + 5} Q {hx + 5.5} {hy + 13} {hx} {hy + 14.5} '
                 f'Q {hx - 5.5} {hy + 13} {hx - 6.5} {hy + 5} Z" fill="{O}"/>')
    if beard:
        bd = (f"M {hx - 19} {hy + 10} C {hx - 23} {hy + 26} {hx - 20} {hy + 40} {hx - 14} {hy + 46} "
              f"C {hx - 11} {hy + 42} {hx - 8} {hy + 42} {hx - 5} {hy + 48} "
              f"C {hx - 2} {hy + 43} {hx + 2} {hy + 43} {hx + 5} {hy + 48} "
              f"C {hx + 8} {hy + 42} {hx + 11} {hy + 42} {hx + 14} {hy + 46} "
              f"C {hx + 20} {hy + 40} {hx + 23} {hy + 26} {hx + 19} {hy + 10} Z")
        parts.append(f'<path d="{bd}" fill="{COAT}" stroke="{O}" stroke-width="2.6" stroke-linejoin="round"/>')
        parts.append(f'<path d="M {hx - 10} {hy + 30} C {hx - 11} {hy + 36} {hx - 10} {hy + 40} {hx - 9} {hy + 43} '
                     f'M {hx + 10} {hy + 30} C {hx + 11} {hy + 36} {hx + 10} {hy + 40} {hx + 9} {hy + 43}" '
                     f'fill="none" stroke="{O}" stroke-width="1.8" stroke-linecap="round"/>')
    parts.append(f'<path d="M {hx - 8} {hy + 16} Q {hx} {hy + 19} {hx + 8} {hy + 16}" stroke="{O}" stroke-width="2.6" stroke-linecap="round" fill="none"/>')
    return f'<g transform="rotate({rot} {hx} {hy})">' + "".join(parts) + "</g>"


def mouth_grip(hx, hy, rot=0):
    """A short jaw stroke drawn over a held object so it reads as gripped."""
    return (f'<g transform="rotate({rot} {hx} {hy})"><path d="M {hx - 7} {hy + 17} Q {hx} {hy + 21} {hx + 7} {hy + 17}" '
            f'stroke="{O}" stroke-width="2.8" stroke-linecap="round" fill="none"/></g>')


def scroll(cx, cy, length=56, rot=0):
    h = length / 2
    parts = [f'<rect x="{-h}" y="-6.5" width="{length}" height="13" rx="2" fill="{WHITE}" stroke="{O}" stroke-width="2.6"/>',
             f'<path d="M {-h + 12} -1 L {h - 20} -1 M {-h + 12} 3 L {h - 14} 3" stroke="{O}" stroke-width="1.4" stroke-linecap="round" fill="none" stroke-opacity="0.8"/>']
    for ex in (-h, h):
        parts.append(f'<circle cx="{ex}" cy="0" r="7.5" fill="{VELLUM_DARK}" stroke="{O}" stroke-width="2.6"/>')
        parts.append(f'<circle cx="{ex}" cy="0" r="2.8" fill="none" stroke="{O}" stroke-width="2"/>')
    parts.append(f'<rect x="{h - 24}" y="-8" width="7" height="16" fill="{VERMILION}" stroke="{O}" stroke-width="1.6"/>')
    return f'<g transform="translate({cx} {cy}) rotate({rot})">' + "".join(parts) + "</g>"


def book_closed(cx, cy, w=30, h=22, rot=0, cover=LAPIS):
    """Closed book seen from the front cover, spine on the left."""
    parts = [
        f'<rect x="{-w / 2 + 3}" y="{-h / 2 + 2}" width="{w}" height="{h}" rx="1.5" fill="{VELLUM_DARK}" stroke="{O}" stroke-width="2.2"/>',
        f'<rect x="{-w / 2}" y="{-h / 2}" width="{w}" height="{h}" rx="1.5" fill="{cover}" stroke="{O}" stroke-width="2.4"/>',
        f'<rect x="{-w / 2 + 4}" y="{-h / 2 + 4}" width="{w - 8}" height="{h - 8}" fill="none" stroke="{GOLD}" stroke-width="1.6"/>',
        f'<rect x="{w / 2 - 6}" y="-3" width="7" height="6" fill="{GOLD}" stroke="{O}" stroke-width="1.4"/>',
    ]
    return f'<g transform="translate({cx} {cy}) rotate({rot})">' + "".join(parts) + "</g>"


def book_spine(x, y_base, w, h, colour, bands=True):
    """A book standing on a shelf, spine towards the viewer."""
    s = f'<rect x="{x}" y="{y_base - h}" width="{w}" height="{h}" rx="1.2" fill="{colour}" stroke="{O}" stroke-width="2.2"/>'
    if bands:
        s += (f'<path d="M {x + 2} {y_base - h + 6} L {x + w - 2} {y_base - h + 6} '
              f'M {x + 2} {y_base - 6} L {x + w - 2} {y_base - 6}" stroke="{GOLD}" stroke-width="1.8"/>')
    return s


def shelf_board(x1, x2, y, thick=5):
    return (f'<rect x="{x1}" y="{y}" width="{x2 - x1}" height="{thick}" fill="{GOLD_DARK}" stroke="{O}" stroke-width="2"/>')


def star(cx, cy, r=4):
    pts = []
    for k in range(8):
        ang = math.pi / 4 * k - math.pi / 2
        rr = r if k % 2 == 0 else r * 0.4
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    return f'<path d="{poly_path(pts)}" fill="{GOLD}" stroke="{GOLD_DARK}" stroke-width="0.8"/>'


def crescent(cx, cy, r=12):
    """Crescent by overpainting: a gold disc with an offset disc in the sky colour."""
    return (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{GOLD}" stroke="{GOLD_DARK}" stroke-width="1.2"/>'
            f'<circle cx="{cx + r * 0.45}" cy="{cy - r * 0.15}" r="{r * 0.85}" fill="{LAPIS}"/>')


def lantern(cx, cy):
    """Medieval horn lantern: gold frame, vermilion flame, ring handle. cy = base."""
    parts = [
        f'<circle cx="{cx}" cy="{cy - 16}" r="22" fill="{GOLD_LIGHT}" fill-opacity="0.22"/>',
        f'<path d="M {cx - 8} {cy} L {cx - 8} {cy - 28} L {cx + 8} {cy - 28} L {cx + 8} {cy} Z" fill="{VELLUM}" stroke="{O}" stroke-width="2.4" stroke-linejoin="round"/>',
        f'<path d="M {cx - 10} {cy - 28} L {cx} {cy - 36} L {cx + 10} {cy - 28} Z" fill="{GOLD}" stroke="{O}" stroke-width="2.2" stroke-linejoin="round"/>',
        f'<rect x="{cx - 10}" y="{cy - 2}" width="20" height="4" fill="{GOLD}" stroke="{O}" stroke-width="1.8"/>',
        f'<path d="M {cx} {cy - 6} C {cx - 6} {cy - 12} {cx - 3} {cy - 18} {cx} {cy - 24} C {cx + 3} {cy - 18} {cx + 6} {cy - 12} {cx} {cy - 6} Z" fill="{VERMILION}" stroke="{O}" stroke-width="1.6" stroke-linejoin="round"/>',
        f'<path d="M {cx} {cy - 9} C {cx - 2.5} {cy - 13} {cx - 1} {cy - 17} {cx} {cy - 19} C {cx + 1} {cy - 17} {cx + 2.5} {cy - 13} {cx} {cy - 9} Z" fill="{GOLD_LIGHT}"/>',
        f'<path d="M {cx - 8} {cy - 20} L {cx + 8} {cy - 20} M {cx - 8} {cy - 12} L {cx + 8} {cy - 12}" stroke="{O}" stroke-width="1.6" stroke-opacity="0.7"/>',
        f'<circle cx="{cx}" cy="{cy - 40}" r="4" fill="none" stroke="{O}" stroke-width="2.4"/>',
    ]
    return "".join(parts)


def open_book(cx, cy, w=72, h=24):
    """Open book lying flat, seen slightly from above; cx,cy = centre of the spine edge."""
    hw = w / 2
    parts = [
        f'<path d="M {cx - hw - 3} {cy - h / 2 + 3} L {cx} {cy - h / 2 + 7} L {cx + hw + 3} {cy - h / 2 + 3} '
        f'L {cx + hw + 3} {cy + h / 2 + 3} L {cx} {cy + h / 2 + 7} L {cx - hw - 3} {cy + h / 2 + 3} Z" '
        f'fill="{LAPIS}" stroke="{O}" stroke-width="2.4" stroke-linejoin="round"/>',
        f'<path d="M {cx - hw} {cy - h / 2} Q {cx - hw / 2} {cy - h / 2 - 3} {cx} {cy - h / 2 + 4} L {cx} {cy + h / 2 + 4} '
        f'Q {cx - hw / 2} {cy + h / 2 - 3} {cx - hw} {cy + h / 2} Z" fill="{VELLUM}" stroke="{O}" stroke-width="2.2" stroke-linejoin="round"/>',
        f'<path d="M {cx + hw} {cy - h / 2} Q {cx + hw / 2} {cy - h / 2 - 3} {cx} {cy - h / 2 + 4} L {cx} {cy + h / 2 + 4} '
        f'Q {cx + hw / 2} {cy + h / 2 - 3} {cx + hw} {cy + h / 2} Z" fill="{VELLUM}" stroke="{O}" stroke-width="2.2" stroke-linejoin="round"/>',
    ]
    # text lines on each page, with a vermilion initial on the left page
    for i in range(3):
        yy = cy - h / 2 + 7 + i * 6
        parts.append(f'<path d="M {cx - hw + 12} {yy} L {cx - 6} {yy + 1} M {cx + 6} {yy + 1} L {cx + hw - 6} {yy}" '
                     f'stroke="{O}" stroke-width="1.8" stroke-linecap="round" fill="none"/>')
    parts.append(f'<rect x="{cx - hw + 5}" y="{cy - h / 2 + 4}" width="5" height="7" fill="{VERMILION}"/>')
    return "".join(parts)


def satchel(cx, cy, rot=0):
    parts = [
        f'<path d="M -9 -8 L 9 -8 L 10 8 Q 0 12 -10 8 Z" fill="{TAN}" stroke="{O}" stroke-width="2.2" stroke-linejoin="round"/>',
        f'<path d="M -10 -8 Q 0 -4 10 -8 L 9 -1 Q 0 3 -9 -1 Z" fill="{GOLD_DARK}" stroke="{O}" stroke-width="2" stroke-linejoin="round"/>',
        f'<circle cx="0" cy="1" r="1.8" fill="{GOLD}" stroke="{O}" stroke-width="1"/>',
    ]
    return f'<g transform="translate({cx} {cy}) rotate({rot})">' + "".join(parts) + "</g>"


def wrap(inner, prefix):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">\n'
            f'<g id="{prefix}-roundel">\n{inner}\n</g>\n</svg>\n')


def dog_retrieve():
    hx, hy = 66, 98
    p = [roundel(), ground_line(44, 164), body_trot(6, 0), head(hx, hy)]
    # scroll held to the left side of the mouth, angled up-forward so the beard stays visible
    p.append(f'<g transform="translate({hx - 2} {hy + 20}) rotate(-9)">' + scroll(-24, 0, 52) + '</g>')
    p.append(mouth_grip(hx, hy))
    return wrap("\n".join(p), "sd-retrieve")


def dog_search():
    hx, hy = 78, 98
    p = [roundel(), ground_line(14, 172, tufts=False)]
    # a tall bookcase on the left: side board, three shelves of spines, one book tipped out
    p.append(f'<rect x="14" y="62" width="5" height="100" fill="{GOLD_DARK}" stroke="{O}" stroke-width="2"/>')
    p.append(shelf_board(14, 50, 160, 4))
    p.append(shelf_board(14, 50, 126, 4))
    p.append(shelf_board(14, 50, 92, 4))
    for x, w, h, c in ((20, 10, 28, VERDIGRIS), (31, 9, 24, VERMILION), (41, 8, 26, LAPIS)):
        p.append(book_spine(x, 160, w, h, c))
    for x, w, h, c in ((20, 9, 26, GOLD), (30, 10, 24, LAPIS)):
        p.append(book_spine(x, 126, w, h, c))
    for x, w, h, c in ((22, 9, 22, VERMILION), (32, 10, 24, VERDIGRIS)):
        p.append(book_spine(x, 92, w, h, c))
    # the found book, tipped out of the top shelf towards the dog
    p.append(f'<g transform="rotate(24 52 92)">' + book_spine(42, 92, 10, 26, VERMILION) + '</g>')
    p.append(body_trot(18, 0))
    p.append(head(hx, hy))
    return wrap("\n".join(p), "sd-search")


def dog_ingest():
    hx, hy = 70, 98
    p = [roundel(), ground_line(16, 172, tufts=False)]
    # shelf with a gap awaiting the carried book
    p.append(shelf_board(14, 60, 160))
    for x, w, h, c in ((16, 10, 36, VERDIGRIS), (27, 11, 32, VERMILION), (50, 9, 34, GOLD)):
        p.append(book_spine(x, 160, w, h, c))
    p.append(f'<path d="M 40 148 l 0 -14 M 46 148 l 0 -14" stroke="{O}" stroke-width="1.6" stroke-dasharray="3 3" fill="none" stroke-opacity="0.6"/>')
    p.append(body_trot(10, 0))
    p.append(head(hx, hy))
    p.append(f'<g transform="translate({hx - 2} {hy + 20}) rotate(-12)">' + book_closed(-16, 8, 30, 22) + '</g>')
    p.append(mouth_grip(hx, hy))
    return wrap("\n".join(p), "sd-ingest")


def dog_surveil():
    p = [roundel(night=True)]
    p.append(crescent(48, 54, 13))
    for sx, sy, r in ((80, 34, 4), (124, 40, 3.2), (150, 62, 4), (160, 92, 2.8), (30, 92, 3), (108, 26, 2.4), (40, 130, 2.6)):
        p.append(star(sx, sy, r))
    p.append(ground_line(34, 168, 162, tufts=False))
    p.append(lantern(44, 162))
    p.append(body_sit(2, 2))
    p.append(head(102, 96))
    return wrap("\n".join(p), "sd-surveil")


def dog_interrogate():
    p = [roundel(), ground_line(40, 166, 166, tufts=False)]
    p.append(body_sit(0, -4, paws=False))
    p.append(head(100, 92, rot=-8, spectacles=True))
    p.append(open_book(100, 152, 76, 24))
    p.append(paw(118, 146))
    return wrap("\n".join(p), "sd-interrogate")


def scallop(cx, cy, r=5):
    """Pilgrim's scallop shell: fan of ribs on a hinge."""
    pts = [(cx, cy + r * 0.9)]
    for k in range(9):
        ang = math.pi + math.pi * k / 8
        pts.append((cx + r * math.cos(ang), cy + r * 0.8 * math.sin(ang) + r * 0.1))
    d = poly_path(pts)
    ribs = "".join(f"M {cx} {cy + r * 0.9} L {cx + r * 0.75 * math.cos(math.pi + math.pi * k / 4)} "
                   f"{cy + r * 0.6 * math.sin(math.pi + math.pi * k / 4) + r * 0.1} "
                   for k in range(1, 4))
    return (f'<path d="{d}" fill="{GOLD_LIGHT}" stroke="{O}" stroke-width="1.5" stroke-linejoin="round"/>'
            f'<path d="{ribs}" stroke="{O}" stroke-width="1.1" stroke-linecap="round" fill="none"/>')


def dog_discover():
    hx, hy = 82, 98
    p = [roundel()]
    # distant hills with a tiny tower, and a road that winds away towards them
    p.append(f'<path d="M 12 98 L 12 90 A 24 18 0 0 1 60 90 L 60 98 Z" fill="{VERDIGRIS}" stroke="{O}" stroke-width="1.8" stroke-linejoin="round"/>')
    p.append(f'<path d="M 28 98 L 28 92 A 22 14 0 0 1 72 92 L 72 98 Z" fill="{VERDIGRIS}" stroke="{O}" stroke-width="1.8" stroke-linejoin="round"/>')
    p.append(f'<path d="M 10 98 L 76 98" stroke="{O}" stroke-width="1.8" stroke-linecap="round"/>')
    p.append(f'<path d="M 44 84 L 44 70 L 46 70 L 46 67 L 49 67 L 49 70 L 51 70 L 51 67 L 54 67 L 54 70 L 56 70 L 56 84 Z" '
             f'fill="{VELLUM_DARK}" stroke="{O}" stroke-width="1.6" stroke-linejoin="round"/>')
    p.append(f'<rect x="48" y="76" width="4" height="5" fill="{O}"/>')
    road = ("M 66 166 C 36 156 14 140 24 124 C 32 112 44 108 46 98 L 54 98 C 54 110 40 118 36 128 "
            "C 30 142 60 156 118 166 Z")
    p.append(f'<path d="{road}" fill="{VELLUM_DARK}" stroke="{O}" stroke-width="1.8" stroke-linejoin="round"/>')
    p.append(f'<path d="M 30 136 l 4 -2 M 48 152 l 4 -2 M 80 160 l 5 -1" stroke="{O}" stroke-width="1.5" stroke-linecap="round" fill="none" stroke-opacity="0.6"/>')
    p.append(ground_line(60, 176, 166, tufts=False))
    p.append(body_trot(22, 0))
    # satchel slung on the flank with a strap over the shoulder, scallop shell as the pilgrim's badge
    p.append(f'<path d="M 110 86 C 118 100 126 112 132 126" stroke="{O}" stroke-width="5" stroke-linecap="round"/>')
    p.append(f'<path d="M 110 86 C 118 100 126 112 132 126" stroke="{GOLD_DARK}" stroke-width="2.6" stroke-linecap="round"/>')
    p.append(f'<g transform="scale(1.3)">' + satchel(102, 100, rot=-6) + '</g>')
    p.append(scallop(132, 126, 5.5))
    p.append(head(hx, hy))
    # walking staff: gripped in the mouth near its top, foot on the road ahead of the front paws
    sx1, sy1, sx2, sy2 = hx - 10, hy - 36, hx - 30, 164
    p.append(f'<path d="M {sx1} {sy1} L {sx2} {sy2}" stroke="{O}" stroke-width="6.5" stroke-linecap="round"/>')
    p.append(f'<path d="M {sx1} {sy1} L {sx2} {sy2}" stroke="{GOLD_DARK}" stroke-width="3.5" stroke-linecap="round"/>')
    p.append(f'<circle cx="{sx1}" cy="{sy1}" r="4" fill="{GOLD}" stroke="{O}" stroke-width="1.8"/>')
    p.append(mouth_grip(hx, hy))
    return wrap("\n".join(p), "sd-discover")


# ---------------------------------------------------------------- divider
def divider_svg():
    y = 20
    parts = [
        f'<path d="M 24 {y} L 400 {y}" stroke="{VERMILION}" stroke-width="2.4" stroke-linecap="round"/>',
        f'<path d="M 500 {y} L 876 {y}" stroke="{VERMILION}" stroke-width="2.4" stroke-linecap="round"/>',
        # verdigris tendrils curling in from each side
        f'<path d="M 400 {y} C 418 {y} 426 {y - 10} 436 {y - 10} C 444 {y - 10} 444 {y} 436 {y}" fill="none" stroke="{VERDIGRIS}" stroke-width="2.4" stroke-linecap="round"/>',
        f'<path d="M 500 {y} C 482 {y} 474 {y + 10} 464 {y + 10} C 456 {y + 10} 456 {y} 464 {y}" fill="none" stroke="{VERDIGRIS}" stroke-width="2.4" stroke-linecap="round"/>',
        # gold lozenge with a dark edge, and two small berries
        f'<path d="M 450 {y - 11} L 462 {y} L 450 {y + 11} L 438 {y} Z" fill="{GOLD}" stroke="{GOLD_DARK}" stroke-width="2" stroke-linejoin="round"/>',
        f'<path d="M 450 {y - 5} L 455.5 {y} L 450 {y + 5} L 444.5 {y} Z" fill="{VERMILION}"/>',
        f'<circle cx="416" cy="{y - 4}" r="2.6" fill="{VERMILION}"/>',
        f'<circle cx="484" cy="{y + 4}" r="2.6" fill="{VERMILION}"/>',
        f'<circle cx="24" cy="{y}" r="3" fill="{GOLD}" stroke="{GOLD_DARK}" stroke-width="1"/>',
        f'<circle cx="876" cy="{y}" r="3" fill="{GOLD}" stroke="{GOLD_DARK}" stroke-width="1"/>',
    ]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="900" height="40" viewBox="0 0 900 40">\n'
            f'<g id="sdiv-rule">\n' + "\n".join(parts) + '\n</g>\n</svg>\n')


DOGS = {
    "dog-retrieve.svg": dog_retrieve,
    "dog-search.svg": dog_search,
    "dog-ingest.svg": dog_ingest,
    "dog-surveil.svg": dog_surveil,
    "dog-interrogate.svg": dog_interrogate,
    "dog-discover.svg": dog_discover,
}


# ---------------------------------------------------------------- main
def main():
    font = TTFont(FONT_PATH)
    ROMAN_S.update(load_roman_s(ROMAN_FONT, 680))
    cmap = font.getBestCmap()
    missing = [c for c in "criptorium" if ord(c) not in cmap]
    if missing:
        raise SystemExit(f"font lacks glyphs for {missing}")

    light, end_x = banner(font, dark=False)
    dark, _ = banner(font, dark=True)
    (OUT_DIR / "banner-light.svg").write_text(light)
    (OUT_DIR / "banner-dark.svg").write_text(dark)
    for name, fn in DOGS.items():
        (OUT_DIR / name).write_text(fn())
    (OUT_DIR / "divider.svg").write_text(divider_svg())
    print(f"word ends at x={end_x:.1f}")

    # standalone initial for the S-readability check
    g, defs = lombardic_s("t", 0, 0, 200)
    (TEST_DIR / "test-initial.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">'
        f'<defs>{defs}</defs>{g}</svg>')


if __name__ == "__main__":
    main()
