# osm2stitch

Turns OpenStreetMap data into embroidery files. The map is generalized and
converted into an SVG for [Ink/Stitch](https://inkstitch.org), which then
produces the embroidery file (PES, DST, JEF, …) and a realistic preview.

## Setup

    uv sync

Ink/Stitch must be installed in `~/.config/inkscape/extensions/inkstitch`; it is
called headless via `bin/inkstitch`.

## Web app

    uv run app.py                    # http://127.0.0.1:8000

Pick a section on the map, choose hoop size, scale and style, and download the
embroidery file or a ZIP with the SVG, preview, style settings and attribution.
Jobs are processed one after another through a queue; results are stored in
`web_jobs/<id>/`.

## How a map is built

1. **Data:** one SQL query to Postpass (Geofabrik's public OSM PostGIS API),
   filtered to the tags that are used. Responses are cached with some margin in
   `data/cache/postpass/` (24 h by default), so style changes and small shifts
   need no new query.
2. **Classification:** roads by class, railways, water, green areas (parks,
   gardens, playgrounds, forest, cemeteries) and buildings. Tunnels, covered and
   underground objects are skipped, except short passages (up to 100 m, e.g.
   under a wide bridge) that connect visible lines at both ends.
3. **Projection:** UTM via pyproj, scaled to mm on the fabric.
4. **Lines** (shapely): parallel lines of a class (dual carriageways, track
   bundles) are merged into one centerline via a Voronoi skeleton; lines running
   along a higher class are dropped; satin roads end at crossing satin roads and
   are split at sharp bends.
5. **Areas:** kept clear of lines, cleaned by morphological opening/closing,
   simplified, small parts and holes dropped; buildings merged into blocks.
6. **Stitch types** per layer from the style: satin, running stitch, track
   (rails with sleepers), tatami fill, contour fill (as a single spiral where
   possible) or outline.
7. **Routing** (networkx): all lines form a network; objects are ordered
   greedily, connected by travel paths over lines that are stitched over later,
   with a thread trim only where no such path exists. Order: areas, running
   stitches, satins, grouped by color.
8. **Stitches:** the SVG with Ink/Stitch attributes is passed to the Ink/Stitch
   CLI, which computes the embroidery file and the preview in parallel.
   pyembroidery reads the result for stitch count, trims and estimated time.

## Styles

A style defines for each layer (major, tertiary and minor roads, railways,
water, parks, buildings, frame) whether and how it is stitched and in which
color, plus generalization and routing settings. Defaults are in `style.py`,
presets in `styles/*.json`, which only need to contain the differences:

| Style | Description |
|---|---|
| `toner-water` | black, water as a blue fill (2 threads, default) |
| `toner` | single color, water as an open fill |
| `minimal` | roads and shorelines only |
| `colorful` | dark roads, blue water, green parks, grey buildings (4 threads) |

Routing `continuous` (default) connects as much as possible via travel paths;
`balanced` trims earlier.

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
| `sources.py` | data sources: Postpass with cache, optional local fallback |
| `osmindex.py` | PBF → SQLite/R-tree index for the local fallback |
| `extract.py` | PBF → JSON extract for `osm2stitch.py` on the command line |
| `testpattern.py` | test pattern with parameter variants |
| `app.py` | FastAPI server, queue, Ink/Stitch calls, statistics, ZIP |
| `web/index.html` | web interface (Leaflet map with frame, style editor, preview, downloads) |

## License

Code: [MIT](LICENSE). Map data © OpenStreetMap contributors,
[ODbL](https://www.openstreetmap.org/copyright).
