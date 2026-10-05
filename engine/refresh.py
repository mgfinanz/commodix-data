"""
CommodiX – Aktualisierung mit frischen IBKR-Daten
==================================================
Eingabe:  input/today.json  (von der Claude-Sitzung aus IBKR geholt, Format siehe unten)
Ablauf:   Tageskerze je Wert anhängen bzw. ersetzen (gleicher Tag) -> Optionsdaten/IV übernehmen
          -> slv_signal_engine.run_all() -> build_dashboard.py -> ../commodix-data.json
Aufruf:   cd engine && python3 refresh.py [input/today.json]

today.json
{
  "date": "2026-10-05",            # US-Handelstag der Kerze
  "time": "15:33",                 # Uhrzeit Berlin des Abrufs
  "final": false,                  # true = Tagesschluss (22:50-Lauf), false = Zwischenstand
  "bars":  {"SLV": {"o":55.6,"h":55.61,"l":55.43,"c":55.41}, ...alle 12...},
  "snap":  {"SLV": {"iv":0.3403,"hv":0.3907,"ivp":0.064,"pc":[calls,puts] oder null,"pc_avg":[calls,puts]}, ...},
  "slv_chain": {"expiry":"2027-01-15","puts":[[K,bid,ask,oi],...],"calls":[[K,bid,ask,oi],...]},   # optional
  "macro": {"us10y":5.24, "us10y_real":2.88, "us10y_real_4w_change":0.15, "fed_funds_range":"…",
            "fed_direction":"hike", "silver_spot":61.0, "gold_spot":4180, "sge_silver_usd":69.1,
            "sge_premium_pct":15.0, "sge_premium_percentile":98}                                     # optional, nur bekannte Werte
}
"pc": Put/Call-Volumen des laufenden Tages ist kurz nach Eröffnung zu dünn -> bei Zwischenständen null
übergeben, dann bleibt der letzte volle Tageswert stehen.
"""
import json, sys, subprocess, pathlib
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
ORDER = ["SLV", "GLD", "GDX", "GDXJ", "SIL", "SILJ", "CPER", "URA", "USO", "UNG", "MP", "NB"]


def load(p):
    return json.loads((HERE / p).read_text())


