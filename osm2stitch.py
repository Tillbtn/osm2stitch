"""Turns an OSM section into an SVG ready for Ink/Stitch.

How each object type is stitched (stitch type, width, density, color, on/off)
is defined in the style (see style.py and styles/*.json).

All dimensions in the code are in mm on the fabric.
"""
import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

from pyproj import Transformer
import numpy as np
import shapely
from shapely import STRtree, wkt
import networkx as nx
from shapely.geometry import LineString, MultiLineString, Point, Polygon, box
from shapely.ops import linemerge, nearest_points, unary_union
from xml.sax.saxutils import escape, quoteattr

from style import AREA_LAYERS, LINE_LAYERS, ROUTING, load_style, set_path

INKSTITCH = Path.home() / ".config/inkscape/extensions/inkstitch/bin/inkstitch"

MAJOR = {"motorway", "trunk", "primary", "secondary"}
MEDIUM = {"tertiary"}
MINOR = {"residential", "unclassified", "living_street", "pedestrian"}
RAIL = {"rail", "light_rail", "subway"}
GREEN = {("leisure", "park"), ("leisure", "garden"), ("leisure", "playground"),
         ("landuse", "forest"), ("natural", "wood"), ("landuse", "cemetery")}
WATER = {("natural", "water"), ("waterway", "riverbank")}

# Parallel lines of a class closer than MERGE_M meters (but at least
# MERGE_FACTOR x line width) are merged into one centerline
MERGE_M = {"major": 30, "medium": 25, "minor": 0, "rail": 20}
MERGE_FACTOR = {"major": 1.1, "medium": 1.1, "minor": 1.5, "rail": 2.0}

# Tunnels and covered sections up to this length that connect visible lines of the
# same class at both ends (e.g. under a wide bridge) are stitched like the surface
PASSAGE_M = 100

OSM_SOURCE = "https://www.openstreetmap.org"
OSM_RIGHTS = "Map data © OpenStreetMap contributors, ODbL – https://www.openstreetmap.org/copyright"


# ---------------------------------------------------------------- Geometry

def lines_of(geom):
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if hasattr(geom, "geoms"):
        return [g for part in geom.geoms for g in lines_of(part)]
    return []


def polys_of(geom):
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if hasattr(geom, "geoms"):
        return [g for part in geom.geoms for g in polys_of(part)]
    return []


def clean_area(geom, s, min_area=None):
    """Morphological opening/closing, simplify, remove small bits."""
    a = s["min_feature"] / 2
    geom = geom.buffer(-a).buffer(a).buffer(a).buffer(-a)
    geom = geom.simplify(s["simplify"])
    min_area = min_area or s["min_area"]
    out = []
    for p in polys_of(geom):
        if p.area < min_area:
            continue
        holes = [h for h in p.interiors if Polygon(h).area >= min_area]
        out.append(Polygon(p.exterior, holes))
    return unary_union(out)


def merge_lines(lines, s):
    merged = shapely.line_merge(unary_union(lines)) if lines else MultiLineString()
    return [l.simplify(s["simplify"]) for l in lines_of(merged) if l.length >= s["min_line"]]


def drop_parallel(lines, blocker, s):
    """Removes long sections running inside `blocker` (e.g. the second carriageway
    of a dual carriageway), but keeps short overlaps at junctions."""
    if blocker is None or blocker.is_empty:
        return lines
    keep = []
    for line in lines:
        inside = line.intersection(blocker)
        long_inside = [p for p in lines_of(inside) if p.length > s["parallel_min_length"]]
        if not long_inside:
            keep.append(line)
            continue
        rest = line.difference(unary_union(long_inside).buffer(0.01))
        keep += [p for p in lines_of(rest) if p.length >= s["min_line"]]
    return keep


