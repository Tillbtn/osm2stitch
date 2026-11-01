"""Web app: pick a map section, download an embroidery file.

    uv run app.py                       # Postpass, local index as fallback
    uv run app.py --source local        # local index only (data/berlin.sqlite)
    -> http://127.0.0.1:8000
"""
import argparse
import collections
import json
import math
import queue
import re
import subprocess
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path

import pyembroidery
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from osm2stitch import (OSM_RIGHTS, OSM_SOURCE, build, inkstitch_cmd, inkstitch_version, strip_spirals,
                        write_plain_svg, write_preview_svg, write_svg)
from osmindex import Index
from sources import FallbackSource, PostpassSource, SourceError, bbox_around
from style import LAYERS, RANGES, ROUTING, STITCHES, layer_type, list_presets, load_style

ROOT = Path(__file__).parent
JOBS_DIR = ROOT / "web_jobs"
FORMATS = ("pes", "dst", "jef", "exp", "vp3", "xxx")

# rough assumptions for the time estimate of a home embroidery machine
STITCHES_PER_MIN = 650
SECONDS_PER_TRIM = 7
SECONDS_PER_COLOR_CHANGE = 45


class JobRequest(BaseModel):
    lat: float
    lon: float
    width_m: float = Field(ge=200, le=5000)
    size_w: float = Field(ge=30, le=400)
    size_h: float = Field(ge=30, le=400)
    format: str = "pes"
    style: str = Field("toner-water", pattern=r"^[a-z0-9-]+$")
    overrides: dict = {}


class Job:
    def __init__(self, req, style):
        self.id = uuid.uuid4().hex[:10]
        self.req = req
        self.style = style
        self.status = "queued"
        self.error = None
        self.geometry = None
        self.data_source = None
        self.result = None
        self.files = {}
        self.inkstitch = None
        self.created = time.time()
        self.dir = JOBS_DIR / self.id

    def public(self, position=None):
        return {"id": self.id, "status": self.status, "error": self.error, "queue_position": position,
                "geometry": self.geometry, "result": self.result, "data_source": self.data_source,
                "inkstitch": self.inkstitch,
                "files": {k: f"files/{self.id}/{v}" for k, v in self.files.items()}}


app = FastAPI(title="osm2stitch")
jobs: dict[str, Job] = {}
pending: queue.Queue[Job] = queue.Queue()
source = None  # Index, PostpassSource or FallbackSource


def embroidery_stats(path):
    pattern = pyembroidery.read(str(path))
    c = collections.Counter(s[2] & pyembroidery.COMMAND_MASK for s in pattern.stitches)
    minx, miny, maxx, maxy = pattern.extends()
    stitches, trims = c[pyembroidery.STITCH], c[pyembroidery.TRIM]
    colors = max(1, len(pattern.threadlist))
    seconds = stitches / STITCHES_PER_MIN * 60 + trims * SECONDS_PER_TRIM + (colors - 1) * SECONDS_PER_COLOR_CHANGE
    return {"stitches": stitches, "trims": trims, "jumps": c[pyembroidery.JUMP], "colors": colors,
            "threads": [t.hex_color() for t in pattern.threadlist],
            "width_mm": round((maxx - minx) / 10, 1), "height_mm": round((maxy - miny) / 10, 1),
            "minutes": math.ceil(seconds / 60)}


def run_inkstitch_parallel(svg, emb, png, fmt):
    """Compute the embroidery file and the preview in parallel."""
    procs = []
    for out, ext, params in ((emb, "output", {"format": fmt}), (png, "png_realistic", {"dpi": 200})):
        f = open(out, "wb")
        procs.append((subprocess.Popen(inkstitch_cmd(svg, ext, **params), stdout=f, stderr=subprocess.PIPE), f, out))
    errors = []
    for p, f, out in procs:
        _, err = p.communicate()
        f.close()
        if p.returncode != 0 or out.stat().st_size == 0 or b"Traceback" in err:
            errors.append(err.decode(errors="replace").strip()[-800:])
    if errors:
        raise RuntimeError("Ink/Stitch: " + errors[0])


