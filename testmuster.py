"""Test pattern for trial stitching: 16 fields with variants of the stitch parameters.

Each field carries its number (stitched, top left). The legend with all parameters
is written next to it as a text file. After stitching, compare the fields and
copy the best values into the style (styles/*.json).

    uv run testmuster.py -o out/testmuster
"""
import argparse
import math
from pathlib import Path

from shapely.geometry import LineString, Polygon, box

from osm2stitch import line_item, poly_item, run_inkstitch, satin_rails, write_svg
from style import DEFAULT

CELL, GAP, MARGIN = 22.0, 2.0, 3.0
COLOR = "#1a1a1a"
WATER = "#2f6fb0"
PULL = DEFAULT["fabric"]["pull_compensation"]

# Seven-segment digits, each as one continuous line in a 2 x 3 mm box
DIGITS = {
    "0": [(0, 0), (2, 0), (2, 3), (0, 3), (0, 0)],
    "1": [(1.5, 0), (1.5, 3)],
    "2": [(0, 0), (2, 0), (2, 1.5), (0, 1.5), (0, 3), (2, 3)],
    "3": [(0, 0), (2, 0), (2, 1.5), (0.4, 1.5), (2, 1.5), (2, 3), (0, 3)],
    "4": [(0, 0), (0, 1.5), (2, 1.5), (2, 0), (2, 3)],
    "5": [(2, 0), (0, 0), (0, 1.5), (2, 1.5), (2, 3), (0, 3)],
    "6": [(2, 0), (0, 0), (0, 3), (2, 3), (2, 1.5), (0, 1.5)],
    "7": [(0, 0), (2, 0), (2, 3)],
    "8": [(0, 1.5), (0, 0), (2, 0), (2, 3), (0, 3), (0, 1.5), (2, 1.5)],
    "9": [(2, 1.5), (0, 1.5), (0, 0), (2, 0), (2, 3), (0, 3)],
}


def label(n, x, y):
    items, cx = [], x
    for d in str(n):
        items.append(run([(cx + px, y + py) for px, py in DIGITS[d]], repeats=1, stitch_length=1.0))
        cx += 3.0
    return items


def run(coords, repeats=3, stitch_length=2.0, color=COLOR):
    return line_item(LineString(coords), "run", color=color, bean=(repeats - 1) // 2,
                     stitch_length=stitch_length, display_width=0.5 if repeats >= 3 else 0.25)


def satin(coords, width, density=0.4, color=COLOR):
    return line_item(LineString(coords), "satin", color=color, width=width, density=density,
                     pull_compensation=PULL)


def fill(poly, method="tatami_fill", row_spacing=0.25, angle=0, underlay=True, stitch_length=3.0, color=COLOR,
         **kw):
    return poly_item(poly, "fill", color=color, method=method, row_spacing=row_spacing, angle=angle,
                     underlay=underlay, stitch_length=stitch_length, opacity=1, **kw)


def s_curve(x, y, w=18, h=12, n=36):
    """Smooth arc with flat ends and a tight curve in the middle."""
    return [(x + w * t / n, y + h / 2 * (1 - math.cos(2 * math.pi * t / n))) for t in range(n + 1)]


def zigzag_street(x, y):
    return [(x, y + 2), (x + 8, y + 2), (x + 8, y + 9), (x + 18, y + 9), (x + 18, y + 15), (x + 2, y + 15)]


