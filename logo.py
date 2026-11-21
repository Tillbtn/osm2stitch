"""Embroidery files of the OSM logo (data/osm_logo_simple.svg) for download.

    uv run logo.py                      # 50 mm wide, all formats -> web/logo/
    uv run logo.py --width 80 -o out/logo
"""
import argparse
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from shapely.geometry import Polygon

from app import FORMATS, embroidery_stats
from osm2stitch import inkstitch_version, run_inkstitch

ROOT = Path(__file__).parent


FILL = ('style="fill:#000000;stroke:none" inkstitch:trim_after="True" '
        'inkstitch:fill_method="auto_fill" inkstitch:angle="45" inkstitch:row_spacing_mm="0.25" '
        'inkstitch:max_stitch_length_mm="3" inkstitch:fill_underlay="True" inkstitch:fill_underlay_angle="135" '
        'inkstitch:fill_underlay_row_spacing_mm="1.5" inkstitch:expand_mm="0" inkstitch:staggers="4"')


def subpaths(d):
    """Split a path of relative m/l/c/z commands into subpaths, each starting with an absolute M.
    Returns (d, ring) per subpath; ring is a polyline approximation for containment tests."""
    toks = re.findall(r"[a-zA-Z]|-?(?:\d+\.?\d*|\.\d+)(?:e-?\d+)?", d)
    if set(t for t in toks if t.isalpha()) - set("mlcz"):
        raise ValueError("only relative m/l/c/z paths are supported")
    out, i, cur, start, cmd = [], 0, (0.0, 0.0), (0.0, 0.0), None

    def num():
        nonlocal i
        i += 1
        return float(toks[i - 1])

    while i < len(toks):
        if toks[i].isalpha():
            cmd = toks[i]
            i += 1
        if cmd == "m":
            cur = start = (cur[0] + num(), cur[1] + num())
            out.append([f"M {cur[0]:.5f},{cur[1]:.5f}", [cur]])
            cmd = "l"  # further pairs after m are implicit linetos
            if i < len(toks) and not toks[i].isalpha():
                out[-1][0] += " l"
            continue
        if cmd == "z":
            out[-1][0] += " z"
            cur, cmd = start, None
            continue
        n = 2 if cmd == "l" else 6
        vals = [num() for _ in range(n)]
        if not out[-1][0].endswith(cmd):
            out[-1][0] += f" {cmd}"
        out[-1][0] += " " + " ".join(f"{v:g}" for v in vals)
        if cmd == "c":  # control points are good enough for containment of whole rings
            out[-1][1] += [(cur[0] + vals[k], cur[1] + vals[k + 1]) for k in (0, 2)]
        cur = (cur[0] + vals[-2], cur[1] + vals[-1])
        out[-1][1].append(cur)
    return [(d, Polygon(ring).buffer(0)) for d, ring in out]


def shapes(d):
    """Group subpaths into separate filled shapes (outer ring + its holes), by nesting depth."""
    subs = subpaths(d)
    parents = []
    for k, (_, p) in enumerate(subs):
        pt = p.representative_point()
        containing = [j for j, (_, q) in enumerate(subs) if j != k and q.area > p.area and q.contains(pt)]
        parents.append(containing)
    depth = [len(c) for c in parents]
    groups = {k: [k] for k in range(len(subs)) if depth[k] % 2 == 0}
    for k in range(len(subs)):
        if depth[k] % 2:  # hole: belongs to its innermost filled container
            groups[max(parents[k], key=lambda j: depth[j])].append(k)
    return [(" ".join(subs[k][0] for k in ks), subs[ks[0]][1]) for ks in groups.values()]


def stitch_order(items):
    """Greedy nearest-neighbour order, starting with the largest shape."""
    rest = sorted(items, key=lambda it: -it[1].area)
    order = [rest.pop(0)]
    while rest:
        rest.sort(key=lambda it: it[1].distance(order[-1][1]))
        order.append(rest.pop(0))
    return order


def logo_svg(src, width_mm):
    """Ink/Stitch SVG: every separate part of the logo as its own fill with a trim after it,
    so no travel stitches cross the white gaps. Scaled via viewBox."""
    text = src.read_text()
    vw, vh = (float(re.search(rf'\s{k}="([\d.]+)"', text).group(1)) for k in ("width", "height"))
    tx, ty = (float(v) for v in re.search(r'translate\(([-\d.]+),([-\d.]+)\)', text).groups())
    d = re.search(r'\sd="([^"]+)"', text).group(1)
    h_mm = round(width_mm * vh / vw, 2)
    paths = "\n".join(f'    <path id="part{n}" d="{pd}" {FILL} />'
                      for n, (pd, _) in enumerate(stitch_order(shapes(d)), 1))
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     xmlns:inkstitch="http://inkstitch.org/namespace"
     width="{width_mm}mm" height="{h_mm}mm" viewBox="{-tx} {-ty} {vw} {vh}">
  <metadata><inkstitch:inkstitch_svg_version>4</inkstitch:inkstitch_svg_version></metadata>
  <g inkscape:groupmode="layer" inkscape:label="OpenStreetMap logo">
{paths}
  </g>
</svg>
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=ROOT / "data/osm_logo_simple.svg")
    ap.add_argument("--width", type=float, default=50, help="width in mm")
    ap.add_argument("-o", "--output", type=Path, default=ROOT / "web/logo/osm_logo", help="output base path")
    args = ap.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    svg = args.output.with_suffix(".svg")
    svg.write_text(logo_svg(args.src, args.width))
    jobs = [(svg, "output", args.output.with_suffix(f".{fmt}"), {"format": fmt}) for fmt in FORMATS]
    jobs.append((svg, "png_realistic", args.output.with_suffix(".png"), {"dpi": 200}))
    with ThreadPoolExecutor(len(jobs)) as pool:
        ok = list(pool.map(lambda j: run_inkstitch(j[0], j[1], j[2], **j[3]), jobs))
    for (_, _, out, _), good in zip(jobs, ok):
        print(f"{'ok  ' if good else 'FAIL'} {out}")
    print(f"Ink/Stitch {inkstitch_version()}:", embroidery_stats(args.output.with_suffix(".pes")))


if __name__ == "__main__":
    main()
