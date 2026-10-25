# osm2stitch

Turns OpenStreetMap data into embroidery files. The map is generalized and
converted into an SVG for [Ink/Stitch](https://inkstitch.org), which then
produces the embroidery file (PES, DST, JEF, …) and a realistic preview without
the Inkscape GUI.

## Setup

    uv sync

Ink/Stitch must be installed in `~/.config/inkscape/extensions/inkstitch`; it is
called headless via `bin/inkstitch`.

## Web app

    uv run app.py                    # http://127.0.0.1:8000
    uv run app.py --host 0.0.0.0     # reachable on the local network

Pick a section on the map, choose hoop size, scale and style, and download the
embroidery file or a ZIP with the SVG, preview, style settings and attribution.
Jobs are processed one after another through a queue; results are stored in
`web_jobs/<id>/`.

### Data sources

- **Postpass** (default): Geofabrik's public OSM PostGIS API. One SQL query per
  map, filtered to the tags that are used. Responses are cached with some margin
  in `data/cache/postpass/` (24 h by default, `--cache-hours`), so style changes
  and small shifts need no new query.
- **Local index** (fallback): if `data/berlin.sqlite` exists, it is used when
  Postpass fails and the section lies within it. Build it from a PBF:

      uv run osmindex.py berlin-latest.osm.pbf data/berlin.sqlite

`--source local` uses only the local index, `--source postpass` only Postpass.

## Command line

1. Extract the relevant objects from a PBF once:

       uv run extract.py berlin-latest.osm.pbf --lat 52.498751 --lon 13.381239 -o kreuzberg.json

2. Generate maps from it:

       uv run osm2stitch.py kreuzberg.json -o out/kreuzberg --width-m 800
       uv run osm2stitch.py kreuzberg.json -o out/colorful --width-m 800 --style colorful
       uv run osm2stitch.py kreuzberg.json -o out/test --set layers.major.width=1.6 --set routing.mode=balanced

   Options: `--width-m` (section width in meters), `--size` (embroidery size in
   mm, default `100x100`), `--style` (preset from `styles/` or path to a JSON
   file), `--set PATH=VALUE` (override single style values), `--format` (pes,
   dst, jef, …), `--no-inkstitch` (SVG only).

Output: `out/<name>.svg` (editable in Inkscape), `out/<name>.pes`,
`out/<name>_preview.png`.

## Styles

A style defines for each layer (major, tertiary and minor roads, railways,
water, parks, buildings, frame) whether and how it is stitched (satin, running
stitch, track, fill, contour fill, outline) and in which color, plus
generalization and routing settings. Defaults are in `style.py`, presets in
`styles/*.json`, which only need to contain the differences:

| Style | Description |
|---|---|
| `toner-water` | black, water as a blue fill (2 threads, default) |
| `toner` | single color, water as an open fill |
| `minimal` | roads and shorelines only |
| `colorful` | dark roads, blue water, green parks, grey buildings (4 threads) |

Routing `continuous` (default) connects as much as possible via travel paths
that are stitched over later, which saves most thread trims; `balanced` trims
earlier.

## Test pattern

    uv run testpattern.py -o out/testpattern

16 numbered fields (92 × 92 mm) with variants of satin width and spacing,
junctions, a canal between satin roads, running stitch repeats and stitch
lengths, fills and contour fills. The parameters of each field are written to
`out/testpattern_legend.txt`.

## Files

| File | Purpose |
|---|---|
| `osm2stitch.py` | generalization, stitch types, routing, SVG for Ink/Stitch and quick view |
| `style.py`, `styles/` | style schema, presets, validation |
| `sources.py` | data sources: Postpass with cache, fallback |
| `osmindex.py` | PBF → SQLite/R-tree index |
| `extract.py` | PBF → JSON extract for the command line |
| `testpattern.py` | test pattern with parameter variants |
| `app.py` | FastAPI server, queue, Ink/Stitch calls, statistics, ZIP |
| `web/index.html` | web interface (Leaflet map with frame, style editor, preview, downloads) |

## Outlook

- A self-hosted PostGIS database as data source instead of Postpass.

## License

Code: [MIT](LICENSE). Map data © OpenStreetMap contributors,
[ODbL](https://www.openstreetmap.org/copyright).
