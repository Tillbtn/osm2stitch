"""Stickstile: welche Objektart wie, in welcher Farbe und ob überhaupt gestickt wird.

Ein Stil ist ein verschachteltes dict (bzw. eine JSON-Datei unter styles/), das nur
die Abweichungen von DEFAULT enthalten muss. `load_style(name, overrides)` führt
Standard, Voreinstellung und Änderungen (z.B. aus dem Web-Editor) zusammen und
prüft das Ergebnis.
"""
import copy
import json
import re
from pathlib import Path

STYLES_DIR = Path(__file__).parent / "styles"

# Linienklassen in absteigender Priorität: höhere schneiden/verdrängen niedrigere
LINE_LAYERS = ("major", "medium", "minor", "rail")
AREA_LAYERS = ("water", "green", "buildings")
LAYERS = LINE_LAYERS + AREA_LAYERS + ("frame",)

STITCHES = {
    "line": ("satin", "run"),
    "area": ("tatami", "contour", "outline"),
    "frame": ("run",),
}

DEFAULT = {
    "name": "Standard",
    "description": "",
    "fabric": {"color": "#f5f2ea", "pull_compensation": 0.15},
    "routing": {"mode": "continuous"},
    "layers": {
        "major": {"label": "Hauptstraßen", "enabled": True, "color": "#1a1a1a",
                  "stitch": "satin", "width": 1.3, "density": 0.4, "repeats": 3, "stitch_length": 2.0},
        "medium": {"label": "Tertiärstraßen", "enabled": True, "color": "#1a1a1a",
                   "stitch": "satin", "width": 0.9, "density": 0.4, "repeats": 3, "stitch_length": 2.0},
        "minor": {"label": "Nebenstraßen", "enabled": True, "color": "#1a1a1a",
                  "stitch": "run", "width": 0.8, "density": 0.4, "repeats": 3, "stitch_length": 2.0},
        "rail": {"label": "Bahn", "enabled": True, "color": "#1a1a1a",
                 "stitch": "run", "width": 0.8, "density": 0.4, "repeats": 3, "stitch_length": 2.0},
        "water": {"label": "Gewässer", "enabled": True, "color": "#2f6fb0",
                  "stitch": "tatami", "row_spacing": 0.25, "angle": 0, "underlay": True,
                  "repeats": 3, "stitch_length": 3.0},
        "green": {"label": "Parks & Wald", "enabled": True, "color": "#1a1a1a",
                  "stitch": "contour", "row_spacing": 1.8, "angle": 0, "underlay": False,
                  "repeats": 1, "stitch_length": 2.5},
        "buildings": {"label": "Gebäude", "enabled": True, "color": "#1a1a1a",
                      "stitch": "outline", "row_spacing": 0.8, "angle": 135, "underlay": False,
                      "repeats": 1, "stitch_length": 1.5},
        "frame": {"label": "Rahmenlinie", "enabled": True, "color": "#1a1a1a",
                  "stitch": "run", "repeats": 3, "stitch_length": 2.5},
    },
    "generalization": {
        "simplify": 0.2,             # Douglas-Peucker-Toleranz
        "min_line": 2.0,             # kürzere Linienstücke fallen weg
        "min_area": 4.0,             # kleinere Flächen/Löcher fallen weg
        "min_feature": 0.6,          # schmalere Flächenteile fallen weg
        "building_merge": 0.6,       # Gebäude mit kleinerem Abstand verschmelzen zu Blöcken
        "min_building": 2.0,         # kleinere Gebäudeblöcke/Innenhöfe fallen weg
        "road_clearance": 0.2,       # Abstand Füllflächen <-> Linien
        "parallel_min_length": 4.0,  # ab dieser Länge gilt eine Überlappung als Parallele
    },
}

# Routing-Modi: wie viel Laufweg ist ein Fadenschnitt wert?
ROUTING = {
    "balanced": {"jump_distance": 1.0, "snap_distance": 1.0, "ring_snap": 2.0,
                 "max_travel": 25.0, "trim_penalty": 15.0},
    "continuous": {"jump_distance": 1.0, "snap_distance": 1.5, "ring_snap": 3.0,
                   "max_travel": 150.0, "trim_penalty": 100.0},
}

RANGES = {
    "width": (0.4, 4.0), "density": (0.25, 1.0), "stitch_length": (1.0, 6.0),
    "row_spacing": (0.2, 5.0), "angle": (-180, 180), "pull_compensation": (0.0, 1.0),
}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def layer_type(name):
    return "line" if name in LINE_LAYERS else "area" if name in AREA_LAYERS else "frame"


def deep_merge(base, update):
    out = copy.deepcopy(base)
    for k, v in (update or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def validate(style):
    errors = []
    unknown = set(style["layers"]) - set(LAYERS)
    if unknown:
        errors.append(f"Unbekannte Ebenen: {', '.join(sorted(unknown))}")
    if style["routing"]["mode"] not in ROUTING:
        errors.append(f"Unbekannter Routing-Modus: {style['routing']['mode']}")
    if not HEX.match(str(style["fabric"]["color"])):
        errors.append("Stofffarbe muss #rrggbb sein")
    for name in LAYERS:
        cfg = style["layers"][name]
        label = cfg.get("label", name)
        if cfg["stitch"] not in STITCHES[layer_type(name)]:
            errors.append(f"{label}: Stickart '{cfg['stitch']}' nicht möglich")
        if not HEX.match(str(cfg["color"])):
            errors.append(f"{label}: Farbe muss #rrggbb sein")
        if cfg.get("repeats", 1) not in (1, 3, 5):
            errors.append(f"{label}: Wiederholungen müssen 1, 3 oder 5 sein")
        for k, (lo, hi) in RANGES.items():
            v = cfg.get(k)
            if v is not None and not (isinstance(v, (int, float)) and lo <= v <= hi):
                errors.append(f"{label}: {k} muss zwischen {lo} und {hi} liegen")
    pc = style["fabric"]["pull_compensation"]
    if not (isinstance(pc, (int, float)) and 0 <= pc <= 1):
        errors.append("Zugausgleich muss zwischen 0 und 1 mm liegen")
    if errors:
        raise ValueError("; ".join(errors))
    return style


def list_presets():
    """{id: vollständiger Stil} für alle JSON-Dateien unter styles/."""
    return {p.stem: load_style(p.stem) for p in sorted(STYLES_DIR.glob("*.json"))}


def load_style(name_or_path="toner-wasser", overrides=None):
    path = Path(name_or_path)
    if not path.suffix:
        path = STYLES_DIR / f"{name_or_path}.json"
    if not path.exists():
        raise ValueError(f"Unbekannter Stil: {name_or_path}")
    style = deep_merge(DEFAULT, json.loads(path.read_text()))
    return validate(deep_merge(style, overrides))


def set_path(overrides, dotted, value):
    """'layers.water.color' = '#ff0000' -> verschachteltes dict (für die Kommandozeile)."""
    *parents, last = dotted.split(".")
    node = overrides
    for p in parents:
        node = node.setdefault(p, {})
    try:
        node[last] = json.loads(value)
    except json.JSONDecodeError:
        node[last] = value
    return overrides
