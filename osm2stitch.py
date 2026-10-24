"""Erzeugt aus einem OSM-Ausschnitt (siehe extract.py) eine Ink/Stitch-fertige SVG.

Stil: an Stamen Toner angelehnt, ein Garn auf hellem Stoff.
  - Gewässer          -> offene Linienfüllung, oder dichte Füllung in eigener Farbe
  - Parks/Wald        -> Konturfüllung (konzentrische Linien wie Höhenlinien)
  - Bahn, Nebenstraßen-> dreifacher Geradstich (bean stitch)
  - Tertiärstraßen    -> schmale Satinsäule
  - Hauptstraßen      -> breite Satinsäule
  - Rahmen            -> dreifacher Geradstich

Alle Maße im Code sind in mm auf dem Stoff.
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
from shapely.ops import nearest_points, unary_union
from xml.sax.saxutils import quoteattr

INKSTITCH = Path.home() / ".config/inkscape/extensions/inkstitch/bin/inkstitch"

MAJOR = {"motorway", "trunk", "primary", "secondary"}
MEDIUM = {"tertiary"}
MINOR = {"residential", "unclassified", "living_street", "pedestrian"}
RAIL = {"rail", "light_rail", "subway"}
GREEN = {("leisure", "park"), ("landuse", "forest"), ("natural", "wood"), ("landuse", "cemetery")}
WATER = {("natural", "water"), ("waterway", "riverbank")}

STYLE = {
    "major_width": 1.3,     # Satinbreite Hauptstraßen
    "medium_width": 0.9,    # Satinbreite Tertiärstraßen
    "line_width": 0.5,      # optische Breite der Geradstich-Linien (nur für Abstände)
    "simplify": 0.2,        # Douglas-Peucker-Toleranz
    "min_line": 2.0,        # kürzere Linienstücke fallen weg
    "min_area": 4.0,        # kleinere Flächen/Löcher fallen weg
    "building_merge": 0.6,  # Gebäude mit kleinerem Abstand werden zu Blöcken verschmolzen
    "min_building": 2.0,    # kleinere Gebäudeblöcke/Innenhöfe fallen weg
    "min_feature": 0.6,     # schmalere Flächenteile fallen weg
    "road_clearance": 0.2,  # Abstand Füllflächen <-> Straßen
    "jump_distance": 1.0,   # kürzere Lücken werden direkt überstickt
    "snap_distance": 1.0,   # Linienenden bis zu diesem Abstand ans Netz anbinden
    "ring_snap": 2.0,       # dasselbe für Gebäudeumrisse
    "max_travel": 25.0,     # längere Laufwege lohnen sich nicht ggü. Schnitt
    "trim_penalty": 15.0,   # ein Fadenschnitt "kostet" so viel wie x mm Laufweg
    "frame_margin": 0.0,
}


# ---------------------------------------------------------------- Geometrie

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
    """Morphologisch öffnen/schließen, vereinfachen, Kleinkram entfernen."""
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
    """Entfernt lange Abschnitte, die innerhalb von `blocker` verlaufen (z.B. zweite
    Richtungsfahrbahn), lässt aber kurze Überlappungen an Kreuzungen stehen."""
    if blocker is None or blocker.is_empty:
        return lines
    keep = []
    for line in lines:
        inside = line.intersection(blocker)
        long_inside = [p for p in lines_of(inside) if p.length > 3 * s["major_width"]]
        if not long_inside:
            keep.append(line)
            continue
        rest = line.difference(unary_union(long_inside).buffer(0.01))
        keep += [p for p in lines_of(rest) if p.length >= s["min_line"]]
    return keep


def dedupe_class(lines, distance, s):
    """Doppelt erfasste Parallelen (Richtungsfahrbahnen, Gleisbündel) zusammenfassen."""
    kept, blocker = [], None
    for line in sorted(lines, key=lambda l: -l.length):
        for part in drop_parallel([line], blocker, s):
            kept.append(part)
            buf = part.buffer(distance, cap_style="flat")
            blocker = buf if blocker is None else blocker.union(buf)
    return merge_lines(kept, s)


def cut_lines(lines, cutter, s):
    """Linien an einer Fläche abschneiden (Satin endet an querender Satin)."""
    out = []
    for line in lines:
        out += [p for p in lines_of(line.difference(cutter)) if p.length >= s["min_line"]]
    return out


def split_sharp(line, max_angle=60):
    """Satinsäulen an spitzen Knicken teilen, damit die Schienen sich nicht kreuzen."""
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
    """Zwei Schienen mit gleicher Knotenzahl (Gehrung an jedem Knoten)."""
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


# ---------------------------------------------------------------- Reihenfolge

def order_paths(items, start):
    """Greedy nearest neighbour; Linien dürfen umgedreht werden."""
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
    """Linien-Netz für Laufwege zwischen zwei Objekten.

    Laufwege dürfen über Geradstich-Linien (werden ohnehin dreifach überstickt
    bzw. sind es schon) und über die Mittellinien noch nicht gestickter
    Satinsäulen führen (die decken den Laufweg später zu)."""

    def __init__(self, lines, s):
        geoms = [l for l, _ in lines]
        tree = STRtree(geoms)
        connectors = []
        for i, line in enumerate(geoms):
            # offene Linien an beiden Enden anbinden, Ringe (Gebäude) an ihrer nächsten Stelle
            probes = [line] if line.is_ring else [Point(line.coords[0]), Point(line.coords[-1])]
            snap = s["ring_snap"] if line.is_ring else s["snap_distance"]
            for p in probes:
                best = None
                for j in tree.query(p, predicate="dwithin", distance=snap):
                    if j == i:
                        continue
                    d = geoms[j].distance(p)
                    if d < 1e-6:
                        best = None
                        break
                    # Umrisse bevorzugt an Straßen/Bahn statt an Nachbarumrisse anbinden
                    rank = (geoms[j].is_ring, d) if line.is_ring else (False, d)
                    if best is None or rank < best[0]:
                        best = (rank, j)
                if best:
                    connectors.append(LineString(nearest_points(p, geoms[best[1]])))
        # Graph aus Einzelsegmenten, damit jeder Stützpunkt ein Knoten ist
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

    def route(self, items, pos, sewn, s):
        """Greedy: immer das Objekt als nächstes, das über das Netz (oder mit
        Schnitt) am billigsten erreichbar ist. Liefert Objekte + Laufwege."""
        def weight(u, v, d):
            o = d["owner"]
            return d["weight"] if o is None or o[0] == "run" or o not in sewn else None

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
                out.append({"kind": "run", "coords": coords, "start": coords[0], "end": coords[-1],
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


# ---------------------------------------------------------------- SVG

def fmt(pts):
    return " ".join(f"{x:.3f},{y:.3f}" for x, y in pts)


def path_d_poly(poly):
    rings = [poly.exterior] + list(poly.interiors)
    return " ".join(f"M {fmt(r.coords[:-1])} Z" for r in rings)


def attrs(d):
    return " ".join(f"{k}={quoteattr(str(v))}" for k, v in d.items())


def svg_element(it, color, ident):
    k = it["kind"]
    color = it.get("color", color)
    base = {"id": ident}
    if it.get("trim_after"):
        base["inkstitch:trim_after"] = "True"
    if k == "fill":
        base.update({
            "d": path_d_poly(it["poly"]),
            "style": f"fill:{color};fill-rule:evenodd;stroke:none" + (";fill-opacity:0.35" if it["sparse"] else ""),
            "inkstitch:fill_method": it.get("method", "tatami_fill"),
            "inkstitch:angle": it["angle"],
            "inkstitch:row_spacing_mm": it["row_spacing"],
            "inkstitch:max_stitch_length_mm": it["stitch_length"],
            "inkstitch:running_stitch_length_mm": it["stitch_length"] if it["sparse"] else 2.0,
            "inkstitch:fill_underlay": "False" if it["sparse"] else "True",
            "inkstitch:fill_underlay_angle": it["angle"] + 90,
            "inkstitch:fill_underlay_row_spacing_mm": 1.5,
            "inkstitch:expand_mm": 0 if it["sparse"] else 0.2,
            "inkstitch:staggers": 4,
        })
    elif k == "satin":
        left, right = satin_rails(LineString(it["coords"]), it["width"])
        base.update({
            "d": f"M {fmt(left)} M {fmt(right)}",
            "style": f"fill:none;stroke:{color};stroke-width:0.15",
            "inkstitch:satin_column": "True",
            "inkstitch:zigzag_spacing_mm": 0.4,
            "inkstitch:pull_compensation_mm": 0.15,
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


def write_svg(path, layers, w, h, color):
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
        f'xmlns:inkstitch="http://inkstitch.org/namespace" '
        f'width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}">',
        f'  <metadata><inkstitch:inkstitch_svg_version>4</inkstitch:inkstitch_svg_version></metadata>',
    ]
    n = 0
    for name, items in layers:
        out.append(f'  <g inkscape:groupmode="layer" inkscape:label={quoteattr(name)} id={quoteattr("layer_" + name)}>')
        for it in items:
            n += 1
            out.append(svg_element(it, color, f"{it['kind']}{n}"))
        out.append("  </g>")
    out.append("</svg>")
    Path(path).write_text("\n".join(out))


def write_preview_svg(path, layers, w, h, color, fabric="#f5f2ea"):
    """Schnelle Ansicht fürs Web: Satins als breite Linie, Laufwege ausgeblendet."""
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}">',
           f'<rect width="{w}" height="{h}" fill="{fabric}"/>']
    for _, items in layers:
        for it in items:
            c = it.get("color", color)
            if it.get("travel"):
                continue
            if it["kind"] == "fill":
                op = 1 if not it["sparse"] else (0.18 if it.get("method") == "contour_fill" else 0.4)
                out.append(f'<path d="{path_d_poly(it["poly"])}" fill="{c}" fill-opacity="{op}" fill-rule="evenodd"/>')
            elif it["kind"] == "satin":
                out.append(f'<polyline points="{fmt(it["coords"])}" fill="none" stroke="{c}" '
                           f'stroke-width="{it["width"]}" stroke-linejoin="round"/>')
            else:
                sw = 0.45 if it.get("bean", 1) else 0.25
                out.append(f'<polyline points="{fmt(it["coords"])}" fill="none" stroke="{c}" '
                           f'stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round"/>')
    out.append("</svg>")
    Path(path).write_text("\n".join(out))


# ---------------------------------------------------------------- Hauptteil

ALL_LAYERS = {"water", "green", "rail", "minor", "frame"}


def build(data, width_m, w_mm, h_mm, s, water_color=None, buildings="outline", include=ALL_LAYERS):
    lat, lon = data["center"]
    zone = int((lon + 180) // 6) + 1
    proj = Transformer.from_crs("EPSG:4326", f"+proj=utm +zone={zone} +ellps=GRS80 +units=m", always_xy=True)
    cx, cy = proj.transform(lon, lat)
    scale = w_mm / width_m  # mm pro m

    def to_mm(xy):
        x, y = proj.transform(xy[:, 0], xy[:, 1])
        return np.column_stack([(x - cx) * scale + w_mm / 2, h_mm / 2 - (y - cy) * scale])

    frame = box(0, 0, w_mm, h_mm).buffer(-s["frame_margin"])
    groups = {k: [] for k in ("major", "medium", "minor", "rail", "water", "green", "building")}

    for f in data["features"]:
        t = f["tags"]
        if t.get("tunnel", "no") != "no" or t.get("covered", "no") != "no":
            continue
        try:
            if int(t.get("layer", 0)) < 0:
                continue
        except ValueError:
            pass
        g = None
        if f["kind"] == "way" and t.get("area") != "yes":
            hw, rw = t.get("highway", "").removesuffix("_link"), t.get("railway")
            if hw in MAJOR:
                g = "major"
            elif hw in MEDIUM:
                g = "medium"
            elif hw in MINOR:
                g = "minor"
            elif rw in RAIL and "service" not in t:
                g = "rail"
        elif f["kind"] == "area":
            items = set(t.items())
            if items & WATER:
                g = "water"
            elif items & GREEN:
                g = "green"
            elif t.get("building", "no") not in ("no", "roof"):
                g = "building"
        if g is None or (g in ALL_LAYERS and g not in include) or (g == "building" and buildings == "none"):
            continue
        geom = f["geom"] if "geom" in f else wkt.loads(f["wkt"])
        geom = shapely.transform(geom, to_mm).intersection(frame)
        if not geom.is_empty:
            groups[g].append(geom)

    mw, dw, lw = s["major_width"], s["medium_width"], s["line_width"]

    # Linien: zusammenführen, Parallelen ausdünnen, Satin an Satin abschneiden
    major = dedupe_class(merge_lines(groups["major"], s), mw * 1.1, s)
    major_area = unary_union([l.buffer(mw / 2) for l in major])
    medium = dedupe_class(merge_lines(groups["medium"], s), dw * 1.1, s)
    medium = cut_lines(medium, major_area.buffer(-0.15), s)
    medium_area = unary_union([l.buffer(dw / 2) for l in medium])
    satin_area = major_area.union(medium_area)
    minor = dedupe_class(merge_lines(groups["minor"], s), lw * 1.5, s)
    minor = drop_parallel(minor, satin_area.buffer(lw), s)
    rail = dedupe_class(merge_lines(groups["rail"], s), lw * 2, s)
    rail = drop_parallel(rail, satin_area.buffer(lw), s)

    # Flächen: Platz für Straßen freihalten, damit nichts übereinander gestickt wird
    road_area = unary_union([satin_area] + [l.buffer(lw / 2) for l in minor + rail])
    keepout = road_area.buffer(s["road_clearance"])
    water = clean_area(unary_union(groups["water"]).difference(keepout), s)
    a = s["building_merge"] / 2
    blocks = unary_union(groups["building"]).buffer(a, join_style="mitre").buffer(-a, join_style="mitre")
    blocks = clean_area(blocks.difference(keepout).difference(water.buffer(0.3)), s, s["min_building"])
    green = clean_area(unary_union(groups["green"]).difference(keepout).difference(water.buffer(0.5))
                       .difference(blocks.buffer(0.3)), s)

    # Linienobjekte; Koordinaten gerundet, damit sie exakt auf Netzknoten liegen
    def lines_to_items(lines, kind, **kw):
        out = []
        for l in lines:
            l = LineString([key(c) for c in l.coords])
            out.append(line_item(l, kind, owner=(kind, len(all_lines)), **kw))
            all_lines.append((l, (kind, len(all_lines))))
        return out

    all_lines = []
    rings = [r for p in polys_of(blocks) for r in [p.exterior, *p.interiors]] if buildings == "outline" else []
    runs = (lines_to_items(rail, "run", bean=1, stitch_length=2.0, display_width=0.4)
            + lines_to_items(rings, "run", ring=True, bean=0, stitch_length=1.5, display_width=0.25)
            + lines_to_items(minor, "run", bean=1, stitch_length=2.0, display_width=0.5))
    medium_items = lines_to_items([p for l in medium for p in split_sharp(l)], "satin", width=dw)
    major_items = lines_to_items([p for l in major for p in split_sharp(l)], "satin", width=mw)
    net = Network(all_lines, s)

    layers, trims, pos, sewn = [], 0, (0.0, 0.0), set()
    for name, items in [
        ("Wasser", [poly_item(p, "fill", angle=0, row_spacing=0.25, stitch_length=3.0, sparse=False, color=water_color)
                    if water_color else
                    poly_item(p, "fill", angle=0, row_spacing=0.6, stitch_length=3.0, sparse=True)
                    for p in polys_of(water)]),
        ("Gruen", [poly_item(p, "fill", method="contour_fill", angle=0, row_spacing=1.8, stitch_length=2.5, sparse=True)
                   for p in polys_of(green)]),
        ("Gebaeude", [poly_item(p, "fill", angle=135, row_spacing=0.8, stitch_length=3.0, sparse=True)
                      for p in polys_of(blocks)] if buildings == "fill" else []),
    ]:
        ordered, pos = order_paths(items, pos)
        for it in ordered:
            it["trim_after"] = True
        layers.append((name, ordered))
        trims += len(ordered)
    for name, items in [("Linien", runs), ("Tertiaerstrassen", medium_items), ("Hauptstrassen", major_items)]:
        ordered, pos, t = net.route(items, pos, sewn, s)
        layers.append((name, ordered))
        trims += t
    if layers[-1][1]:
        layers[-1][1][-1]["trim_after"] = True
    if "frame" in include:
        fr = frame.exterior.coords
        layers.append(("Rahmen", [{"kind": "run", "coords": list(fr), "start": fr[0], "end": fr[-1],
                                   "bean": 1, "stitch_length": 2.5, "display_width": 0.5}]))

    stats = {name: n for name, items in layers if (n := sum(1 for it in items if not it.get("travel")))}
    stats["davon Gebaeudeumrisse"] = len(rings)
    stats["Laufwege"] = sum(1 for _, items in layers for it in items if it.get("travel"))
    stats["Schnitte"] = trims
    return layers, stats


def inkstitch_cmd(svg, extension, **params):
    return [str(INKSTITCH), f"--extension={extension}"] + [f"--{k}={v}" for k, v in params.items()] + [str(svg)]


def run_inkstitch(svg, extension, out, **params):
    with open(out, "wb") as f:
        r = subprocess.run(inkstitch_cmd(svg, extension, **params), stdout=f, stderr=subprocess.PIPE)
    if r.returncode or r.stderr.strip():
        print(r.stderr.decode(errors="replace")[-2000:], file=sys.stderr)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("extract", help="JSON aus extract.py")
    ap.add_argument("-o", "--output", default="karte", help="Ausgabe-Basisname")
    ap.add_argument("--width-m", type=float, default=1600, help="Kartenbreite in der Realität")
    ap.add_argument("--size", default="100x100", help="Stickgröße in mm, BxH")
    ap.add_argument("--color", default="#000000", help="Garnfarbe")
    ap.add_argument("--buildings", choices=("outline", "fill", "none"), default="outline",
                    help="Gebäudeblöcke als Umriss, offene Füllung oder gar nicht")
    ap.add_argument("--water-color", help="eigene Garnfarbe für Wasser (sonst offene Füllung in --color)")
    ap.add_argument("--format", default="pes", help="Stickformat für Ink/Stitch (pes, dst, jef, ...)")
    ap.add_argument("--no-inkstitch", action="store_true", help="nur SVG schreiben")
    args = ap.parse_args()

    w_mm, h_mm = (float(v) for v in args.size.lower().split("x"))
    data = json.load(open(args.extract))
    layers, stats = build(data, args.width_m, w_mm, h_mm, STYLE, args.water_color, args.buildings)
    svg = Path(args.output + ".svg")
    svg.parent.mkdir(parents=True, exist_ok=True)
    write_svg(svg, layers, w_mm, h_mm, args.color)
    print(f"{svg}: " + ", ".join(f"{k} {v}" for k, v in stats.items()))

    if not args.no_inkstitch:
        emb = Path(f"{args.output}.{args.format}")
        if run_inkstitch(svg, "output", emb, format=args.format):
            print(f"{emb} geschrieben")
        png = Path(args.output + "_vorschau.png")
        if run_inkstitch(svg, "png_realistic", png, dpi=300):
            print(f"{png} geschrieben")


if __name__ == "__main__":
    main()
