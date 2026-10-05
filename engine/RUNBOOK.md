# CommodiX – Anleitung für einen Aktualisierungslauf

Gilt für die geplanten Aufgaben **„CommodiX Signaldienst“ (22:50, Tagesschluss)** und **„CommodiX Zwischenstand (stündlich)“**.
Keine Orders platzieren. Regelbasiert, keine Anlageberatung. Antworten auf Deutsch.

## 0. Repository
`add_repo` (owner `mgfinanz`, repo `commodix-data`, access `push`), dann wie angegeben klonen. Arbeitsverzeichnis: `engine/`.
Python-Pakete: `pip install pandas numpy --break-system-packages` falls nötig.

## 1. Modus bestimmen
- **Tagesschluss** (`final: true`): 22:50-Lauf. Datum = heutiger US-Handelstag. Aktualisiert Repository, Webseite (wie bisher) und Claude-Dashboard.
- **Zwischenstand** (`final: false`): stündlicher Lauf oder manueller Start aus dem Claude-Dashboard („Jetzt neu berechnen“).
  Aktualisiert **nur das Claude-Dashboard** – nichts committen, nichts pushen, Webseite nicht anstoßen.
  - Läuft die US-Sitzung (Mo–Fr 15:30–22:00 Berlin): Tageskerze bis jetzt, `final:false`, `time` = jetzt.
  - Außerhalb der Sitzung (manueller Start): letzte abgeschlossene Kerze nehmen; ist ihr Datum = `last` in `engine/slv_daily.json`,
    ist der Stand schon aktuell → trotzdem mit `final:true` und `time` = "" rechnen und das Dashboard veröffentlichen (kein Push).
- US-Feiertag/Wochenende ohne manuellen Start → nichts tun.

## 2. IBKR-Daten (Interactive_Brokers_IBKR, per ToolSearch laden)
Werte und contract_ids: SLV 39039301 · GLD 51529211 · GDX 229726316 · GDXJ 229726197 · SIL 211651690 · SILJ 680887855 ·
CPER 97462781 · URA 211651685 · USO 418893644 · UNG 676612559 · MP 455592408 · NB 620917098.

Je Wert:
- `get_price_history` (STK, step ONE_DAY, step_count 2, outside_rth false) → Kerze des heutigen Tages (o/h/l/c). Prüfen, dass die vorletzte Kerze
  dem letzten Tag in `engine/slv_daily.json` (`last`) bzw. dem letzten Wert in `engine/data/<T>.json` entspricht. Fehlen dazwischen Tage
  (z. B. ausgefallener Lauf), die fehlenden Tage zuerst nachtragen: dafür `refresh.py` je fehlendem Tag mit `final:true` laufen lassen (ältester zuerst).
- `get_price_snapshot` mit `last, underlying_today_option_volume, underlying_avg_option_volume, implied_vol_underlying, implied_volatility_percentile, historical_vol`.
  Schlusskurs der Kerze beim Zwischenstand = `last` aus dem Snapshot (h/l entsprechend erweitern).

SLV-Optionskette: `get_option_parameters` (39039301) → Monatsverfall mit 80–130 Tagen Restlaufzeit, am nächsten an 100 →
`get_option_data` mit Strikes von Kurs+8 % bis Kurs+18 % (Puts) → je Put `get_price_snapshot` mit `bid_ask, option_open_interest`.
Im Signal „KAUFEN“ zusätzlich Calls von Kurs−12 % bis Kurs−4 %.

Nur Tagesschluss: Websuche (nur belegte Werte, sonst weglassen) für US 10J nominal, 10J-TIPS-Realzins (Fed H.15), Fed-Funds-Bereich und
letzte Fed-Richtung, Silber-/Gold-Spot, Shanghai-SGE-Silber in USD/oz und Prämie (metalcharts.org/shanghai).

## 3. Eingabedatei `engine/input/today.json`
Format steht im Kopf von `engine/refresh.py`. Zusätzlich:
- `"time"`: Uhrzeit Berlin (HH:MM).
- `"pc"`: beim Zwischenstand `null` (Tagesvolumen noch zu dünn), beim Tagesschluss `[callVolume, putVolume]` des Tages.
- `"pc_avg"`: `[avgCallVolume, avgPutVolume]`.
- `"iv"` = implied_vol_underlying.annual_iv, `"hv"` = historical_vol.annual_pct, `"ivp"` = implied_volatility_percentile.high_52w.
- `"slv_chain"`: `{"expiry":"JJJJ-MM-TT","puts":[[K,bid,ask,oi],…],"calls":[…]}`.
- `"macro"`: nur beim Tagesschluss, nur belegte Werte.

