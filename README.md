# commodix-data

Tagesdaten für **CommodiX – Rohstoff-Signale** auf https://manuel360finanz.de/commodix/ (automatisch erzeugt, regelbasiert, keine Anlageberatung).

- `commodix-data.json` – aktueller Datenstand, den die Webseite abholt (`/wp-json/m360/v1/commodix/sync-tick`).
  Feld `status`: `{date, time, final}` – `final:false` = Zwischenstand während der US-Sitzung, `final:true` = Tagesschluss.
- `engine/` – Signal-Engine (Python) und Datenbestand.
  - `refresh.py` übernimmt frische IBKR-Werte aus `engine/input/today.json` (Format im Kopf der Datei), rechnet alle 12 Werte neu
    und schreibt `commodix-data.json`, `engine/slv-signal.html` (Claude-Dashboard) und `engine/commodix.html` (Webseiten-App).
  - `slv_signal_engine.py`, `build_dashboard.py`, `dashboard_template.html`, `data/` – Engine, Build und Kursdaten.

Ablauf je Lauf: IBKR-Daten holen → `engine/input/today.json` schreiben → `cd engine && python3 refresh.py` → committen und pushen → Webseite per `sync-tick` anstoßen.
Telegram meldet nur Signalwechsel zum Tagesschluss (`final:true`).