def centerlines(lines, d, s):
    """Medial axis of all lines of a class: buffer (radius d), union, and determine the
    skeleton of the area via a Voronoi diagram. Dual carriageways and bundles of
    tracks less than 2*d apart thus become one line, and junctions meet in a
    single point."""
    if not lines:
        return []
    poly = unary_union([l.buffer(d) for l in lines]).simplify(d / 10)
    pts = shapely.get_coordinates(shapely.segmentize(poly.boundary, d / 3))
    edges = shapely.get_parts(shapely.voronoi_polygons(shapely.multipoints(pts), only_edges=True))
    shapely.prepare(poly)
    skeleton = unary_union(edges[shapely.contains_properly(poly, edges)])

    # prune the skeleton's spurs (towards the corners of the buffer area)
    for _ in range(6):
        pieces = lines_of(shapely.line_merge(skeleton))
        degree = {}
        for l in pieces:
            for c in (key(l.coords[0]), key(l.coords[-1])):
                degree[c] = degree.get(c, 0) + 1
        keep = [l for l in pieces
                if l.length >= 2 * d
                or (degree[key(l.coords[0])] == 1) == (degree[key(l.coords[-1])] == 1)]
        if len(keep) == len(pieces):
            break
        skeleton = unary_union(keep)
    return [l.simplify(s["simplify"]) for l in lines_of(shapely.line_merge(skeleton)) if l.length >= s["min_line"]]


def cut_lines(lines, cutter, s):
    """Cut lines at an area (satin ends at a crossing satin)."""
    out = []
    for line in lines:
        out += [p for p in lines_of(line.difference(cutter)) if p.length >= s["min_line"]]
    return out


def split_sharp(line, max_angle=60):
    """Split satin columns at sharp bends so the rails do not cross."""
    pts = list(line.coords)
    parts, cur = [], [pts[0]]
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        cur.append(b)
        h1 = math.atan2(b[1] - a[1], b[0] - a[0])
        h2 = math.atan2(c[1] - b[1], c[0] - b[0])
        turn = abs((math.degrees(h2 - h1) + 180) % 360 - 180)
        if turn > max_angle:
            parts.append(cur)
            cur = [b]
    cur.append(pts[-1])
    parts.append(cur)
    return [LineString(p) for p in parts if len(p) >= 2]


def satin_rails(line, width):
    """Two rails with the same number of nodes (mitered at every node)."""
    pts = list(line.coords)
    normals = []
    for i in range(len(pts) - 1):
        (x1, y1), (x2, y2) = pts[i], pts[i + 1]
        d = math.hypot(x2 - x1, y2 - y1) or 1e-9
        normals.append((-(y2 - y1) / d, (x2 - x1) / d))
    left, right = [], []
    for i, (x, y) in enumerate(pts):
        if i == 0:
            nx, ny = normals[0]
        elif i == len(pts) - 1:
            nx, ny = normals[-1]
        else:
            (ax, ay), (bx, by) = normals[i - 1], normals[i]
            mx, my = ax + bx, ay + by
            m = math.hypot(mx, my) or 1e-9
            mx, my = mx / m, my / m
            scale = min(1 / max(mx * bx + my * by, 1e-3), 2.0)
            nx, ny = mx * scale, my * scale
        h = width / 2
        left.append((x + nx * h, y + ny * h))
        right.append((x - nx * h, y - ny * h))
    return left, right


def track_walk(line, width, spacing):
    """Railway track as one closed running-stitch path: out along the left rail with
    a spike across to the right rail at every sleeper, back along the right rail.
    Rails are stitched once, sleepers twice (the two end sleepers once)."""
    coords = list(line.coords)
    n = max(1, round(line.length / spacing))
    ties = [line.length * k / n for k in range(n + 1)]
    cum = np.cumsum([0] + [math.dist(a, b) for a, b in zip(coords, coords[1:])])
    # vertices stay so the rails follow curves; those right next to a sleeper merge into it
    bends = [d for d in cum[1:-1] if min(abs(d - t) for t in ties) > 0.1]
    stations = sorted([(d, True) for d in ties] + [(d, False) for d in bends])
    pts = [line.interpolate(d).coords[0] for d, _ in stations]
    left, right = satin_rails(LineString(pts), width)
    walk = [left[0]]
    for i in range(1, len(pts)):
        walk.append(left[i])
        if stations[i][1] and i < len(pts) - 1:
            walk += [right[i], left[i]]
    return walk + right[::-1] + [left[0]]


# ---------------------------------------------------------------- Ordering

def order_paths(items, start):
    """Greedy nearest neighbour; lines may be reversed."""
    items = list(items)
    ordered, pos = [], start
    while items:
        best, best_d, best_rev = None, float("inf"), False
        for i, it in enumerate(items):
            for rev in ((False, True) if it["reversible"] else (False,)):
                p = it["end"] if rev else it["start"]
                d = math.dist(pos, p)
                if d < best_d:
                    best, best_d, best_rev = i, d, rev
        it = items.pop(best)
        if best_rev:
            it = it["reverse"]()
        ordered.append(it)
        pos = it["end"]
    return ordered, pos


