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

       uv run osm2stitch.py kreuzberg.json -o out/kreuzberg --width-m 800
       uv run osm2stitch.py kreuzberg.json -o out/bunt --width-m 800 --style bunt
       uv run osm2stitch.py kreuzberg.json -o out/test --set layers.major.width=1.6 --set routing.mode=balanced

   Optionen: `--width-m` (Ausschnittbreite in Metern), `--size` (Stickgröße in mm,
   Standard `100x100`), `--style` (Voreinstellung aus `styles/` oder Pfad zu einer
   JSON-Datei), `--set PFAD=WERT` (einzelne Stilwerte ändern), `--format` (pes,
   dst, jef, …), `--no-inkstitch` (nur SVG).

Ergebnis: `out/<name>.svg` (in Inkscape nachbearbeitbar), `out/<name>.pes`,
`out/<name>_vorschau.png`.

## Stile

Ein Stil legt pro Ebene (Haupt-, Tertiär-, Nebenstraßen, Bahn, Gewässer, Parks,
Gebäude, Rahmen) fest, ob, wie (Satin, Geradstich, Füllung, Konturfüllung,
Umriss) und in welcher Farbe gestickt wird, dazu Generalisierung und
Fadenführung. Standardwerte stehen in `style.py`, Voreinstellungen in
`styles/*.json` – dort genügen die Abweichungen vom Standard:

| Stil | Beschreibung |
|---|---|
| `toner-wasser` | Schwarz wie Stamen Toner, Wasser blau (2 Garne, Standard) |
| `toner` | einfarbig, Wasser als offene Füllung |
| `minimal` | nur Straßen und Uferlinien |
| `bunt` | Straßen dunkel, Wasser blau, Parks grün, Gebäude grau (4 Garne) |

Fadenführung `continuous` (Standard) verbindet möglichst alles über Laufwege, die
später überstickt werden (~70 % weniger Fadenschnitte); `balanced` schneidet
früher.

## Testmuster

    uv run testmuster.py -o out/testmuster

16 nummerierte Felder (92 × 92 mm) mit Varianten von Satinbreite und -dichte,
Kreuzung, Kanal zwischen Satinstraßen, Geradstich-Wiederholungen und
Stichlängen, Füllungen und Konturfüllung. Die Parameter je Feld stehen in
`out/testmuster_legende.txt`. Nach dem Probesticken die besten Werte in den Stil
übernehmen.

## Demo-Website (lokal)

1. Einmalig den räumlichen Index aus der PBF bauen (~75 s, ~270 MB für Berlin):

       uv run osmindex.py /pfad/zu/berlin-latest.osm.pbf data/berlin.sqlite

2. Server starten und http://127.0.0.1:8000 öffnen:

       uv run app.py --db data/berlin.sqlite

Ablauf pro Auftrag: Ausschnitt aus dem Index (~0,05 s) → Generalisierung und
Routing in Python (<1 s) → Ink/Stitch berechnet Stickdatei und Vorschau
parallel (~5–15 s). Aufträge laufen nacheinander über eine Warteschlange; die
Ergebnisse liegen in `web_jobs/<id>/` (Stickdatei, SVG, Vorschau, verwendeter
Stil als `stil.json`, ZIP mit LIESMICH inkl. OSM-Quellenangabe).

Aufbau:

| Datei | Aufgabe |
|---|---|
| `osmindex.py` | PBF → SQLite/R-Tree; `Index.query()` liefert Ausschnitte (später austauschbar gegen PostGIS) |
| `style.py`, `styles/` | Stil-Schema, Voreinstellungen, Prüfung |
| `osm2stitch.py` | Generalisierung, Stickarten, Routing, SVG für Ink/Stitch + Schnellansicht |
| `testmuster.py` | Probestick-Muster mit Parametervarianten |
| `app.py` | FastAPI-Server, Warteschlange, Ink/Stitch-Aufruf, Statistik, ZIP |
| `web/index.html` | Oberfläche (Leaflet-Karte mit Rahmen, Einstellungen, Vorschau, Downloads) |