def cell_content(n, x, y):
    """Content of field n (1..16) in the box x..x+CELL, y+5..y+CELL."""
    y0 = y + 5
    if n <= 4:  # satin widths
        w = (0.8, 1.0, 1.3, 1.6)[n - 1]
        return [satin(s_curve(x + 2, y0 + 1), w)], f"Satin {w} mm breit, Stichabstand 0.4, Unterlage Mittellinie" + (
            " + Kontur" if w >= 1.5 else "")
    if n in (5, 6):
        d = 0.3 if n == 5 else 0.5
        return [satin(s_curve(x + 2, y0 + 1), 1.3, density=d)], f"Satin 1.3 mm, Stichabstand {d} mm"
    if n == 7:  # junction of a major road (1.3) with a cut-off tertiary road (0.9)
        return [satin([(x + 9, y0), (x + 11, y0 + 6.2)], 0.9), satin([(x + 11.6, y0 + 8.0), (x + 13, y0 + 15)], 0.9),
                satin([(x + 1, y0 + 8), (x + 21, y0 + 6)], 1.3)], \
            "Kreuzung: Satin 1.3 mm durchgehend, Satin 0.9 mm davor und danach abgeschnitten"
    if n == 8:  # canal between two waterfront roads
        top = [(x + 1, y0 + 4), (x + 21, y0 + 6)]
        bottom = [(x + 1, y0 + 9.4), (x + 21, y0 + 11.4)]
        canal = Polygon([(x + 1, y0 + 5.05), (x + 21, y0 + 7.05), (x + 21, y0 + 10.35), (x + 1, y0 + 8.35)])
        return [fill(canal.buffer(-0.2), color=WATER), satin(top, 1.3), satin(bottom, 1.3)], \
            "Kanal: Wasser 2.9 mm breit (blau, Tatami 0.25 + Unterlage) zwischen zwei Satinstraßen 1.3 mm"
    if n <= 12:
        variants = {9: (1, 1.5), 10: (1, 2.0), 11: (3, 2.0), 12: (3, 2.5)}
        rep, sl = variants[n]
        items = [run(zigzag_street(x + 1, y0), rep, sl)]
        for i, size in enumerate((1.5, 2.0, 3.0)):  # small building blocks
            bx = x + 11 + i * 3.5
            r = box(bx, y0 + 0.5, bx + size, y0 + 0.5 + size).exterior.coords
            items.append(run(list(r), rep, sl))
        return items, f"Geradstich {rep}x, Stichlänge {sl} mm; Gebäude 1.5 / 2 / 3 mm"
    sq = box(x + 1, y0 + 0.5, x + CELL - 1, y0 + 15.5)
    if n == 13:
        return [fill(sq, color=WATER)], "Füllung dicht: Tatami 0.25 mm, Unterlage, 0°"
    if n == 14:
        return [fill(sq, row_spacing=0.6, underlay=False)], "Füllung offen: Tatami 0.6 mm, ohne Unterlage, 0°"
    if n == 15:
        return [fill(sq, method="contour_fill", row_spacing=1.8, underlay=False, stitch_length=2.5)], \
            "Konturfüllung 1.8 mm, innen nach außen (Standard), Stichlänge 2.5"
    return [fill(sq, method="contour_fill", row_spacing=1.8, underlay=False, stitch_length=2.5, strategy=1)], \
        "Konturfüllung 1.8 mm als eine Spirale, Stichlänge 2.5"


def build():
    layers, legend, blue = [], [], []
    for n in range(1, 17):
        row, col = divmod(n - 1, 4)
        x, y = MARGIN + col * (CELL + GAP), MARGIN + row * (CELL + GAP)
        content, text = cell_content(n, x, y)
        items = label(n, x + 0.5, y + 0.5) + content
        for it in items:
            it["trim_after"] = True
        # stitch everything blue first so there is only one color change
        blue += [it for it in items if it["color"] == WATER]
        layers.append((f"Feld {n}", [it for it in items if it["color"] != WATER]))
        legend.append(f"{n:>2}  {text}")
    return [("Blau", blue)] + layers, legend


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default="out/testmuster")
    ap.add_argument("--format", default="pes")
    args = ap.parse_args()

    size = 4 * CELL + 3 * GAP + 2 * MARGIN
    layers, legend = build()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_svg(out.with_suffix(".svg"), layers, size, size)
    text = (f"Testmuster {size:.0f} x {size:.0f} mm – Felder zeilenweise von links oben, Nummer oben links im Feld\n"
            f"Garne: {COLOR} (Schwarz), {WATER} (Blau, Felder 8 und 13). Zugausgleich Satin: {PULL} mm\n\n"
            + "\n".join(legend) + "\n\nNotieren: Stoff, Vlies, Garn, Maschine – und welche Felder gut aussehen.\n")
    out.with_name(out.name + "_legende.txt").write_text(text)
    print(text)
    if run_inkstitch(out.with_suffix(".svg"), "output", out.with_suffix("." + args.format), format=args.format):
        print(f"{out.with_suffix('.' + args.format)} geschrieben")
    if run_inkstitch(out.with_suffix(".svg"), "png_realistic", out.with_name(out.name + "_vorschau.png"), dpi=300):
        print(f"{out.name}_vorschau.png geschrieben")


if __name__ == "__main__":
    main()