def key(c):
    return (round(c[0], 3), round(c[1], 3))


class Network:
    """Line network for travel paths between two objects.

    Each edge belongs to an object (owner = (kind, no, color, covers)) or is a
    short connector (owner None). A travel path may use an edge if it will be
    stitched over later anyway (not yet stitched and satin or multi-pass stitch)
    or if it already exists as a running stitch in the same color."""

    def __init__(self, lines, s):
        geoms = [l for l, _ in lines]
        tree = STRtree(geoms)
        connectors = []
        for i, line in enumerate(geoms):
            # connect open lines at both ends, rings (buildings, tracks) at their nearest point
            probes = [line] if line.is_closed else [Point(line.coords[0]), Point(line.coords[-1])]
            snap = s["ring_snap"] if line.is_closed else s["snap_distance"]
            for p in probes:
                best = None
                for j in tree.query(p, predicate="dwithin", distance=snap):
                    if j == i:
                        continue
                    d = geoms[j].distance(p)
                    if d < 1e-6:
                        best = None
                        break
                    # prefer connecting outlines to roads/rail rather than to neighboring outlines
                    rank = (geoms[j].is_closed, d) if line.is_closed else (False, d)
                    if best is None or rank < best[0]:
                        best = (rank, j)
                if best:
                    connectors.append(LineString(nearest_points(p, geoms[best[1]])))
        # graph of individual segments so every vertex is a node
        self.g = nx.Graph()
        for piece in lines_of(unary_union(geoms + connectors)):
            mid = piece.interpolate(0.5, normalized=True)
            owner = None
            for j in tree.query(mid, predicate="dwithin", distance=1e-4):
                owner = lines[j][1]
                if owner[0] == "run":
                    break
            for c1, c2 in zip(piece.coords, piece.coords[1:]):
                a, b = key(c1), key(c2)
                if a != b:
                    self.g.add_edge(a, b, weight=math.dist(c1, c2), coords=[c1, c2], owner=owner)

    def route(self, items, pos, sewn, s, color):
        """Greedy: always pick next the object that is cheapest to reach via the
        network (or with a trim). Returns objects + travel paths."""
        def weight(u, v, d):
            o = d["owner"]
            if o is None:
                return d["weight"]
            kind, _, c, covers = o
            if o not in sewn:
                return d["weight"] if covers or c == color else None
            return d["weight"] if kind == "run" and c == color else None

        items, out, trims = list(items), [], 0
        while items:
            dist, paths = {}, {}
            if key(pos) in self.g:
                dist, paths = nx.single_source_dijkstra(self.g, key(pos), cutoff=s["max_travel"], weight=weight)
            best = None
            for i, it in enumerate(items):
                if it.get("ring"):
                    entries = list(enumerate(it["coords"][:-1]))
                else:
                    entries = [(False, it["start"]), (True, it["end"])]
                for rev, p in entries:
                    e = math.dist(pos, p)
                    if e <= s["jump_distance"]:
                        cand = (e, None, False)
                    elif key(p) in dist:
                        cand = (dist[key(p)], paths[key(p)], False)
                    else:
                        cand = (e + s["trim_penalty"], None, True)
                    if best is None or cand[0] < best[0][0]:
                        best = (cand, i, rev)
            (_, path, trim), i, rev = best
            it = items.pop(i)
            if it.get("ring"):
                it = it["rotate"](rev)
            elif rev:
                it = it["reverse"]()
            if trim:
                trims += 1
                if out:
                    out[-1]["trim_after"] = True
            elif path and len(path) > 1:
                coords = [path[0]]
                for u, v in zip(path, path[1:]):
                    c = self.g[u][v]["coords"]
                    coords += (c if key(c[0]) == u else c[::-1])[1:]
                out.append({"kind": "run", "coords": coords, "start": coords[0], "end": coords[-1], "color": color,
                            "bean": 0, "stitch_length": 2.0, "display_width": 0.15, "travel": True})
            out.append(it)
            sewn.add(it["owner"])
            pos = it["end"]
        return out, pos, trims


def line_item(line, kind, **kw):
    coords = list(line.coords)
    def make(c):
        return {"kind": kind, "coords": c, "start": c[0], "end": c[-1], "reversible": True,
                "reverse": lambda: make(c[::-1]),
                "rotate": lambda j: make(c[j:-1] + c[:j] + [c[j]]), **kw}
    return make(coords)


