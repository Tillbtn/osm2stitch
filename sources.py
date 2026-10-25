"""Data sources for OSM sections.

All sources return the same format via `query(lat, lon, width_m, height_m)`:
{"center": [lat, lon], "features": [{"kind": "way"|"area", "tags": {...}, "geom": shapely}],
 "source": {"name": ..., "timestamp": ..., "cached": bool, "note": ...}}

- PostpassSource: Geofabrik's public PostGIS API, with a cache
- osmindex.Index: local SQLite index built from a PBF (regional)
- FallbackSource: Postpass first, the local index on failure (if it covers the section)
"""
import gzip
import json
import math
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import shapely
from shapely.geometry import box, shape

from osm2stitch import GREEN, MAJOR, MEDIUM, MINOR, RAIL, WATER

POSTPASS_URL = "https://postpass.geofabrik.de/api/interpreter"
USER_AGENT = "osm2stitch/0.1 (+https://github.com/Tillbtn/osm2stitch)"


class SourceError(RuntimeError):
    """Data source unreachable or request rejected."""


def bbox_around(lat, lon, width_m, height_m, margin=1.0):
    dlat = height_m / 2 * margin / 111_320
    dlon = width_m / 2 * margin / (111_320 * math.cos(math.radians(lat)))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def _sql_str(v):
    return "'" + v.replace("'", "''") + "'"


def _in(values):
    return "(" + ",".join(_sql_str(v) for v in sorted(values)) + ")"


def postpass_sql(minx, miny, maxx, maxy):
    """A single query for lines and areas, filtered to what osm2stitch uses."""
    highways = MAJOR | MEDIUM | MINOR | {f"{v}_link" for v in MAJOR | MEDIUM}
    bbox = f"ST_MakeEnvelope({minx:.6f},{miny:.6f},{maxx:.6f},{maxy:.6f},4326)"
    area_tags = " OR ".join(f"tags @> {_sql_str(json.dumps({k: v}))}::jsonb" for k, v in sorted(WATER | GREEN))
    return (
        f"SELECT 'way' AS kind, tags, geom FROM postpass_line WHERE geom && {bbox} AND "
        f"(tags->>'highway' IN {_in(highways)} OR tags->>'railway' IN {_in(RAIL)}) "
        f"UNION ALL "
        f"SELECT 'area' AS kind, tags, geom FROM postpass_polygon WHERE geom && {bbox} AND "
        f"((tags ? 'building' AND tags->>'building' NOT IN ('no', 'roof')) OR {area_tags})"
    )


def clip(features, bounds):
    """Clip geometries to the section, drop empty ones."""
    if not features:
        return []
    clipped = shapely.intersection([f["geom"] for f in features], box(*bounds))
    return [f | {"geom": g} for f, g in zip(features, clipped) if not g.is_empty]


class PostpassSource:
    name = "Postpass"
    bounds = None  # no coverage limit

    def __init__(self, cache_dir, url=POSTPASS_URL, ttl_hours=24, timeout=90, fetch_margin=1.4):
        self.url, self.timeout, self.ttl = url, timeout, ttl_hours * 3600
        self.fetch_margin = fetch_margin  # fetch extra margin so small shifts are served from the cache
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with self._db() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS cache(
                file TEXT PRIMARY KEY, minx REAL, miny REAL, maxx REAL, maxy REAL,
                fetched REAL, timestamp TEXT)""")

    def describe(self):
        return "Postpass (Geofabrik)"

    def covers(self, bounds):
        return True

    def _db(self):
        return sqlite3.connect(self.cache_dir / "cache.sqlite")

    def _lookup(self, bounds):
        minx, miny, maxx, maxy = bounds
        area = (maxx - minx) * (maxy - miny)
        with self._db() as con:
            return con.execute("""
                SELECT file, timestamp FROM cache
                WHERE minx <= ? AND miny <= ? AND maxx >= ? AND maxy >= ? AND fetched >= ?
                  AND (maxx - minx) * (maxy - miny) <= ?
                ORDER BY fetched DESC LIMIT 1""",
                (minx, miny, maxx, maxy, time.time() - self.ttl, area * 6)).fetchone()

    def _store(self, bounds, raw, timestamp):
        name = f"{uuid.uuid4().hex}.geojson.gz"
        (self.cache_dir / name).write_bytes(gzip.compress(raw))
        with self._db() as con:
            con.execute("INSERT INTO cache VALUES (?,?,?,?,?,?,?)", (name, *bounds, time.time(), timestamp))
            # clean up expired entries
            for (old,) in con.execute("SELECT file FROM cache WHERE fetched < ?", (time.time() - self.ttl,)).fetchall():
                (self.cache_dir / old).unlink(missing_ok=True)
            con.execute("DELETE FROM cache WHERE fetched < ?", (time.time() - self.ttl,))

    def _fetch(self, bounds):
        body = urllib.parse.urlencode({"data": postpass_sql(*bounds)}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace").strip()
            detail = "" if detail.startswith("<") else f": {detail[:300]}"  # omit HTML error pages
            raise SourceError(f"Postpass returned error {e.code}{detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise SourceError(f"Postpass unreachable: {getattr(e, 'reason', e)}") from e

    def query(self, lat, lon, width_m, height_m, margin=1.15):
        need = bbox_around(lat, lon, width_m, height_m, margin)
        hit = self._lookup(need)
        if hit:
            raw, timestamp, cached = gzip.decompress((self.cache_dir / hit[0]).read_bytes()), hit[1], True
        else:
            fetch = bbox_around(lat, lon, width_m, height_m, margin * self.fetch_margin)
            raw = self._fetch(fetch)
            try:
                timestamp = json.loads(raw).get("postpass_properties", {}).get("timestamp")
            except json.JSONDecodeError as e:
                raise SourceError("Postpass returned invalid data") from e
            self._store(fetch, raw, timestamp)
            cached = False
        fc = json.loads(raw)
        features = [{"kind": f["properties"]["kind"], "tags": f["properties"]["tags"] or {},
                     "geom": shape(f["geometry"])}
                    for f in fc.get("features", []) if f.get("geometry")]
        return {"center": [lat, lon], "features": clip(features, need),
                "source": {"name": self.describe(), "timestamp": timestamp, "cached": cached}}


class FallbackSource:
    """The primary source first; if it fails, the fallback source (if it covers the section)."""

    def __init__(self, primary, fallback):
        self.primary, self.fallback = primary, fallback
        self.bounds = primary.bounds
        self.name = primary.name

    def describe(self):
        return f"{self.primary.describe()}, fallback: {self.fallback.describe()}"

    def covers(self, bounds):
        return self.primary.covers(bounds)

    def query(self, lat, lon, width_m, height_m, margin=1.15):
        try:
            return self.primary.query(lat, lon, width_m, height_m, margin)
        except SourceError as e:
            if not self.fallback.covers(bbox_around(lat, lon, width_m, height_m)):
                raise SourceError(f"{e} (and the section is outside the local data)") from e
            data = self.fallback.query(lat, lon, width_m, height_m, margin)
            data["source"]["note"] = f"{e}; used local data instead."
            return data
