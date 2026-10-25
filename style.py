"""Embroidery styles: whether, how and in which color each object type is stitched.

A style is a nested dict (or a JSON file under styles/) that only needs to contain
the deviations from DEFAULT. `load_style(name, overrides)` merges default, preset
and changes (e.g. from the web editor) and validates the result.
"""
import copy
import json
import re
from pathlib import Path

STYLES_DIR = Path(__file__).parent / "styles"

# Line classes in descending priority: higher ones cut/displace lower ones
LINE_LAYERS = ("major", "medium", "minor", "rail")
AREA_LAYERS = ("water", "green", "buildings")
LAYERS = LINE_LAYERS + AREA_LAYERS + ("frame",)

STITCHES = {
    "line": ("satin", "run", "track"),
    "area": ("tatami", "contour", "outline"),
    "frame": ("run",),
}

DEFAULT = {
    "name": "Default",
    "description": "",
    "fabric": {"color": "#f5f2ea", "pull_compensation": 0.15},
    "routing": {"mode": "continuous"},
    "layers": {
        "major": {"label": "Major roads", "enabled": True, "color": "#1a1a1a",
                  "stitch": "satin", "width": 1.3, "density": 0.4, "tie_spacing": 1.6, "repeats": 3, "stitch_length": 2.0},
        "medium": {"label": "Tertiary roads", "enabled": True, "color": "#1a1a1a",
                   "stitch": "satin", "width": 0.9, "density": 0.4, "tie_spacing": 1.6, "repeats": 3, "stitch_length": 2.0},
        "minor": {"label": "Minor roads", "enabled": True, "color": "#1a1a1a",
                  "stitch": "run", "width": 0.8, "density": 0.4, "tie_spacing": 1.6, "repeats": 3, "stitch_length": 2.0},
        "rail": {"label": "Railways", "enabled": True, "color": "#1a1a1a",
                 "stitch": "track", "width": 1.2, "density": 0.4, "tie_spacing": 1.6, "repeats": 1, "stitch_length": 2.0},
        "water": {"label": "Water", "enabled": True, "color": "#2f6fb0",
                  "stitch": "tatami", "row_spacing": 0.25, "angle": 0, "underlay": True,
                  "repeats": 3, "stitch_length": 3.0},
        "green": {"label": "Parks & forest", "enabled": True, "color": "#1a1a1a",
                  "stitch": "contour", "row_spacing": 1.8, "angle": 0, "underlay": False,
                  "repeats": 1, "stitch_length": 2.5},
        "buildings": {"label": "Buildings", "enabled": True, "color": "#1a1a1a",
                      "stitch": "outline", "row_spacing": 0.8, "angle": 135, "underlay": False,
                      "repeats": 1, "stitch_length": 1.5},
        "frame": {"label": "Frame", "enabled": True, "color": "#1a1a1a",
                  "stitch": "run", "repeats": 3, "stitch_length": 2.5},
    },
    "generalization": {
        "simplify": 0.2,             # Douglas-Peucker tolerance
        "min_line": 2.0,             # shorter line pieces are dropped
        "min_area": 4.0,             # smaller areas/holes are dropped
        "min_feature": 0.6,          # narrower area parts are dropped
        "building_merge": 0.6,       # buildings closer than this merge into blocks
        "min_building": 2.0,         # smaller building blocks/courtyards are dropped
        "road_clearance": 0.2,       # clearance between fill areas <-> lines
        "parallel_min_length": 4.0,  # overlaps from this length on count as parallel
    },
}

# Routing modes: how much travel stitching is a thread trim worth?
ROUTING = {
    "balanced": {"jump_distance": 1.0, "snap_distance": 1.0, "ring_snap": 2.0,
                 "max_travel": 25.0, "trim_penalty": 15.0},
    "continuous": {"jump_distance": 1.0, "snap_distance": 1.5, "ring_snap": 3.0,
                   "max_travel": 150.0, "trim_penalty": 100.0},
}

RANGES = {
    "width": (0.4, 4.0), "density": (0.25, 1.0), "stitch_length": (1.0, 6.0),
    "tie_spacing": (0.8, 5.0), "row_spacing": (0.2, 5.0), "angle": (-180, 180), "pull_compensation": (0.0, 1.0),
}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
FIELD_NAMES = {"width": "width", "density": "stitch spacing", "stitch_length": "stitch length",
               "tie_spacing": "sleeper spacing", "row_spacing": "row spacing", "angle": "angle",
               "pull_compensation": "pull compensation"}


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
        errors.append(f"Unknown layers: {', '.join(sorted(unknown))}")
    if style["routing"]["mode"] not in ROUTING:
        errors.append(f"Unknown routing mode: {style['routing']['mode']}")
    if not HEX.match(str(style["fabric"]["color"])):
        errors.append("Fabric color must be #rrggbb")
    for name in LAYERS:
        cfg = style["layers"][name]
        label = cfg.get("label", name)
        if cfg["stitch"] not in STITCHES[layer_type(name)]:
            errors.append(f"{label}: stitch type '{cfg['stitch']}' not allowed")
        if not HEX.match(str(cfg["color"])):
            errors.append(f"{label}: color must be #rrggbb")
        if cfg.get("repeats", 1) not in (1, 3, 5):
            errors.append(f"{label}: repeats must be 1, 3 or 5")
        for k, (lo, hi) in RANGES.items():
            v = cfg.get(k)
            if v is not None and not (isinstance(v, (int, float)) and lo <= v <= hi):
                errors.append(f"{label}: {FIELD_NAMES[k]} must be between {lo} and {hi}")
    pc = style["fabric"]["pull_compensation"]
    if not (isinstance(pc, (int, float)) and 0 <= pc <= 1):
        errors.append("Pull compensation must be between 0 and 1 mm")
    if errors:
        raise ValueError("; ".join(errors))
    return style


def list_presets():
    """{id: complete style} for all JSON files under styles/."""
    return {p.stem: load_style(p.stem) for p in sorted(STYLES_DIR.glob("*.json"))}


def load_style(name_or_path="toner-water", overrides=None):
    path = Path(name_or_path)
    if not path.suffix:
        path = STYLES_DIR / f"{name_or_path}.json"
    if not path.exists():
        raise ValueError(f"Unknown style: {name_or_path}")
    style = deep_merge(DEFAULT, json.loads(path.read_text()))
    return validate(deep_merge(style, overrides))


def set_path(overrides, dotted, value):
    """'layers.water.color' = '#ff0000' -> nested dict (for the command line)."""
    *parents, last = dotted.split(".")
    node = overrides
    for p in parents:
        node = node.setdefault(p, {})
    try:
        node[last] = json.loads(value)
    except json.JSONDecodeError:
        node[last] = value
    return overrides