def poly_item(poly, kind, **kw):
    c = poly.exterior.coords[0]
    return {"kind": kind, "poly": poly, "start": c, "end": c, "reversible": False, **kw}


def order_colors(colors, prev=None, nxt=None):
    """Color order within a stage: put connecting colors at the edges to keep
    color changes to a minimum."""
    return sorted(colors, key=lambda c: (c != prev, c == nxt))


# ---------------------------------------------------------------- SVG

def fmt(pts):
    return " ".join(f"{x:.3f},{y:.3f}" for x, y in pts)


def path_d_poly(poly):
    rings = [poly.exterior] + list(poly.interiors)
    return " ".join(f"M {fmt(r.coords[:-1])} Z" for r in rings)


def attrs(d):
    return " ".join(f"{k}={quoteattr(str(v))}" for k, v in d.items())


def svg_element(it, ident):
    k, color = it["kind"], it["color"]
    base = {"id": ident}
    if it.get("trim_after"):
        base["inkstitch:trim_after"] = "True"
    if k == "fill":
        base.update({
            "d": path_d_poly(it["poly"]),
            "style": f"fill:{color};fill-rule:evenodd;stroke:none;fill-opacity:{it['opacity']}",
            "inkstitch:fill_method": it["method"],
            "inkstitch:angle": it["angle"],
            "inkstitch:row_spacing_mm": it["row_spacing"],
            "inkstitch:max_stitch_length_mm": it["stitch_length"],
            "inkstitch:running_stitch_length_mm": it["stitch_length"] if it["method"] == "contour_fill" else 2.0,
            "inkstitch:fill_underlay": "True" if it["underlay"] else "False",
            "inkstitch:fill_underlay_angle": it["angle"] + 90,
            "inkstitch:fill_underlay_row_spacing_mm": 1.5,
            "inkstitch:expand_mm": 0.2 if it["underlay"] else 0,
            "inkstitch:staggers": 4,
        })
        if it.get("strategy") is not None:
            base["inkstitch:contour_strategy"] = it["strategy"]
    elif k == "satin":
        left, right = satin_rails(LineString(it["coords"]), it["width"])
        base.update({
            "d": f"M {fmt(left)} M {fmt(right)}",
            "style": f"fill:none;stroke:{color};stroke-width:0.15",
            "inkstitch:satin_column": "True",
            "inkstitch:zigzag_spacing_mm": it["density"],
            "inkstitch:pull_compensation_mm": it["pull_compensation"],
            "inkstitch:center_walk_underlay": "True",
            "inkstitch:contour_underlay": "True" if it["width"] >= 1.5 else "False",
        })
    elif k == "run":
        base.update({
            "d": f"M {fmt(it['coords'])}",
            "style": f"fill:none;stroke:{color};stroke-width:{it.get('display_width', 0.5)};stroke-linecap:round;stroke-linejoin:round",
            "inkstitch:running_stitch_length_mm": it.get("stitch_length", 2.0),
            "inkstitch:bean_stitch_repeats": it.get("bean", 1),
        })
    return f"    <path {attrs(base)} />"


def rdf_metadata(meta):
    """Dublin Core metadata, shown by Inkscape under Document Properties > Metadata."""
    if not meta:
        return ""
    fields = "".join(f"<dc:{k}>{escape(str(v))}</dc:{k}>" for k, v in meta.items() if v)
    return (f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:cc="http://creativecommons.org/ns#">'
            f'<cc:Work rdf:about="">{fields}</cc:Work></rdf:RDF>')


def write_svg(path, layers, w, h, meta=None):
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        f'xmlns:inkstitch="http://inkstitch.org/namespace" '
        f'width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}">',
        f'  <metadata><inkstitch:inkstitch_svg_version>4</inkstitch:inkstitch_svg_version>'
        f'{rdf_metadata(meta)}</metadata>',
    ]
    n = 0
    for name, items in layers:
        out.append(f'  <g inkscape:groupmode="layer" inkscape:label={quoteattr(name)}>')
        for it in items:
            n += 1
            out.append(svg_element(it, f"{it['kind']}{n}"))
        out.append("  </g>")
    out.append("</svg>")
    Path(path).write_text("\n".join(out))


