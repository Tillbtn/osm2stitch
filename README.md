# OSM → Stickdatei (Prototyp)

Erzeugt aus OpenStreetMap-Daten eine Ink/Stitch-fertige SVG und daraus direkt
eine Stickdatei (PES, DST, …) samt realistischer Vorschau – ohne Inkscape-GUI.

## Einrichtung

    uv sync

Ink/Stitch muss unter `~/.config/inkscape/extensions/inkstitch` installiert sein
(wird headless über `bin/inkstitch` aufgerufen).

## Benutzung

1. Ausschnitt aus der PBF ziehen (einmalig, ~20 s für Berlin):

       uv run extract.py berlin-latest.osm.pbf --lat 52.498751 --lon 13.381239 -o kreuzberg.json

2. Karte erzeugen (schnell, beliebig oft mit anderen Parametern):

       uv run osm2stitch.py kreuzberg.json -o out/kreuzberg
       uv run osm2stitch.py kreuzberg.json -o out/kreuzberg_blau --water-color "#2f6fb0"

   Mit Gebäuden lieber einen kleineren Ausschnitt wählen:

       uv run osm2stitch.py kreuzberg.json -o out/kreuzberg_800 --width-m 800 --water-color "#2f6fb0"

   Optionen: `--width-m` (Ausschnittbreite in Metern, Standard 1600),
   `--buildings outline|fill|none` (Gebäudeblöcke als Umriss, Standard; offene
   Füllung – zerfasert bei schmalen Blöcken; oder weglassen),
   `--size` (Stickgröße in mm, Standard `100x100`), `--format` (pes, dst, jef, …),
   `--no-inkstitch` (nur SVG).

Ergebnis: `out/<name>.svg` (in Inkscape nachbearbeitbar), `out/<name>.pes`,
`out/<name>_vorschau.png`.

Stilparameter (Satinbreiten, Mindestgrößen, Vereinfachung) stehen in `STYLE`
in `osm2stitch.py`.

## Demo-Website (lokal)

1. Einmalig den räumlichen Index aus der PBF bauen (~75 s, ~270 MB für Berlin):

       uv run osmindex.py /pfad/zu/berlin-latest.osm.pbf data/berlin.sqlite

2. Server starten und http://127.0.0.1:8000 öffnen:

       uv run app.py --db data/berlin.sqlite

Ablauf pro Auftrag: Ausschnitt aus dem Index (~0,05 s) → Generalisierung und
Routing in Python (<1 s) → Ink/Stitch berechnet Stickdatei und Vorschau
parallel (~5–15 s). Aufträge laufen nacheinander über eine Warteschlange; die
Ergebnisse liegen in `web_jobs/<id>/` (Stickdatei, SVG, Vorschau, ZIP mit
LIESMICH inkl. OSM-Quellenangabe).

Aufbau:

| Datei | Aufgabe |
|---|---|
| `osmindex.py` | PBF → SQLite/R-Tree; `Index.query()` liefert Ausschnitte (später austauschbar gegen PostGIS) |
| `osm2stitch.py` | Generalisierung, Stickarten, Routing, SVG für Ink/Stitch + Schnellansicht |
| `app.py` | FastAPI-Server, Warteschlange, Ink/Stitch-Aufruf, Statistik, ZIP |
| `web/index.html` | Oberfläche (Leaflet-Karte mit Rahmen, Einstellungen, Vorschau, Downloads) |