def process(job):
    r = job.req
    job.dir.mkdir(parents=True, exist_ok=True)
    height_m = r.width_m * r.size_h / r.size_w

    job.status = "loading"
    t0 = time.time()
    data = source.query(r.lat, r.lon, r.width_m, height_m)
    job.data_source = data["source"] | {"seconds": round(time.time() - t0, 1)}

    job.status = "geometry"
    t0 = time.time()
    job.inkstitch = inkstitch_version()
    meta = {
        "title": f"osm2stitch {r.lat:.6f}, {r.lon:.6f}",
        "date": time.strftime("%Y-%m-%d"),
        "source": OSM_SOURCE,
        "rights": OSM_RIGHTS,
        "description": f"Section: {r.width_m:.0f} m x {height_m:.0f} m, size {r.size_w:.0f} x {r.size_h:.0f} mm\n"
                       f"Style: {job.style['name']}\n"
                       f"Ink/Stitch: {job.inkstitch}\n"
                       f"Data: {job.data_source['name']}, as of {job.data_source.get('timestamp') or '?'}",
    }
    layers, stats, plain = build(data, r.width_m, r.size_w, r.size_h, job.style)
    write_svg(job.dir / "map.svg", layers, r.size_w, r.size_h, meta)
    write_plain_svg(job.dir / "map_plain.svg", plain, job.style, r.size_w, r.size_h, meta)
    write_preview_svg(job.dir / "quickview.svg", layers, r.size_w, r.size_h, job.style["fabric"]["color"])
    (job.dir / "style.json").write_text(json.dumps(job.style, ensure_ascii=False, indent=2))
    job.geometry = {"objects": stats, "seconds": round(time.time() - t0, 1)}
    job.files.update(svg="map.svg", plain_svg="map_plain.svg", preview_svg="quickview.svg")

    job.status = "inkstitch"
    t0 = time.time()
    emb = job.dir / f"map.{r.format}"
    png = job.dir / "preview.png"
    try:
        run_inkstitch_parallel(job.dir / "map.svg", emb, png, r.format)
    except RuntimeError:
        # Ink/Stitch occasionally fails on spiral contour fills -> retry without spirals
        if not strip_spirals(layers):
            raise
        write_svg(job.dir / "map.svg", layers, r.size_w, r.size_h, meta)
        run_inkstitch_parallel(job.dir / "map.svg", emb, png, r.format)
    job.files.update(embroidery=emb.name, png=png.name)
    job.result = embroidery_stats(emb) | {"seconds": round(time.time() - t0, 1), "inkstitch": job.inkstitch}

    readme = (
        "osm2stitch\n==========\n\n"
        f"Center:     {r.lat:.6f}, {r.lon:.6f}\n"
        f"Section:    {r.width_m:.0f} m x {height_m:.0f} m\n"
        f"Size:       {r.size_w:.0f} x {r.size_h:.0f} mm  (1 mm = {r.width_m / r.size_w:.1f} m)\n"
        f"Style:      {job.style['name']} (settings in style.json)\n"
        f"Stitches:   {job.result['stitches']}, trims: {job.result['trims']}, "
        f"approx. {job.result['minutes']} min\n"
        f"Threads (in stitching order): {', '.join(job.result['threads'])}\n"
        f"Ink/Stitch: {job.inkstitch}\n\n"
        "map.svg        Ink/Stitch file as stitched (with stitch parameters and travel paths)\n"
        "map_plain.svg  plain generalized map: one layer per map layer, lines as centerlines,\n"
        "               no Ink/Stitch parameters – for your own edits in Inkscape\n\n"
        f"{OSM_RIGHTS}\n"
        f"Data source: {job.data_source['name']}, as of {job.data_source.get('timestamp') or '?'}\n"
    )
    (job.dir / "README.txt").write_text(readme)
    with zipfile.ZipFile(job.dir / "osm2stitch.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in (emb.name, "map.svg", "map_plain.svg", png.name, "style.json", "README.txt"):
            z.write(job.dir / name, name)
    job.files["zip"] = "osm2stitch.zip"
    job.status = "done"


def worker():
    while True:
        job = pending.get()
        try:
            process(job)
        except SourceError as e:
            job.status, job.error = "error", f"Could not load map data: {e}"
        except Exception as e:
            traceback.print_exc()
            job.status, job.error = "error", str(e)


@app.get("/")
def home():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/info")
def info():
    return {"bounds": source.bounds, "source": source.describe(), "formats": FORMATS,
            "estimate": {"stitches_per_min": STITCHES_PER_MIN, "seconds_per_trim": SECONDS_PER_TRIM}}


@app.get("/api/styles")
def styles():
    """Presets and schema for the style editor."""
    return {"presets": list_presets(), "layers": list(LAYERS),
            "types": {name: layer_type(name) for name in LAYERS},
            "stitches": STITCHES, "ranges": RANGES, "routing": list(ROUTING)}


@app.post("/api/jobs")
def create_job(req: JobRequest):
    if req.format not in FORMATS:
        raise HTTPException(400, f"Unknown format: {req.format}")
    try:
        style = load_style(req.style, req.overrides)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"Invalid style: {e}")
    height_m = req.width_m * req.size_h / req.size_w
    if not source.covers(bbox_around(req.lat, req.lon, req.width_m, height_m)):
        raise HTTPException(400, "The section lies (partly) outside the available OSM data.")
    job = Job(req, style)
    jobs[job.id] = job
    pending.put(job)
    return job.public(position=pending.qsize())


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{10}", job_id) or job_id not in jobs:
        raise HTTPException(404, "Unknown job")
    job = jobs[job_id]
    position = None
    if job.status == "queued":
        position = sum(1 for j in list(pending.queue) if j.created <= job.created)
    return job.public(position)


def main():
    global source
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=("auto", "postpass", "local"), default="auto",
                    help="auto = Postpass, local index if Postpass fails (if present)")
    ap.add_argument("--db", default=str(ROOT / "data" / "berlin.sqlite"), help="index built by osmindex.py")
    ap.add_argument("--cache-dir", default=str(ROOT / "data" / "cache" / "postpass"))
    ap.add_argument("--cache-hours", type=float, default=24, help="how long Postpass responses stay valid")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    local = Index(args.db) if args.source != "postpass" and Path(args.db).exists() else None
    if args.source == "local":
        if local is None:
            raise SystemExit(f"Local index missing: {args.db} (build it with osmindex.py)")
        source = local
    else:
        source = PostpassSource(args.cache_dir, ttl_hours=args.cache_hours)
        if local is not None:
            source = FallbackSource(source, local)
    JOBS_DIR.mkdir(exist_ok=True)
    app.mount("/files", StaticFiles(directory=JOBS_DIR), name="files")
    threading.Thread(target=worker, daemon=True).start()
    print(f"Data source: {source.describe()}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