def write_plain_svg(path, plain, style, w, h, meta=None):
    """Plain SVG for further editing: the generalized geometry before conversion
    to stitches, one Inkscape layer per map layer, no Ink/Stitch parameters,
    no travel paths. Lines are single centerlines with the stitched width."""
    L = style["layers"]
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        f'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" '
        f'width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}">',
        f'  <metadata>{rdf_metadata(meta)}</metadata>',
        # fabric color as page color instead of a rectangle that would be stitched
        f'  <sodipodi:namedview pagecolor="{style["fabric"]["color"]}" inkscape:pageopacity="1" '
        f'inkscape:document-units="mm" />',
    ]

    def layer(name, elements):
        if elements:
            out.append(f'  <g id="{name}" inkscape:groupmode="layer" inkscape:label={quoteattr(L[name]["label"])}>')
            out.extend(f'    <path id="{name}{i}" {e} />' for i, e in enumerate(elements, 1))
            out.append("  </g>")

    for name, polys in plain["areas"].items():
        cfg = L[name]
        if cfg["stitch"] == "outline":
            st = f"fill:none;stroke:{cfg['color']};stroke-width:{line_width(cfg)};stroke-linejoin:round"
        else:
            st = f"fill:{cfg['color']};fill-opacity:{fill_opacity(cfg)};fill-rule:evenodd;stroke:none"
        layer(name, [f'd="{path_d_poly(p)}" style="{st}"' for p in polys])
    for name in reversed(LINE_LAYERS):  # higher classes on top
        if name in plain["lines"]:
            cfg = L[name]
            st = (f"fill:none;stroke:{cfg['color']};stroke-width:{line_width(cfg)};"
                  "stroke-linecap:round;stroke-linejoin:round")
            layer(name, [f'd="M {fmt(l.coords)}" style="{st}"' for l in plain["lines"][name]])
    if plain["frame"]:
        cfg = L["frame"]
        layer("frame", [f'd="M 0,0 H {w} V {h} H 0 Z" '
                        f'style="fill:none;stroke:{cfg["color"]};stroke-width:{line_width(cfg)}"'])
    out.append("</svg>")
    Path(path).write_text("\n".join(out))


def write_preview_svg(path, layers, w, h, fabric="#f5f2ea"):
    """Quick view for the web: satins as wide lines, travel paths hidden."""
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}">',
           f'<rect width="{w}" height="{h}" fill="{fabric}"/>']
    for _, items in layers:
        for it in items:
            c = it["color"]
            if it.get("travel"):
                continue
            if it["kind"] == "fill":
                out.append(f'<path d="{path_d_poly(it["poly"])}" fill="{c}" fill-opacity="{it["opacity"]}" fill-rule="evenodd"/>')
            elif it["kind"] == "satin":
                out.append(f'<polyline points="{fmt(it["coords"])}" fill="none" stroke="{c}" '
                           f'stroke-width="{it["width"]}" stroke-linejoin="round"/>')
            else:
                sw = 0.45 if it.get("bean", 1) else 0.25
                out.append(f'<polyline points="{fmt(it["coords"])}" fill="none" stroke="{c}" '
                           f'stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round"/>')
    out.append("</svg>")
    Path(path).write_text("\n".join(out))


# ---------------------------------------------------------------- Main

def lowered(t):
    """Below its surroundings: tunnel, covered or negative layer."""
    if t.get("tunnel", "no") != "no" or t.get("covered", "no") != "no":
        return True
    try:
        return int(t.get("layer", 0)) < 0
    except ValueError:
        return False


def underground(f):
    """Tunnel, covered, or below ground. For lines a negative layer alone usually
    just means passing under a bridge, so it only counts for areas and subways."""
    t = f["tags"]
    if t.get("tunnel", "no") != "no" or t.get("covered", "no") != "no":
        return True
    return lowered(t) and (f["kind"] == "area" or t.get("railway") == "subway")


def short_passages(tunnels, surface, max_len):
    """Underground sections (merged into chains) no longer than max_len whose both
    ends meet a surface line, so the line is not interrupted under a bridge."""
    if not tunnels or not surface:
        return []
    surf = unary_union(surface)
    shapely.prepare(surf)
    out = []
    for l in lines_of(linemerge([p for t in tunnels for p in lines_of(t)])):
        ends = [Point(l.coords[0]), Point(l.coords[-1])]
        if l.length <= max_len and all(surf.dwithin(p, 0.01) for p in ends):
            out.append(l)
    return out


