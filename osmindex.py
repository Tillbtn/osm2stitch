"""Spatial index (SQLite + R-tree) over the OSM objects relevant for embroidery maps.

Build it once from an .osm.pbf; afterwards `query()` returns any section in
milliseconds.

    uv run osmindex.py berlin-latest.osm.pbf data/berlin.sqlite
"""
import argparse
import json
import math
import sqlite3
import time
from pathlib import Path

import osmium
import shapely
from osmium.filter import KeyFilter
from shapely.geometry import box

from extract import KEEP_TAGS, KEYS

LINE_KEYS = {"highway", "railway"}
AREA_KEYS = {"natural", "waterway", "landuse", "leisure", "building"}


def build(pbf, db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE features(id INTEGER PRIMARY KEY, kind TEXT, tags TEXT, wkb BLOB);
        CREATE VIRTUAL TABLE features_idx USING rtree(id, minx, maxx, miny, maxy);
    """)
    factory = osmium.geom.WKBFactory()
    fp = (osmium.FileProcessor(pbf)
          .with_locations()
          .with_areas(KeyFilter(*AREA_KEYS))
          .with_filter(KeyFilter(*KEYS)))
    t0, n, batch = time.time(), 0, []

    def flush():
        geoms = shapely.from_wkb([b[2] for b in batch])
        bounds = shapely.bounds(geoms)
        start = n - len(batch)
        con.executemany("INSERT INTO features VALUES (?,?,?,?)",
                        [(start + i, k, t, w) for i, (k, t, w) in enumerate(batch)])
        con.executemany("INSERT INTO features_idx VALUES (?,?,?,?,?)",
                        [(start + i, b[0], b[2], b[1], b[3]) for i, b in enumerate(bounds)])
        batch.clear()

    for obj in fp:
        tags = {k: v for k, v in obj.tags if k in KEEP_TAGS}
        try:
            if obj.is_way() and LINE_KEYS & tags.keys():
                wkb = factory.create_linestring(obj)
                kind = "way"
            elif obj.is_area() and AREA_KEYS & tags.keys():
                wkb = factory.create_multipolygon(obj)
                kind = "area"
            else:
                continue
        except (osmium.InvalidLocationError, RuntimeError):
            continue
        batch.append((kind, json.dumps(tags, ensure_ascii=False), bytes.fromhex(wkb)))
        n += 1
        if len(batch) >= 20000:
            flush()
            print(f"\r{n} objects …", end="", flush=True)
    if batch:
        flush()

    minx, maxx, miny, maxy = con.execute(
        "SELECT min(minx), max(maxx), min(miny), max(maxy) FROM features_idx").fetchone()
    header = fp.header
    meta = {
        "source": Path(pbf).name,
        "timestamp": header.get("osmosis_replication_timestamp") or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(Path(pbf).stat().st_mtime)),
        "bounds": json.dumps([minx, miny, maxx, maxy]),
        "count": str(n),
    }
    con.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    con.commit()
    con.close()
    tmp.replace(db_path)
    print(f"\r{n} objects in {time.time() - t0:.0f}s -> {db_path}")


class Index:
    name = "local"

    def __init__(self, db_path):
        self.db_path = str(db_path)
        con = self._connect()
        self.meta = dict(con.execute("SELECT key, value FROM meta"))
        self.bounds = json.loads(self.meta["bounds"])
        con.close()

    def _connect(self):
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)

    def describe(self):
        return f"local ({self.meta.get('source')})"

    def query(self, lat, lon, width_m, height_m, margin=1.15):
        """All objects in the rectangle (center + extent in m), clipped to the section.
        Same format as extract.py, but with ready-made shapely geometries under "geom"."""
        dlat = height_m / 2 * margin / 111_320
        dlon = width_m / 2 * margin / (111_320 * math.cos(math.radians(lat)))
        clip = box(lon - dlon, lat - dlat, lon + dlon, lat + dlat)
        con = self._connect()
        rows = con.execute("""
            SELECT f.kind, f.tags, f.wkb FROM features_idx i JOIN features f ON f.id = i.id
            WHERE i.maxx >= ? AND i.minx <= ? AND i.maxy >= ? AND i.miny <= ?""",
            (lon - dlon, lon + dlon, lat - dlat, lat + dlat)).fetchall()
        con.close()
        geoms = shapely.intersection(shapely.from_wkb([r[2] for r in rows]), clip) if rows else []
        features = [{"kind": k, "tags": json.loads(t), "geom": g}
                    for (k, t, _), g in zip(rows, geoms) if not g.is_empty]
        return {"center": [lat, lon], "features": features,
                "source": {"name": self.describe(), "timestamp": self.meta.get("timestamp"), "cached": False}}

    def covers(self, bounds):
        """Does the rectangle (minx, miny, maxx, maxy) lie entirely within the data area?"""
        minx, miny, maxx, maxy = self.bounds
        return minx <= bounds[0] and miny <= bounds[1] and bounds[2] <= maxx and bounds[3] <= maxy


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pbf")
    ap.add_argument("db")
    args = ap.parse_args()
    build(args.pbf, args.db)


if __name__ == "__main__":
    main()
