"""Baut slv-signal.html (CommodiX) aus signals_all.json."""
import json

ALL = json.load(open("signals_all.json"))
ORDER = ["SLV", "GLD", "GDX", "GDXJ", "SIL", "SILJ", "CPER", "URA", "USO", "UNG", "MP", "NB"]
TECH = ("MACD", "ADX/DMI", "Trendflex", "Reflex", "Coppock", "RSI")

def fmt(v, d=2):
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")

for t, s in ALL.items():
    I, C, L = s["indicators"], s["context"], s["levels"]
    dates = s["backtest"]["dates"]
    s["history"]["dates"] = dates[-len(s["history"]["close"]):]
    for k, v in s["components"].items():
        v["text"] = s["component_texts"].get(k, "")
        v["group"] = "Technik" if k in TECH else "Umfeld"
    # Treiber für die Zusammenfassung
    w = sorted(((k, v["score"] * v["weight"]) for k, v in s["components"].items()), key=lambda x: x[1])
    neg = [k for k, x in w if x < 0][:2]; pos = [k for k, x in reversed(w) if x > 0][:2]
    tneg = sum(1 for k in TECH if s["components"][k]["score"] < 0)
    above = L["last"] > L["sma200"]
    summ = (f"Gesamtscore {fmt(s['score_pct'], 1)} %. {tneg} von 6 Technik-Bausteinen negativ, "
            f"Kurs {'über' if above else 'unter'} SMA 200 ({fmt(L['sma200'])}), damit ist die "
            f"{'Long' if above else 'Short'}-Seite freigegeben.")
    if neg: summ += " Belastend: " + ", ".join(neg) + "."
    if pos: summ += " Stützend: " + ", ".join(pos) + "."
    s["summary"] = summ
    rows = []
    if "us10y" in C:
        rows += [["Fed Funds", C["fed_funds_range"] + " (↑ 16.09.)"], ["US 10J nominal", fmt(C["us10y"]) + " %"],
                 ["US 10J real (TIPS)", fmt(C["us10y_real"]) + " %"]]
    if t == "SLV":
        rows += [["Silber Spot", fmt(C["silver_spot"]) + " $"],
                 ["Shanghai SGE", fmt(C["sge_silver_usd"]) + f" $ (+{fmt(C['sge_premium_pct'], 1)} %)"],
                 ["Gold/Silber-Ratio", fmt(I["gsr"], 1)]]
    if "Metall-Trend" in s["components"]:
        rows += [["Metall-Trend", s["component_texts"]["Metall-Trend"].split(":")[0]]]
    rows += [["Put/Call heute / Ø", fmt(I["pc_today"]) + " / " + fmt(I["pc_avg"])],
             ["IV / HV 30T", fmt(I["iv"], 1) + " % / " + fmt(I["hv30"], 1) + " %"],
             ["IV-Perzentil 52W", fmt(I["iv_pctl_52w"], 0) + " %"]]
    if C.get("thin_options"):
        rows += [["Optionsmarkt", "dünn (Ø < 5.000 Kontrakte/Tag)"]]
    if I.get("coppock_monthly_equiv") is not None:
        rows += [["Coppock Monat (äquiv.)", fmt(I["coppock_monthly_equiv"], 1)]]
    s["context_rows"] = rows

payload = {"order": ORDER, "assets": ALL}
import os
if os.path.exists("status.json"):                       # von refresh.py: Zwischenstand oder Tagesschluss
    st = json.load(open("status.json"))
    payload["status"] = st
    for _t in ORDER:
        ALL[_t]["asof"] = st["date"]; ALL[_t]["asof_time"] = st.get("time", ""); ALL[_t]["preliminary"] = not st.get("final")
html = open("dashboard_template.html").read().replace("/*DATA*/null", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
open("slv-signal.html", "w").write(html)
print("ok", len(html) // 1024, "KB")

# ---- Webseiten-Fassung (manuel360finanz.de/commodix): App ohne Daten + Daten-JSON für die REST-API
import re
tpl = open("dashboard_template.html").read()
tpl = tpl.replace('<link rel="preconnect" href="https://fonts.googleapis.com">\n', '')
tpl = re.sub(r'<link rel="stylesheet" href="https://fonts.googleapis.com[^>]*>\n', '', tpl)
assert "googleapis" not in tpl
k = tpl.index("</style>") + len("</style>")
web = ('<!doctype html>\n<html lang="de">\n<head>\n<meta charset="utf-8">\n'
       '<meta name="viewport" content="width=device-width, initial-scale=1">\n<meta name="robots" content="noindex">\n'
       + tpl[:k] + "\n</head>\n<body>\n" + tpl[k:] + "\n</body>\n</html>\n")
open("commodix.html", "w").write(web)
json.dump(payload, open("commodix-data.json", "w"), ensure_ascii=False, separators=(",", ":"))
print("web", len(web) // 1024, "KB · data", len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) // 1024, "KB")