## 4. Rechnen
`cd engine && python3 refresh.py` rechnet zweimal:
- **bisherige Regeln** (`RULE_SET="legacy"`, alle Werte wie SLV: Score ±25 %, SMA 200, Stop 2×ATR, Historie ab 25.07.2025)
  → `../commodix-data.json` = Webseite + Telegram (unverändert).
- **optimierte Regeln je Wert** (`RULES` in `slv_signal_engine.py`, SLV unverändert, UNG/NB nur beobachten, 5 Jahre Historie)
  → `slv-signal.html` = Claude-Dashboard (und `commodix-dashboard.json`, nicht eingecheckt).
Ausgabe prüfen: 12 Zeilen mit beiden Spalten, Kurse plausibel (Abweichung zum IBKR-Kurs < 0,5 %).
Die Datendateien `engine/data/<T>.json` haben ein Datumsfeld `t`; `refresh.py` ersetzt die Kerze, wenn `t[-1]` = heutiges Datum, sonst hängt es an.
Regeln nur nach neuer Prüfung mit `research/optimize.py` ändern (Lernzeitraum bis 31.03.2025, Prüfzeitraum danach).

## 5. Veröffentlichen
Zwischenstand: nur Schritt 3. Tagesschluss: alle Schritte.
1. `git add -A`, Commit „CommodiX {Zwischenstand HH:MM | Tagesschluss} {Datum}“, `git push origin main`
   (vorher `git fetch origin main` und ggf. rebase).
2. WebFetch `https://manuel360finanz.de/wp-json/m360/v1/commodix/sync-tick?d={JJJJ-MM-TT-HHMM}` → Feld `status`.
   Erwartet „übernommen …“. Bei „aktuell (…)“ oder „Datei bei GitHub nicht erreichbar“ 3 Minuten warten, genau einmal wiederholen.
   Telegram meldet die Webseite selbst – nur beim Tagesschluss und nur bei Signalwechseln.
3. Claude-Dashboard (Artifact https://claude.ai/artifact/4TwQBp6UJjFrVB2PhZ4zdY): Artifact `read`, dann mit `url` und `file_path` = `engine/slv-signal.html` veröffentlichen.
4. Nur wenn GitHub-Push unmöglich ist und es der Tagesschluss ist: Rückfall über Claude in Chrome wie früher
   (wp-admin/media-new.php, Datei-Input, POST /wp-json/m360/v1/commodix mit `{data}` und X-WP-Nonce, genau einmal).

Nicht ändern: WPCode-Snippets, Seiten, App-Datei auf der Webseite (die Webseite bleibt unverändert; sie bekommt nur den Tagesschluss). Nicht aufrufen: `/commodix/telegram-test`, `/commodix/telegram-post`.

## 6. Nachricht an Manuel (SendUserMessage)
- **Tagesschluss:** Übersichtstabelle aller 12 Werte mit zwei Signal-Spalten „Dashboard (optimiert)“ und „Webseite/Telegram (bisherig)“ (Score, über/unter Trendfilter, Umkehr, Order je Modus mit Gewinnmitnahme- und Stop-Loss-Marke),
  Änderungen gegenüber dem Vortag („⚠ SIGNALWECHSEL“, „⚠ UMKEHRPUNKT“, „⚠ NEUER TRADE“, „⚠ STOP AUSGELÖST“, „⚠ GEWINNMITNAHME“,
  „⚠ STOP-LOSS OPTION“, „⚠ ROLLEN FÄLLIG“), drei Treiber für SLV, eine Zeile Webseite/Telegram (sync-status).
- **Zwischenstand:** nach einem manuellen Start aus dem Dashboard immer eine Zeile („Dashboard neu berechnet HH:MM“ + Signalwechsel). Sonst nur senden, wenn sich ein Signal gegenüber dem letzten Tagesschluss geändert hat oder ein Stop vorbörslich/intraday überschritten ist
  (eine kurze Zeile je Wert, Kennzeichnung „vorläufig“). Sonst keine Nachricht.