def save(p, obj):
    (HERE / p).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def main(inp="input/today.json"):
    import os; os.chdir(HERE)
    T = load(inp)
    day, final = T["date"], bool(T.get("final"))
    missing = [t for t in ORDER if t not in T["bars"]]
    if missing:
        sys.exit(f"Kerzen fehlen: {missing}")

    # ---- SLV (2 Jahre, close/high/low + eigene Open-Datei)
    raw, opens = load("slv_daily.json"), load("slv_open.json")
    b = T["bars"]["SLV"]
    replace = raw["last"] == day
    if replace:                           # Zwischenstand/Schluss desselben Tages ersetzen
        raw["close"][-1], raw["high"][-1], raw["low"][-1], opens[-1] = b["c"], b["h"], b["l"], b["o"]
    elif raw["last"] < day:
        raw["close"].append(b["c"]); raw["high"].append(b["h"]); raw["low"].append(b["l"]); opens.append(b["o"])
        raw["last"] = day
    else:
        sys.exit(f"Datum {day} liegt vor dem letzten SLV-Tag {raw['last']}")
    save("slv_daily.json", raw); save("slv_open.json", opens)

    # ---- übrige Werte (t/o/h/l/c, mehrjährige Historie mit eigener Datumsspalte)
    for t in ORDER[1:]:
        d, b = load(f"data/{t}.json"), T["bars"][t]
        same = (d["t"][-1] == day) if "t" in d else replace
        if same:
            d["o"][-1], d["h"][-1], d["l"][-1], d["c"][-1] = b["o"], b["h"], b["l"], b["c"]
        else:
            if "t" in d and d["t"][-1] > day:
                sys.exit(f"Datum {day} liegt vor dem letzten {t}-Tag {d['t'][-1]}")
            for k in "ohlc":
                d[k].append(b[k])
            if "t" in d:
                d["t"].append(day)
        save(f"data/{t}.json", d)

    # ---- Optionen / Volatilität
    meta = load("data/meta.json")
    import slv_signal_engine as E
    MC = E.MARKET_CONTEXT
    for t in ORDER:
        s = T.get("snap", {}).get(t)
        if not s:
            continue
        if t == "SLV":
            MC["iv_annual"], MC["hv_30d_annual"], MC["iv_percentile_52w"] = s["iv"], s["hv"], s["ivp"]
            if s.get("pc"):
                MC["pc_volume_today"] = s["pc"][1] / max(s["pc"][0], 1)
            if s.get("pc_avg"):
                MC["pc_volume_avg"] = s["pc_avg"][1] / max(s["pc_avg"][0], 1)
        else:
            m = meta[t]
            m["iv"], m["hv"], m["ivp"] = s["iv"], s["hv"], s["ivp"]
            if s.get("pc"):
                m["pc"] = s["pc"]
            if s.get("pc_avg"):
                m["pc_avg"] = s["pc_avg"]
    save("data/meta.json", meta)

    # ---- Persistenter SLV-Kontext (Kette, Makro) zwischen Läufen
    ctx_file = HERE / "data/slv_context.json"
    ctx = json.loads(ctx_file.read_text()) if ctx_file.exists() else {}
    for k in ("iv_annual", "hv_30d_annual", "iv_percentile_52w", "pc_volume_today", "pc_volume_avg"):
        ctx[k] = MC[k]
    if T.get("slv_chain"):
        ch = T["slv_chain"]
        ctx["option_chain"] = {"expiry": ch["expiry"], "puts": ch["puts"],
                               "calls": ch.get("calls") or MC["option_chain"]["calls"]}
    for k, v in (T.get("macro") or {}).items():
        if v is not None:
            ctx[k] = v
    ctx_file.write_text(json.dumps(ctx, ensure_ascii=False, indent=1))
    for k, v in ctx.items():
        if k == "option_chain":
            MC["option_chain"].update(v)
        else:
            MC[k] = v
    MC["asof"] = day
    MC["option_chain"]["dte_from_asof"] = (pd.Timestamp(MC["option_chain"]["expiry"]) - pd.Timestamp(day)).days

    # ---- rechnen + bauen (zweimal)
    #  1) bisherige Regeln  -> ../commodix-data.json (Webseite + Telegram, unverändert)
    #  2) optimierte Regeln -> slv-signal.html (Claude-Dashboard) + commodix-dashboard.json
    status = {"date": day, "time": T.get("time", ""), "final": final}
    (HERE / "status.json").write_text(json.dumps(status))
    outs = {}
    for rs in ("legacy", "optimized"):
        E.RULE_SET = rs
        res = E.run_all()
        json.dump(res, open(HERE / "signals_all.json", "w"), ensure_ascii=False)
        subprocess.run([sys.executable, "build_dashboard.py"], cwd=HERE, check=True)
        data = load("commodix-data.json")
        data["status"] = status
        for t in data["order"]:
            data["assets"][t]["asof"] = day
            data["assets"][t]["asof_time"] = T.get("time", "")
            data["assets"][t]["preliminary"] = not final
        target = HERE.parent / "commodix-data.json" if rs == "legacy" else HERE / "commodix-dashboard.json"
        target.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        (HERE / "commodix-data.json").unlink()
        outs[rs] = data
        if rs == "legacy":
            (HERE / "slv-signal.html").unlink(missing_ok=True)          # Dashboard kommt aus Lauf 2
    json.dump(res["SLV"], open(HERE / "signal.json", "w"), indent=1, ensure_ascii=False)

    w, o = outs["legacy"], outs["optimized"]
    print("Wert  Dashboard (optimiert)        Webseite (bisherig)")
    for t in o["order"]:
        a, b = o["assets"][t], w["assets"][t]
        print(f"{t:5s} {a['signal']:24s} {a['score_pct']:+6.1f}%   {b['signal']:18s}  Kurs {a['levels']['last']:.2f}")
    print("Status:", status, "-> Webseite: ../commodix-data.json · Dashboard: slv-signal.html")


if __name__ == "__main__":
    main(*sys.argv[1:])