def classify(f):
    """OSM object -> style layer (or None)."""
    t = f["tags"]
    if f["kind"] == "way" and t.get("area") != "yes":
        hw, rw = t.get("highway", "").removesuffix("_link"), t.get("railway")
        if hw in MAJOR:
            return "major"
        if hw in MEDIUM:
            return "medium"
        if hw in MINOR:
            return "minor"
        if rw in RAIL and "service" not in t:
            return "rail"
    elif f["kind"] == "area":
        items = set(t.items())
        if items & WATER:
            return "water"
        if items & GREEN:
            return "green"
        if t.get("building", "no") not in ("no", "roof"):
            return "buildings"
    return None


def spiral_ok(poly, spacing):
    """Ink/Stitch can only stitch a contour fill as a single spiral if the area has
    no holes and at least one turn fits inside."""
    inner, d = poly, 0.0
    while True:
        if not isinstance(inner, Polygon) or inner.interiors:
            return False
        d += spacing / 2
        inner = poly.buffer(-d)
        if inner.is_empty:
            return d > spacing


def fill_opacity(cfg):
    """Display opacity of a fill, hinting at its density."""
    if cfg["stitch"] == "contour":
        return 0.18
    return 1 if cfg["row_spacing"] <= 0.4 else 0.4


def line_width(cfg):
    """Visual width of a line on the fabric (for clearances and displacement)."""
    if cfg["stitch"] == "satin":
        return cfg["width"]
    if cfg["stitch"] == "track":
        return cfg["width"] + 0.3
    return 0.5 if cfg["repeats"] >= 3 else 0.3


