"""
CommodiX – Regel-Optimierung je Wert (ohne SLV)
================================================
Ziel: Trefferquote der Signal-Trades >= 50 % bei positivem Ertrag.
Methode: Parametersuche nur auf dem Lernzeitraum (bis SPLIT), Bewertung auf dem ungesehenen Prüfzeitraum.
Daten:   research/hist/<T>.json (IBKR-Tageskerzen, 5 Jahre; NB ab 2023)
Signal:  Technik-Score der Engine (MACD, ADX/DMI, Trendflex, Reflex, Coppock, RSI) wie im Live-Backtest.
"""
import json, itertools, sys, pathlib
import numpy as np, pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "engine"))
import slv_signal_engine as E

SPLIT = "2025-04-01"
ASSETS = ["GLD", "GDX", "GDXJ", "SIL", "SILJ", "CPER", "URA", "USO", "UNG", "MP", "NB"]
TECH = ("MACD", "ADX/DMI", "Trendflex", "Reflex", "Coppock", "RSI")

GRID = dict(
    mode=["long", "both"],
    thr=[15, 25, 35, 45],
    filt=[0, 50, 100, 200],          # 0 = kein Trendfilter
    stop=[2.0, 3.0, 4.0],            # x ATR, nachgezogen
    target=[0, 1.5, 2.5, 4.0],       # x ATR ab Einstieg, 0 = kein Ziel
    exit=["opp", "zero"],            # Gegensignal oder Score kreuzt 0 gegen die Position
)
BASE = dict(mode="both", thr=25, filt=200, stop=2.0, target=0, exit="opp")   # bisherige Regel


def prepare(t):
    h = json.load(open(ROOT / "hist" / f"{t}.json"))
    d = pd.DataFrame({"open": h["o"], "high": h["h"], "low": h["l"], "close": h["c"]})
    c, hi, lo = d.close, d.high, d.low
    d["macd"], d["macd_sig"], d["macd_hist"] = E.macd(c)
    d["atr"] = E.atr(hi, lo, c)
    d["adx"], d["pdi"], d["ndi"] = E.adx(hi, lo, c)
    d["trendflex"], d["reflex"] = E.trendflex_reflex(c, 20)
    d["coppock_d"] = E.coppock_daily(c)
    d["rsi"] = E.rsi(c)
    for L in (50, 100, 200):
        d[f"sma{L}"] = c.rolling(L).mean()
    w = {k: E.WEIGHTS[k] for k in TECH}; mx = sum(2 * v for v in w.values())
    sc = np.full(len(d), np.nan)
    for j in range(60, len(d)):
        s = E.score_technical(d, j); sc[j] = sum(s[k] * w[k] for k in s) / mx * 100
    d["score"] = sc
    return d, h["t"]


def simulate(d, dates, p, start=60):
    o, hi, lo, c, at, sc = (d[k].values for k in ("open", "high", "low", "close", "atr", "score"))
    sma = d[f"sma{p['filt']}"].values if p["filt"] else None
    allow = {1: True, -1: p["mode"] == "both"}
    pos, e, trades = 0, None, []

    def ok(j, w):
        if not allow[w]:
            return False
        if sma is None:
            return True
        if np.isnan(sma[j]):
            return w == 1
        return c[j] > sma[j] if w == 1 else c[j] < sma[j]

    for j in range(start, len(c)):
        s = sc[j]
        want = 1 if s >= p["thr"] else (-1 if s <= -p["thr"] else 0)
        blocked = 0
        if pos:
            xp, why = None, None
            if pos == 1:
                if lo[j] <= e["stop"]:
                    xp, why = min(e["stop"], o[j]), "Stop"
                elif e["tgt"] and hi[j] >= e["tgt"]:
                    xp, why = max(e["tgt"], o[j]), "Ziel"
            else:
                if hi[j] >= e["stop"]:
                    xp, why = max(e["stop"], o[j]), "Stop"
                elif e["tgt"] and lo[j] <= e["tgt"]:
                    xp, why = min(e["tgt"], o[j]), "Ziel"
            if xp is None:
                if want == -pos:
                    xp, why = c[j], "Gegensignal"
                elif p["exit"] == "zero" and s * pos < 0:
                    xp, why = c[j], "Score 0"
            if xp is not None:
                trades.append({"entry": dates[e["i"]], "exit": dates[j], "dir": pos,
                               "ret": (xp / e["px"] - 1) * pos, "days": j - e["i"], "why": why})
                blocked = pos if why in ("Stop", "Ziel") else 0
                pos = 0
        if pos == 0 and want and want != blocked and ok(j, want) and j < len(c) - 1:
            pos = want
            e = {"i": j, "px": c[j], "stop": c[j] - p["stop"] * at[j] * pos,
                 "tgt": (c[j] + p["target"] * at[j] * pos) if p["target"] else 0}
        elif pos:
            e["stop"] = max(e["stop"], c[j] - p["stop"] * at[j]) if pos == 1 else min(e["stop"], c[j] + p["stop"] * at[j])
    open_t = {"dir": pos, "entry": dates[e["i"]], "px": e["px"], "stop": e["stop"], "tgt": e["tgt"]} if pos else None
    return trades, open_t


