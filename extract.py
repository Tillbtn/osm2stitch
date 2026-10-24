"""Cuts the OSM objects relevant for the embroidery map out of an .osm.pbf.

The result is a small JSON file (WGS84 WKT + selected tags), so the actual map
generator can be iterated on quickly without reading the whole PBF every time.
"""
import argparse
import json
import math
import time

import osmium
from osmium.filter import KeyFilter
from shapely import wkb
from shapely.geometry import box

KEYS = ("highway", "railway", "natural", "waterway", "landuse", "leisure", "building")
KEEP_TAGS = KEYS + ("tunnel", "bridge", "layer", "service", "area", "water", "covered", "name")


def bbox_around(lat, lon, radius_m):
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * math.cos(math.radians(lat)))
    return box(lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pbf")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--radius", type=float, default=2500, help="halbe Kantenlänge in m")
    ap.add_argument("-o", "--output", default="extract.json")
    args = ap.parse_args()

    bounds = bbox_around(args.lat, args.lon, args.radius)
    minx, miny, maxx, maxy = bounds.bounds
    factory = osmium.geom.WKBFactory()
    features = []
    t0 = time.time()

    fp = (osmium.FileProcessor(args.pbf)
          .with_locations()
          .with_areas(KeyFilter("natural", "waterway", "landuse", "leisure", "building"))
          .with_filter(KeyFilter(*KEYS)))

    for obj in fp:
        try:
            if obj.is_way():
                # quick pre-check on the nodes before building the geometry
                if not any(minx <= n.lon <= maxx and miny <= n.lat <= maxy
                           for n in obj.nodes if n.location.valid()):
                    continue
                geom = wkb.loads(factory.create_linestring(obj), hex=True)
                kind = "way"
            elif obj.is_area():
                geom = wkb.loads(factory.create_multipolygon(obj), hex=True)
                if not geom.intersects(bounds):
                    continue
                kind = "area"
            else:
                continue
        except (osmium.InvalidLocationError, RuntimeError):
            continue
        if not geom.intersects(bounds):
            continue
        tags = {k: v for k, v in obj.tags if k in KEEP_TAGS}
        features.append({"kind": kind, "tags": tags, "wkt": geom.intersection(bounds.buffer(0.001)).wkt})

    with open(args.output, "w") as f:
        json.dump({"center": [args.lat, args.lon], "radius": args.radius, "features": features}, f)
    print(f"{len(features)} Objekte in {time.time() - t0:.0f}s -> {args.output}")


if __name__ == "__main__":
    main()