def build(data, width_m, w_mm, h_mm, style):
    s = {**style["generalization"], **ROUTING[style["routing"]["mode"]]}
    L = style["layers"]
    enabled = {name for name, cfg in L.items() if cfg["enabled"]}

    lat, lon = data["center"]
    zone = int((lon + 180) // 6) + 1
    proj = Transformer.from_crs("EPSG:4326", f"+proj=utm +zone={zone} +ellps=GRS80 +units=m", always_xy=True)
    cx, cy = proj.transform(lon, lat)
    scale = w_mm / width_m  # mm per m

    def to_mm(xy):
        x, y = proj.transform(xy[:, 0], xy[:, 1])
        return np.column_stack([(x - cx) * scale + w_mm / 2, h_mm / 2 - (y - cy) * scale])

    frame = box(0, 0, w_mm, h_mm)
    groups = {name: [] for name in LINE_LAYERS + AREA_LAYERS}
    # lines passing underneath something: stitched, but they do not displace other lines
    tunnels, below = ({name: [] for name in LINE_LAYERS} for _ in range(2))
    for f in data["features"]:
        g = classify(f)
        if g is None or g not in enabled:
            continue
        geom = f["geom"] if "geom" in f else wkt.loads(f["wkt"])
        geom = shapely.transform(geom, to_mm)
        if underground(f):
            if g in tunnels:
                tunnels[g].append(geom)
            continue
        groups[g].append(geom)
        if g in below and lowered(f["tags"]):
            below[g].append(geom)
    for name in tunnels:
        passages = short_passages(tunnels[name], groups[name], PASSAGE_M * scale)
        groups[name] += passages
        below[name] += passages
    for name in groups:
        clip = frame.buffer(5 if name in LINE_LAYERS else 0)
        groups[name] = [c for g in groups[name] if not (c := g.intersection(clip)).is_empty]

    # Lines in descending priority: merge, remove parallels to higher classes,
    # cut satin at crossing satin
    lines, covered, satin_cover, blocking = {}, None, None, Polygon()
    for name in LINE_LAYERS:
        cfg, w = L[name], line_width(L[name])
        # lines extending beyond the frame make the skeleton reach all the way to the edge
        ls = centerlines(groups[name], max(w * MERGE_FACTOR[name], MERGE_M[name] / width_m * w_mm) / 2, s)
        ls = [p for l in ls for p in lines_of(l.intersection(frame))]
        if cfg["stitch"] == "satin" and satin_cover is not None:
            ls = cut_lines(ls, satin_cover.buffer(-0.15), s)
            other = blocking.difference(satin_cover)
            ls = drop_parallel(ls, other.buffer(w) if not other.is_empty else None, s)
        elif not blocking.is_empty:
            ls = drop_parallel(ls, blocking.buffer(w), s)
        lines[name] = ls
        if ls:
            area = unary_union([l.buffer(w / 2) for l in ls])
            covered = area if covered is None else covered.union(area)
            if cfg["stitch"] == "satin":
                satin_cover = area if satin_cover is None else satin_cover.union(area)
            if below[name]:
                area = area.difference(unary_union(below[name]).buffer(w / 2 + 0.1))
            blocking = blocking.union(area)

    # Areas: keep space free for lines so nothing is stitched on top of each other
    keepout = covered.buffer(s["road_clearance"]) if covered is not None else Polygon()
    areas = {}
    areas["water"] = clean_area(unary_union(groups["water"]).difference(keepout), s)
    a = s["building_merge"] / 2
    blocks = unary_union(groups["buildings"]).buffer(a, join_style="mitre").buffer(-a, join_style="mitre")
    areas["buildings"] = clean_area(blocks.difference(keepout).difference(areas["water"].buffer(0.3)),
                                    s, s["min_building"])
    areas["green"] = clean_area(unary_union(groups["green"]).difference(keepout)
                                .difference(areas["water"].buffer(0.5))
                                .difference(areas["buildings"].buffer(0.3)), s)

    # Generalized geometry before conversion to stitches, for the plain SVG
    plain = {"lines": {name: lines[name] for name in LINE_LAYERS if name in enabled},
             "areas": {name: polys_of(areas[name]) for name in AREA_LAYERS if name in enabled},
             "frame": "frame" in enabled}

    # Create objects; coordinates rounded so they lie exactly on network nodes
    net_lines = []

    def add_lines(geoms, kind, cfg, **kw):
        out = []
        covers = kind == "satin" or cfg.get("repeats", 1) >= 3
        for l in geoms:
            l = LineString([key(c) for c in l.coords])
            owner = (kind, len(net_lines), cfg["color"], covers)
            net_lines.append((l, owner))
            out.append(line_item(l, kind, owner=owner, color=cfg["color"], **kw))
        return out

    def run_kw(cfg):
        return {"bean": (cfg["repeats"] - 1) // 2, "stitch_length": cfg["stitch_length"],
                "display_width": 0.5 if cfg["repeats"] >= 3 else 0.25}

    fills, runs, satins = [], [], {}
    for name in AREA_LAYERS:
        cfg = L[name]
        if name not in enabled:
            continue
        polys = polys_of(areas[name])
        if cfg["stitch"] == "outline":
            rings = [r for p in polys for r in [p.exterior, *p.interiors]]
            runs += add_lines(rings, "run", cfg, ring=True, layer=name, **run_kw(cfg))
        else:
            contour = cfg["stitch"] == "contour"
            fills += [poly_item(p, "fill", layer=name, color=cfg["color"], opacity=fill_opacity(cfg),
                                method="contour_fill" if contour else "tatami_fill",
                                angle=cfg["angle"], row_spacing=cfg["row_spacing"],
                                stitch_length=cfg["stitch_length"], underlay=cfg["underlay"],
                                strategy=1 if contour and spiral_ok(p, cfg["row_spacing"]) else None)
                      for p in polys]
    for name in LINE_LAYERS:
        cfg = L[name]
        if cfg["stitch"] == "satin":
            satins[name] = add_lines([p for l in lines[name] for p in split_sharp(l)], "satin", cfg, layer=name,
                                     width=cfg["width"], density=cfg["density"],
                                     pull_compensation=style["fabric"]["pull_compensation"])
        elif cfg["stitch"] == "track":
            # closed path: the router may enter it anywhere, like a building outline
            walks = [LineString(track_walk(l, cfg["width"], cfg["tie_spacing"])) for l in lines[name]]
            runs += add_lines(walks, "run", cfg, ring=True, layer=name, **run_kw(cfg))
        else:
            runs += add_lines(lines[name], "run", cfg, layer=name, **run_kw(cfg))
    if "frame" in enabled:
        # frame as a ring in the network: roads end on it, it serves as a loop path without trims
        runs += add_lines([frame.exterior], "run", L["frame"], ring=True, layer="frame", **run_kw(L["frame"]))
    net = Network(net_lines, s)

    # Stitch order: areas -> running stitch -> satin (low before high priority),
    # grouped by color within a stage
    satin_stages = [(name, satins[name]) for name in reversed(LINE_LAYERS) if satins.get(name)]
    first_satin = L[satin_stages[0][0]]["color"] if satin_stages else None
    run_colors = order_colors({it["color"] for it in runs}, nxt=first_satin)
    fill_colors = order_colors({it["color"] for it in fills}, nxt=run_colors[0] if run_colors else first_satin)

    layers, trims, pos, sewn = [], 0, (0.0, 0.0), set()
    for color in fill_colors:
        ordered, pos = order_paths([it for it in fills if it["color"] == color], pos)
        for it in ordered:
            it["trim_after"] = True
        layers.append((f"Areas {color}", ordered))
        trims += len(ordered)
    for color in run_colors:
        ordered, pos, t = net.route([it for it in runs if it["color"] == color], pos, sewn, s, color)
        layers.append((f"Lines {color}", ordered))
        trims += t
    for name, items in satin_stages:
        ordered, pos, t = net.route(items, pos, sewn, s, L[name]["color"])
        layers.append((f"{L[name]['label']} (Satin)", ordered))
        trims += t

    all_items = [it for _, items in layers for it in items]
    stats = {L[name]["label"]: n for name in L
             if (n := sum(1 for it in all_items if it.get("layer") == name))}
    stats["Travel paths"] = sum(1 for it in all_items if it.get("travel"))
    stats["Trims"] = trims
    stats["Colors"] = len({it["color"] for it in all_items})
    return layers, stats, plain


def strip_spirals(layers):
    """Reset contour fills to the default method; True if there were any."""
    found = False
    for _, items in layers:
        for it in items:
            if it.get("strategy") is not None:
                it["strategy"], found = None, True
    return found


def inkstitch_version():
    """Version of the installed Ink/Stitch, e.g. 'v3.3.0 (linux-x86_64) 2026-07-31 09:01'."""
    try:
        return (INKSTITCH.parent.parent / "VERSION").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def inkstitch_cmd(svg, extension, **params):
    return [str(INKSTITCH), f"--extension={extension}"] + [f"--{k}={v}" for k, v in params.items()] + [str(svg)]


def run_inkstitch(svg, extension, out, **params):
    with open(out, "wb") as f:
        r = subprocess.run(inkstitch_cmd(svg, extension, **params), stdout=f, stderr=subprocess.PIPE)
    if r.returncode or r.stderr.strip():
        print(r.stderr.decode(errors="replace")[-2000:], file=sys.stderr)
    return r.returncode == 0 and b"Traceback" not in r.stderr


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("extract", help="JSON from extract.py")
    ap.add_argument("-o", "--output", default="map", help="output base name")
    ap.add_argument("--width-m", type=float, default=1600, help="real-world width of the map in meters")
    ap.add_argument("--size", default="100x100", help="embroidery size in mm, WxH")
    ap.add_argument("--style", default="toner-water", help="preset from styles/ or path to a JSON file")
    ap.add_argument("--set", action="append", default=[], metavar="PATH=VALUE",
                    help="override a style value, e.g. layers.water.color=#ff0000 or routing.mode=balanced")
    ap.add_argument("--format", default="pes", help="embroidery format (pes, dst, jef, ...)")
    ap.add_argument("--no-inkstitch", action="store_true", help="only write the SVG")
    args = ap.parse_args()

    overrides = {}
    for item in args.set:
        path, _, value = item.partition("=")
        set_path(overrides, path, value)
    style = load_style(args.style, overrides)
    w_mm, h_mm = (float(v) for v in args.size.lower().split("x"))
    data = json.load(open(args.extract))
    layers, stats, plain = build(data, args.width_m, w_mm, h_mm, style)
    meta = {"title": f"osm2stitch {args.extract}", "source": OSM_SOURCE, "rights": OSM_RIGHTS,
            "description": f"Style: {style['name']}\nInk/Stitch: {inkstitch_version()}"}
    svg = Path(args.output + ".svg")
    svg.parent.mkdir(parents=True, exist_ok=True)
    write_svg(svg, layers, w_mm, h_mm, meta)
    write_plain_svg(Path(args.output + "_plain.svg"), plain, style, w_mm, h_mm, meta)
    print(f"{svg}: " + ", ".join(f"{k} {v}" for k, v in stats.items()))

    if not args.no_inkstitch:
        emb = Path(f"{args.output}.{args.format}")
        if not run_inkstitch(svg, "output", emb, format=args.format) and strip_spirals(layers):
            print("Ink/Stitch failed, retrying without spirals", file=sys.stderr)
            write_svg(svg, layers, w_mm, h_mm, meta)
            run_inkstitch(svg, "output", emb, format=args.format)
        if emb.stat().st_size:
            print(f"wrote {emb}")
        png = Path(args.output + "_preview.png")
        if run_inkstitch(svg, "png_realistic", png, dpi=300):
            print(f"wrote {png}")


if __name__ == "__main__":
    main()