def stats(tr):
    if not tr:
        return {"n": 0, "win": None, "avg": None, "tot": 0.0, "pf": None}
    r = np.array([t["ret"] for t in tr])
    g, l = r[r > 0].sum(), -r[r <= 0].sum()
    return {"n": len(r), "win": round(float((r > 0).mean() * 100), 1), "avg": round(float(r.mean() * 100), 2),
            "tot": round(float((np.prod(1 + r) - 1) * 100), 1), "pf": round(float(g / l), 2) if l > 0 else 99.0}


def split(tr):
    return [t for t in tr if t["entry"] < SPLIT], [t for t in tr if t["entry"] >= SPLIT]


def optimize(t):
    d, dates = prepare(t)
    keys = list(GRID)
    rows = []
    for vals in itertools.product(*GRID.values()):
        p = dict(zip(keys, vals))
        tr, _ = simulate(d, dates, p)
        a, b = split(tr)
        rows.append((p, stats(a), stats(b), stats(tr)))
    # Auswahl NUR auf dem Lernzeitraum: Trefferquote >= 55 %, >= 8 Trades, Ertrag > 0, Profit-Faktor > 1.1
    cand = [r for r in rows if r[1]["n"] >= 8 and r[1]["win"] >= 55 and r[1]["tot"] > 0 and r[1]["pf"] > 1.1]

    # Robustheit: Nachbarn (eine Stellschraube um einen Schritt verändert) sollen ebenfalls tragen
    def nb_score(p):
        vals = []
        for k in keys:
            opts = GRID[k]; i = opts.index(p[k])
            for jj in (i - 1, i + 1):
                if 0 <= jj < len(opts):
                    q = dict(p); q[k] = opts[jj]
                    r = next(x for x in rows if x[0] == q)[1]
                    vals.append((r["win"] or 0) if r["n"] >= 5 else 0)
        return float(np.mean(vals)) if vals else 0.0
    ranked = sorted(cand, key=lambda r: (min(r[1]["win"], 70) + 0.5 * nb_score(r[0]) + 5 * min(r[1]["pf"], 3)), reverse=True)
    base_tr, _ = simulate(d, dates, BASE)
    ba, bb = split(base_tr)
    best = ranked[0] if ranked else None
    return {"t": t, "base": (stats(ba), stats(bb), stats(base_tr)), "best": best, "n_cand": len(cand),
            "nb": nb_score(best[0]) if best else None, "d": d, "dates": dates}


if __name__ == "__main__":
    res = {}
    for t in ASSETS:
        r = optimize(t); res[t] = r
        b = r["base"]; x = r["best"]
        print(f"\n{t}: Kandidaten {r['n_cand']}")
        print(f"  bisher   Lern {b[0]}  Prüf {b[1]}")
        if x:
            print(f"  optimiert {x[0]}  Nachbarn Ø {r['nb']:.0f} %")
            print(f"            Lern {x[1]}  Prüf {x[2]}")
    out = {t: {"params": r["best"][0] if r["best"] else None,
               "learn": r["best"][1] if r["best"] else None, "test": r["best"][2] if r["best"] else None,
               "all": r["best"][3] if r["best"] else None,
               "base_learn": r["base"][0], "base_test": r["base"][1], "base_all": r["base"][2]} for t, r in res.items()}
    json.dump(out, open(ROOT / "optimize_result.json", "w"), indent=1, ensure_ascii=False)
