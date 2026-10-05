"""
SLV Signal-Engine (iShares Silver Trust)
=========================================
Berechnet ein Kaufen / Halten / Verkaufen-Signal aus
  Technik : MACD, ATR, ADX/DMI, Ehlers Trendflex & Reflex, Coppock, RSI
  Optionen: Put/Call-Volumenverhältnis, implizite Volatilität (IV, IV-Perzentil, IV/HV)
  Makro   : Fed-Leitzins-Richtung, 10J-Realzins (TIPS)
  Physisch: Shanghai-Prämie (SGE vs. Spot), Gold/Silber-Ratio (Niveau + Trend)

Eingabe : slv_daily.json (IBKR-Tageskerzen) + MARKET_CONTEXT unten
Ausgabe : signal.json (aktuelles Signal + Komponenten + Backtest)

Live-Handel: siehe ibkr_executor.py (standardmäßig DRY-RUN / Paper).
"""
import json, math
import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Marktkontext (Stand 02.10.2026 – bei jedem Lauf aktualisieren)
# --------------------------------------------------------------------------
MARKET_CONTEXT = {
    "asof": "2026-10-02",
    "fed_funds_range": "3,75–4,00 %",
    "fed_direction": "hike",        # hike / hold / cut  (16.09.2026: +25 bp)
    "us10y": 5.24,                  # nominal, H.15 01.10.2026
    "us10y_real": 2.88,             # TIPS 10J, H.15 01.10.2026
    "us10y_real_4w_change": +0.15,  # grob: Anstieg seit Mitte Sept.
    "silver_spot": 61.07,           # XAG/USD Schluss 02.10.
    "gold_spot": 4182.3,            # XAU/USD
    "sge_silver_usd": 69.18,        # Shanghai Ag(T+D) in USD/oz
    "sge_premium_pct": 15.1,        # inkl. ~13 % MwSt -> netto ≈ +2 %
    "sge_premium_percentile": 98,   # Perzentil seit 2024
    # Optionen (IBKR Snapshot SLV)
    "pc_volume_today": 81822 / 203463,
    "pc_volume_avg": 135694 / 262177,
    "iv_annual": 0.3247,
    "hv_30d_annual": 0.3907,
    "iv_percentile_52w": 0.044,
    "risk_free": 0.039,
    # SLV-Optionskette 15.01.2027 (IBKR Bid/Ask Schluss 02.10.2026, IV/Delta per Black-Scholes aus Mid)
    "option_chain": {
        "expiry": "2027-01-15", "dte_from_asof": 105,
        "puts":  [[60, 7.15, 7.30, 32633], [61, 7.95, 8.10, 1124], [61.5, 8.30, 8.45, 1577],
                  [62, 8.70, 8.85, 2993], [62.5, 9.10, 9.30, 1293], [63, 9.50, 9.70, 2206],
                  [63.5, 9.90, 10.10, 391]],
        "calls": [[51, 6.20, 6.35, 1990], [51.5, 5.90, 6.05, 465]],
    },
}

# --------------------------------------------------------------------------
# Options-Umsetzung: Signal -> Kauf Call (Long) / Kauf Put (Short)
# --------------------------------------------------------------------------
OPT = {
    "target_delta": 0.70,      # |Delta| bei Einstieg
    "target_dte": 100,         # Kalendertage Restlaufzeit bei Einstieg
    "roll_dte": 45,            # unter 45 Tagen in neue 100-Tage-Option rollen
    "notional_usd": 10_000,    # Delta-äquivalentes Exposure je Trade
    "max_premium_usd": 4_000,  # Obergrenze Prämie je Position
    "cost_pct": 1.0,           # halber Spread + Gebühren je Seite, in % der Prämie
    "put_skew_vol": 0.05,      # Put-IV-Aufschlag im Backtest (heute ~ +6 Vol-Punkte)
    # Ausstieg auf Basis der Optionsprämie (einstellbar, 0 oder None = aus).
    # Bezug: Gewinn/Verlust des Trades (inkl. Rollen) im Verhältnis zur ersten gezahlten Prämie.
    "take_profit_pct": 20,     # Gewinnmitnahme bei +20 %
    "stop_loss_pct": 40,       # Stop-Loss bei −40 %
}

from math import log as _ln, sqrt as _sq, exp as _ex, erf as _erf
_N = lambda x: 0.5 * (1 + _erf(x / _sq(2)))

def bs(S, K, T, r, sig, cp):
    """Black-Scholes (europäisch) -> (Preis, Delta)."""
    T = max(T, 1e-4)
    d1 = (_ln(S / K) + (r + sig * sig / 2) * T) / (sig * _sq(T)); d2 = d1 - sig * _sq(T)
    if cp == "C":
        return S * _N(d1) - K * _ex(-r * T) * _N(d2), _N(d1)
    return K * _ex(-r * T) * _N(-d2) - S * _N(-d1), _N(d1) - 1

def implied_vol(S, K, T, r, price, cp):
    lo, hi = 0.01, 3.0
    for _ in range(80):
        m = (lo + hi) / 2
        if bs(S, K, T, r, m, cp)[0] > price: hi = m
        else: lo = m
    return m

def strike_for_delta(S, T, r, sig, cp, target):
    """Strike, bei dem |Delta| = target (Bisektion)."""
    lo, hi = S * 0.5, S * 1.5
    for _ in range(80):
        K = (lo + hi) / 2
        dl = abs(bs(S, K, T, r, sig, cp)[1])
        # Call: höherer Strike -> kleineres Delta; Put: höherer Strike -> größeres |Delta|
        if (dl > target) == (cp == "C"): lo = K
        else: hi = K
    return round((lo + hi) / 2 * 2) / 2   # auf 0,50 runden

def pick_live_option(ctx, cp, S):
    """Wählt aus der IBKR-Kette die Option mit |Delta| am nächsten an 0,70."""
    ch = ctx["option_chain"]; T = ch["dte_from_asof"] / 365; r = ctx["risk_free"]
    rows = []
    for K, b, a, oi in ch["puts" if cp == "P" else "calls"]:
        mid = (a + b) / 2; iv = implied_vol(S, K, T, r, mid, cp); _, dl = bs(S, K, T, r, iv, cp)
        rows.append({"strike": K, "bid": b, "ask": a, "mid": round(mid, 3), "oi": oi,
                     "iv": round(iv * 100, 1), "delta": round(dl, 3),
                     "spread_pct": round((a - b) / mid * 100, 1)})
    liquid = [x for x in rows if x["oi"] >= 1000] or rows
    best = min(liquid, key=lambda x: abs(abs(x["delta"]) - OPT["target_delta"]))
    return best, rows

# --------------------------------------------------------------------------
# Indikatoren
# --------------------------------------------------------------------------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def wilder(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()

def wma(s, n):
    w = np.arange(1, n + 1)
    return s.rolling(n).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)

def rsi(c, n=14):
    d = c.diff()
    up, dn = wilder(d.clip(lower=0), n), wilder(-d.clip(upper=0), n)
    return 100 - 100 / (1 + up / dn)

def macd(c, f=12, s=26, sig=9):
    m = ema(c, f) - ema(c, s)
    sg = ema(m, sig)
    return m, sg, m - sg

def atr(h, l, c, n=14):
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return wilder(tr, n)

def adx(h, l, c, n=14):
    upm, dnm = h.diff(), -l.diff()
    pdm = np.where((upm > dnm) & (upm > 0), upm, 0.0)
    ndm = np.where((dnm > upm) & (dnm > 0), dnm, 0.0)
    a = atr(h, l, c, n)
    pdi = 100 * wilder(pd.Series(pdm, index=c.index), n) / a
    ndi = 100 * wilder(pd.Series(ndm, index=c.index), n) / a
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi)
    return wilder(dx, n), pdi, ndi

def _supersmoother(c, period):
    a1 = math.exp(-1.414 * math.pi / period)
    b1 = 2 * a1 * math.cos(1.414 * math.pi / period)
    c2, c3 = b1, -a1 * a1
    c1 = 1 - c2 - c3
    x = c.values
    f = np.zeros(len(x))
    for i in range(len(x)):
        if i < 2:
            f[i] = x[i]
        else:
            f[i] = c1 * (x[i] + x[i - 1]) / 2 + c2 * f[i - 1] + c3 * f[i - 2]
    return f

def trendflex_reflex(c, L=20):
    """John Ehlers, TASC Feb 2020 – beide Oszillatoren normiert (≈ ±2)."""
    f = _supersmoother(c, 0.5 * L)
    n = len(f)
    tf, rf = np.full(n, np.nan), np.full(n, np.nan)
    ms_t = ms_r = 0.0
    for i in range(L, n):
        slope = (f[i - L] - f[i]) / L
        s_t = sum(f[i] - f[i - k] for k in range(1, L + 1)) / L
        s_r = sum((f[i] + k * slope) - f[i - k] for k in range(1, L + 1)) / L
        ms_t = 0.04 * s_t * s_t + 0.96 * ms_t
        ms_r = 0.04 * s_r * s_r + 0.96 * ms_r
        tf[i] = s_t / math.sqrt(ms_t) if ms_t else 0
        rf[i] = s_r / math.sqrt(ms_r) if ms_r else 0
    return pd.Series(tf, index=c.index), pd.Series(rf, index=c.index)

def coppock_daily(c, wma_n=10, roc_long=14, roc_short=11):
    """TradingView-Standard auf Tageskerzen (kurzfristig)."""
    r = 100 * (c / c.shift(roc_long) - 1) + 100 * (c / c.shift(roc_short) - 1)
    return wma(r, wma_n)

def coppock_monthly_equiv(c, m=21):
    """Klassischer Monats-Coppock aus Tagesdaten (1 Monat ≈ 21 Handelstage)."""
    r = 100 * (c / c.shift(14 * m) - 1) + 100 * (c / c.shift(11 * m) - 1)
    out = pd.Series(np.nan, index=c.index)
    w = np.arange(1, 11)
    for i in range(len(c)):
        idx = [i - k * m for k in range(9, -1, -1)]
        if idx[0] < 0:
            continue
        vals = r.iloc[idx].values
        if np.isnan(vals).any():
            continue
        out.iloc[i] = np.dot(vals, w) / w.sum()
    return out

# --------------------------------------------------------------------------
# Scoring: jede Komponente -2 … +2
# --------------------------------------------------------------------------
def score_technical(d, i):
    r = d.iloc[i]; p = d.iloc[i - 1]
    s = {}
    # MACD: Lage + Histogramm-Richtung
    v = (1 if r.macd > r.macd_sig else -1) + (1 if r.macd_hist > p.macd_hist else -1) * 0.5
    v += 0.5 if r.macd > 0 else -0.5
    s["MACD"] = float(np.clip(v, -2, 2))
    # ADX/DMI: Trendrichtung, gewichtet mit Trendstärke
    dirn = 1 if r.pdi > r.ndi else -1
    strength = 2 if r.adx >= 25 else (1 if r.adx >= 18 else 0.5)
    s["ADX/DMI"] = float(np.clip(dirn * strength, -2, 2))
    # Trendflex (Trend) und Reflex (Zyklus/Wendepunkt)
    s["Trendflex"] = float(np.clip(r.trendflex, -2, 2))
    rv = r.reflex
    cross_up = p.reflex < 0 <= rv
    cross_dn = p.reflex > 0 >= rv
    s["Reflex"] = 1.5 if cross_up else (-1.5 if cross_dn else float(np.clip(rv * 0.75, -1.5, 1.5)))
    # Coppock (daily): Richtung + Vorzeichen; Kaufsignal klassisch: dreht unter 0 nach oben
    cp, cpp = r.coppock_d, p.coppock_d
    v = (1 if cp > cpp else -1) + (0.5 if cp > 0 else -0.5)
    if cp < 0 and cp > cpp:
        v = 1.5
    s["Coppock"] = float(np.clip(v, -2, 2))
    # RSI: Momentum + Extremzonen (konträr)
    x = r.rsi
    if x < 30: v = 1.5
    elif x > 75: v = -1.5
    elif x >= 55: v = 1
    elif x <= 45: v = -1
    else: v = 0
    s["RSI"] = float(v)
    return s

def score_context(ctx, gsr_trend_10w):
    s = {}
    # Zinsen: Realzins > 2 % und steigend + Fed hebt an = Gegenwind
    v = 0
    v += -1 if ctx["fed_direction"] == "hike" else (1 if ctx["fed_direction"] == "cut" else 0)
    v += -1 if ctx["us10y_real"] > 2.0 else (1 if ctx["us10y_real"] < 1.0 else 0)
    v += -0.5 if ctx["us10y_real_4w_change"] > 0.1 else (0.5 if ctx["us10y_real_4w_change"] < -0.1 else 0)
    s["Zinsen/Fed"] = float(np.clip(v, -2, 2))
    # Put/Call (Volumen) – konträr: niedriges P/C = Sorglosigkeit
    pc, pca = ctx["pc_volume_today"], ctx["pc_volume_avg"]
    if pc > 1.0: v = 1.5
    elif pc > pca * 1.3: v = 1
    elif pc < pca * 0.8: v = -0.5
    else: v = 0
    s["Put/Call"] = float(v)
    # Implizite Volatilität: Panik-IV in fallendem Markt = Kapitulation (bullisch),
    # sehr niedrige IV nach Abverkauf = keine Kapitulation (leicht bärisch)
    ivp, ivhv = ctx["iv_percentile_52w"], ctx["iv_annual"] / ctx["hv_30d_annual"]
    if ivp > 0.8 and ivhv > 1.1: v = 1.5
    elif ivp < 0.15 and ivhv < 0.9: v = -0.5
    else: v = 0
    s["Implizite Vola"] = float(v)
    # Shanghai-Prämie (über 13 % MwSt-Parität = echte physische Knappheit)
    net = ctx["sge_premium_pct"] - 13
    v = 1.5 if net > 3 else (1 if net > 1 else (0 if net > -1 else -1))
    if ctx["sge_premium_percentile"] >= 90: v = min(2, v + 0.5)
    s["Shanghai SGE"] = float(v)
    # Gold/Silber-Ratio: Niveau (hoch = Silber günstig) + Trend (steigend = Silber schwach)
    gsr = ctx["gold_spot"] / ctx["silver_spot"]
    lvl = 1 if gsr > 80 else (0.5 if gsr > 70 else (-0.5 if gsr < 55 else 0))
    tr = -1 if gsr_trend_10w > 3 else (1 if gsr_trend_10w < -3 else 0)
    s["Gold/Silber-Ratio"] = float(np.clip(lvl + tr, -2, 2))
    return s, gsr

WEIGHTS = {"MACD": 1.0, "ADX/DMI": 1.0, "Trendflex": 1.25, "Reflex": 0.75, "Coppock": 0.75,
           "RSI": 0.75, "Zinsen/Fed": 1.25, "Put/Call": 0.5, "Implizite Vola": 0.5,
           "Shanghai SGE": 0.75, "Gold/Silber-Ratio": 0.75}

# --------------------------------------------------------------------------
# Handelsregeln je Wert (research/optimize.py, Lernzeitraum 10/2021–03/2025, geprüft 04/2025–10/2026)
#   mode: long | both · thr: Score-Schwelle in % · filt: Trendfilter SMA-Länge (0 = keiner)
#   stop: nachgezogener Stop in ATR · target: Kursziel in ATR ab Einstieg (0 = keins)
#   exit: "opp" = Ausstieg beim Gegensignal, "zero" = schon wenn der Score gegen die Position dreht
#   trade: False = nur beobachten (keine Regel hat Trefferquote >= 50 % UND positiven Ertrag erreicht)
# --------------------------------------------------------------------------
LEGACY_RULE = dict(mode="both", thr=25, filt=200, stop=2.0, target=0, exit="opp", trade=True)
COMMON_RULE = dict(mode="long", thr=45, filt=100, stop=3.0, target=1.5, exit="opp", trade=True)
RULES = {
    "SLV": dict(LEGACY_RULE),                       # unverändert (bereits abgestimmt)
    "GLD": dict(COMMON_RULE), "GDX": dict(COMMON_RULE), "GDXJ": dict(COMMON_RULE),
    "SIL": dict(COMMON_RULE), "SILJ": dict(COMMON_RULE), "CPER": dict(COMMON_RULE), "USO": dict(COMMON_RULE),
    "MP":  dict(mode="long", thr=45, filt=200, stop=4.0, target=1.5, exit="opp", trade=True),
    "URA": dict(mode="both", thr=35, filt=50, stop=3.0, target=1.5, exit="zero", trade=True),
    "UNG": dict(COMMON_RULE, trade=False),
    "NB":  dict(COMMON_RULE, trade=False),
}
RULE_SET = "optimized"            # "legacy" = bisherige Regeln für alle Werte (Webseite/Telegram)
LEGACY_START = "2025-07-25"       # Webseite: Historie wie bisher ab diesem Tag (wächst täglich um eine Kerze), außer SLV


def rule_for(ticker):
    if RULE_SET == "legacy":
        return dict(LEGACY_RULE)
    return dict(RULES.get(ticker, LEGACY_RULE))


def rule_text(r):
    if not r.get("trade", True):
        return "Nur beobachten – keine geprüfte Regel mit Trefferquote ≥ 50 % und positivem Ertrag"
    parts = ["Long & Short" if r["mode"] == "both" else "nur Long", f"Schwelle ±{r['thr']:g} %",
             f"Trendfilter SMA {r['filt']}" if r["filt"] else "ohne Trendfilter",
             f"Stop {r['stop']:g}×ATR", f"Ziel {r['target']:g}×ATR" if r["target"] else "ohne festes Ziel",
             "Ausstieg bei Score-Wende" if r["exit"] == "zero" else "Ausstieg beim Gegensignal"]
    return " · ".join(parts)


def decide(total, max_total, rule=None):
    rule = rule or LEGACY_RULE
    pct = total / max_total * 100
    if not rule.get("trade", True):
        return "BEOBACHTEN · KEIN HANDEL", pct
    if pct >= rule["thr"]: return "KAUFEN · LONG", pct
    if pct <= -rule["thr"]: return ("VERKAUFEN · SHORT" if rule["mode"] == "both" else "VERKAUFEN · FLAT"), pct
    return "NEUTRAL · FLAT", pct

# --------------------------------------------------------------------------
def score_context_profile(MC, profile, d, extra):
    """Umfeld-Bausteine je Profil. Liefert (scores, gsr, gsr_series, texte)."""
    s, txt, gsr, gsr_series = {}, {}, None, None
    pc, pca = MC["pc_volume_today"], MC["pc_volume_avg"]
    ivp, ivhv = MC["iv_percentile_52w"], MC["iv_annual"] / MC["hv_30d_annual"]
    if "rates" in profile:
        v = 0
        v += -1 if MC["fed_direction"] == "hike" else (1 if MC["fed_direction"] == "cut" else 0)
        v += -1 if MC["us10y_real"] > 2.0 else (1 if MC["us10y_real"] < 1.0 else 0)
        v += -0.5 if MC["us10y_real_4w_change"] > 0.1 else (0.5 if MC["us10y_real_4w_change"] < -0.1 else 0)
        s["Zinsen/Fed"] = float(np.clip(v, -2, 2))
        txt["Zinsen/Fed"] = f"Fed hebt an ({MC['fed_funds_range']}), 10J {MC['us10y']} %, Realzins {MC['us10y_real']} % steigend"
    v = 1.5 if pc > 1.0 else (1 if pc > pca * 1.3 else (-0.5 if pc < pca * 0.8 else 0))
    s["Put/Call"] = float(v)
    txt["Put/Call"] = f"P/C-Volumen {pc:.2f} vs. Ø {pca:.2f}" + (": viel Absicherung, konträr positiv" if v > 0 else (": wenig Absicherung, konträr leicht negativ" if v < 0 else ": unauffällig"))
    v = 1.5 if (ivp > 0.8 and ivhv > 1.1) else (-0.5 if (ivp < 0.15 and ivhv < 0.9) else 0)
    s["Implizite Vola"] = float(v)
    txt["Implizite Vola"] = f"IV {MC['iv_annual']*100:.1f} % vs. HV {MC['hv_30d_annual']*100:.1f} %, IV-Perzentil {ivp*100:.0f} %" + (": Panik-IV, Kapitulation möglich" if v > 0 else (": keine Kapitulation" if v < 0 else ""))
    if "sge" in profile:
        net = MC["sge_premium_pct"] - 13
        v = 1.5 if net > 3 else (1 if net > 1 else (0 if net > -1 else -1))
        if MC["sge_premium_percentile"] >= 90: v = min(2, v + 0.5)
        s["Shanghai SGE"] = float(v)
        txt["Shanghai SGE"] = f"SGE ${MC['sge_silver_usd']} = +{MC['sge_premium_pct']} % (≈ +{net:.0f} % über MwSt-Parität, {MC['sge_premium_percentile']}. Perzentil)"
    if "gsr" in profile:
        gld = pd.Series(extra["gld_weekly_close"]); c = d.close
        slv_w = c.iloc[::-5][::-1].reset_index(drop=True).iloc[-len(gld):].reset_index(drop=True)
        ratio = gld.values / slv_w.values
        gsr = MC["gold_spot"] / MC["silver_spot"]
        gsr_series = ratio / ratio[-1] * gsr
        tr10 = gsr_series[-1] - gsr_series[-11]
        lvl = 1 if gsr > 80 else (0.5 if gsr > 70 else (-0.5 if gsr < 55 else 0))
        tr = -1 if tr10 > 3 else (1 if tr10 < -3 else 0)
        s["Gold/Silber-Ratio"] = float(np.clip(lvl + tr, -2, 2))
        txt["Gold/Silber-Ratio"] = f"GSR {gsr:.1f} (Mittelfeld), 10-Wochen-Änderung {tr10:+.1f}"
        extra["gsr_trend_10w"] = tr10
    if "metal" in profile:
        tf = float(extra["metal_trendflex"])
        s["Metall-Trend"] = float(np.clip(tf, -2, 2))
        txt["Metall-Trend"] = f"Trendflex des Metalls ({extra['metal']}) {tf:+.2f}: " + ("Rückenwind" if tf > 0.3 else ("Gegenwind" if tf < -0.3 else "neutral"))
    return s, gsr, gsr_series, txt

PROFILES = {
    "SLV":  ["rates", "sge", "gsr"],
    "GLD":  ["rates"],
    "GDX":  ["rates", "metal"], "GDXJ": ["rates", "metal"], "SIL": ["rates", "metal"], "SILJ": ["rates", "metal"],
    "CPER": [], "URA": [], "USO": [], "UNG": [], "NB": [], "MP": [],
}
WEIGHTS["Metall-Trend"] = 1.0

def tech_texts(d, comp):
    r = d.iloc[-1]
    return {
        "MACD": f"MACD {r.macd:+.2f} {'über' if r.macd > r.macd_sig else 'unter'} Signallinie {r.macd_sig:+.2f}, Histogramm {r.macd_hist:+.2f}",
        "ADX/DMI": f"ADX {r.adx:.1f} ({'starker' if r.adx >= 25 else ('mittlerer' if r.adx >= 18 else 'schwacher')} Trend), {'+DI' if r.pdi > r.ndi else '−DI'} führt ({r.pdi:.1f} / {r.ndi:.1f})",
        "Trendflex": f"Trendflex(20) {r.trendflex:+.2f}: " + ("Aufwärtstrend" if r.trendflex > 0.3 else ("Abwärtstrend" if r.trendflex < -0.3 else "kein klarer Trend")),
        "Reflex": f"Reflex(20) {r.reflex:+.2f}: Zyklus zeigt " + ("nach oben" if r.reflex > 0 else "nach unten"),
        "Coppock": f"Coppock (Tag) {r.coppock_d:+.1f}, " + ("steigt" if r.coppock_d > d.coppock_d.iloc[-2] else "fällt"),
        "RSI": f"RSI(14) {r.rsi:.1f}" + (": überverkauft" if r.rsi < 30 else (": überkauft" if r.rsi > 75 else (": stark" if r.rsi >= 55 else (": schwach" if r.rsi <= 45 else ": neutral")))),
    }

def analyze(ticker, raw, opens, dates, MC, profile, extra=None):
    extra = extra or {}
    rule = rule_for(ticker)
    THR, SM, TG = rule["thr"], rule["stop"], rule["target"]
    d = pd.DataFrame({"close": raw["close"], "high": raw["high"], "low": raw["low"]})
    c, h, l = d.close, d.high, d.low
    d["macd"], d["macd_sig"], d["macd_hist"] = macd(c)
    d["atr"] = atr(h, l, c)
    d["adx"], d["pdi"], d["ndi"] = adx(h, l, c)
    d["trendflex"], d["reflex"] = trendflex_reflex(c, 20)
    d["coppock_d"] = coppock_daily(c)
    d["coppock_m"] = coppock_monthly_equiv(c)
    d["rsi"] = rsi(c)
    d["sma50"], d["sma200"] = c.rolling(50).mean(), c.rolling(200).mean()
    d["sma100"] = c.rolling(100).mean()
    FILT = d[f"sma{rule['filt']}"] if rule["filt"] else None

    i = len(d) - 1
    tech = score_technical(d, i)
    ctxs, gsr, gsr_series, ctx_txt = score_context_profile(MC, profile, d, extra)
    comp = {**tech, **ctxs}
    total = sum(comp[k] * WEIGHTS[k] for k in comp)
    max_total = sum(2 * WEIGHTS[k] for k in comp)
    signal, pct = decide(total, max_total, rule)
    comp_txt = {**tech_texts(d, comp), **ctx_txt}

    r = d.iloc[i]
    a = float(r.atr)
    cl = float(r.close)
    levels = {
        "last": cl, "atr14": round(a, 2),
        # Long-Seite
        "long_entry_trigger": round(float(d.high.iloc[-10:].max()) + 0.25 * a, 2),
        "long_stop": round(cl - SM * a, 2), "long_target": round(cl + (TG or 3) * a, 2),
        # Short-Seite
        "short_entry_trigger": round(float(d.low.iloc[-10:].min()) - 0.25 * a, 2),
        "short_stop": round(cl + SM * a, 2), "short_target": round(cl - (TG or 3) * a, 2),
        "short_exit_trigger": round(float(d.low.iloc[-10:].min()) - 0.25 * a, 2),
        "sma50": round(float(r.sma50), 2), "sma200": round(float(r.sma200), 2),
        "filter_len": rule["filt"], "filter_sma": (round(float(FILT.iloc[-1]), 2) if FILT is not None and not np.isnan(FILT.iloc[-1]) else None),
    }

    # ---------------- Backtest (nur technische Komponenten – Makro historisch nicht verfügbar)
    tech_w = {k: WEIGHTS[k] for k in tech}
    tech_max = sum(2 * w for w in tech_w.values())
    assert len(dates) == len(d) == len(opens)
    start = 60
    tps = {}
    for j in range(start, len(d)):
        ts = score_technical(d, j)
        tps[j] = sum(ts[k] * tech_w[k] for k in ts) / tech_max * 100

    def regime_ok(j, direction, filt):
        """Trendfilter der Regel: Long nur über SMA(filt), Short nur darunter (filt=True)."""
        if not filt or FILT is None:
            return True
        sm = FILT.iloc[j]
        if np.isnan(sm):
            return direction == 1          # ohne SMA200-Historie keine Shorts
        return d.close.iloc[j] > sm if direction == 1 else d.close.iloc[j] < sm

    # Umkehrpunkt (Variante C): MACD kreuzt Signallinie + RSI dreht aus <40 (Long) bzw. >60 (Short),
    # beide innerhalb von 3 Handelstagen.
    REV_WIN = 3
    _mu = (d.macd > d.macd_sig) & (d.macd.shift() <= d.macd_sig.shift())
    _md = (d.macd < d.macd_sig) & (d.macd.shift() >= d.macd_sig.shift())
    _ru = (d.rsi > d.rsi.shift()) & (d.rsi.shift() <= d.rsi.shift(2)) & (d.rsi.rolling(5).min() < 40)
    _rd = (d.rsi < d.rsi.shift()) & (d.rsi.shift() >= d.rsi.shift(2)) & (d.rsi.rolling(5).max() > 60)
    _w = lambda x: x.rolling(REV_WIN).max().fillna(0).astype(bool)
    rev_up, rev_dn = (_w(_mu) & _w(_ru)).values, (_w(_md) & _w(_rd)).values

    def run(mode, filt=False, rev=False):
        """mode: long | short | both. Einstieg zum Schluss bei Score-Schwelle,
        Ausstieg bei Gegensignal (Schluss) oder 2xATR-Trailing-Stop (Stopkurs, bei Gap Eröffnung)."""
        allow = {1: mode in ("long", "both") and rule.get("trade", True),
                 -1: (mode == "short" or (mode == "both" and rule["mode"] == "both")) and rule.get("trade", True)}
        pos, eq, bh, log, entry, ev = 0, [1.0], [1.0], [], None, []
        for j in range(start, len(d) - 1):
            tp = tps[j]
            px, nx, at = d.close.iloc[j], d.close.iloc[j + 1], d.atr.iloc[j]
            want = 1 if (tp >= THR or (rev and rev_up[j])) else (-1 if (tp <= -THR or (rev and rev_dn[j])) else 0)
            stopped_dir = 0
            if pos != 0:
                tg_hit = False
                if pos == 1:
                    hit = d.low.iloc[j] <= entry["stop"]; fill = min(entry["stop"], opens[j])
                    if not hit and entry["tgt"] and d.high.iloc[j] >= entry["tgt"]:
                        tg_hit, fill = True, max(entry["tgt"], opens[j])
                else:
                    hit = d.high.iloc[j] >= entry["stop"]; fill = max(entry["stop"], opens[j])
                    if not hit and entry["tgt"] and d.low.iloc[j] <= entry["tgt"]:
                        tg_hit, fill = True, min(entry["tgt"], opens[j])
                opp = want == -pos
                zero = rule["exit"] == "zero" and tp * pos < 0
                if hit or tg_hit or opp or zero:
                    xp = fill if (hit or tg_hit) else px
                    ret_t = (xp / entry["px"] - 1) * pos
                    eq[-1] *= 1 + pos * (xp - px) / px
                    log.append({"dir": "Long" if pos == 1 else "Short",
                                "entry_date": dates[entry["i"]], "entry_px": round(float(entry["px"]), 2),
                                "entry_score": round(float(entry["score"]), 0),
                                "exit_date": dates[j], "exit_px": round(float(xp), 2),
                                "reason": "ATR-Stop" if hit else ("Kursziel" if tg_hit else ("Gegensignal" if opp else "Score-Wende")),
                                "days": j - entry["i"], "ret_pct": round(ret_t * 100, 1)})
                    ev.append({"i": j - start, "t": "sell" if pos == 1 else "buy",
                               "k": "exit", "px": round(float(xp), 2)})
                    stopped_dir = pos if (hit or tg_hit) else 0
                    pos = 0
            if pos == 0 and want != 0 and allow[want] and want != stopped_dir and regime_ok(j, want, filt):
                pos = want
                entry = {"i": j, "px": px, "score": tp, "stop": px - SM * at * pos, "tgt": (px + TG * at * pos) if TG else 0}
                ev.append({"i": j - start, "t": "buy" if pos == 1 else "sell",
                           "k": "entry", "px": round(float(px), 2)})
            if pos == 1 and entry["i"] != j:
                entry["stop"] = max(entry["stop"], px - SM * at)
            elif pos == -1 and entry["i"] != j:
                entry["stop"] = min(entry["stop"], px + SM * at)
            ret = nx / px - 1
            eq.append(eq[-1] * (1 + ret * pos)); bh.append(bh[-1] * (1 + ret))
        # letzter Bar: Signal heute prüfen (Einstieg zum heutigen Schluss)
        j = len(d) - 1
        want = 1 if (tps[j] >= THR or (rev and rev_up[j])) else (-1 if (tps[j] <= -THR or (rev and rev_dn[j])) else 0)
        if pos == 0 and want != 0 and allow[want] and regime_ok(j, want, filt):
            pos, entry = want, {"i": j, "px": d.close.iloc[j], "score": tps[j],
                                "stop": d.close.iloc[j] - SM * d.atr.iloc[j] * want,
                                "tgt": (d.close.iloc[j] + TG * d.atr.iloc[j] * want) if TG else 0}
            ev.append({"i": j - start, "t": "buy" if pos == 1 else "sell", "k": "entry",
                       "px": round(float(d.close.iloc[j]), 2)})
        eq, bh = np.array(eq), np.array(bh)
        mdd = lambda x: float((x / np.maximum.accumulate(x) - 1).min())
        rets = [t["ret_pct"] for t in log]
        out = {
            "mode": mode + ("+filter" if filt else "") + ("+umkehr" if rev else ""),
            "strategy_return_pct": round((eq[-1] - 1) * 100, 1),
            "buyhold_return_pct": round((bh[-1] - 1) * 100, 1),
            "strategy_maxdd_pct": round(mdd(eq) * 100, 1),
            "buyhold_maxdd_pct": round(mdd(bh) * 100, 1),
            "trades": len(log),
            "long_trades": sum(t["dir"] == "Long" for t in log),
            "short_trades": sum(t["dir"] == "Short" for t in log),
            "win_rate_pct": round(100 * float(np.mean([x > 0 for x in rets])), 0) if rets else None,
            "avg_trade_pct": round(float(np.mean(rets)), 1) if rets else None,
            "trade_log": log, "events": ev,
            "open_trade": ({"dir": "Long" if pos == 1 else "Short", "entry_date": dates[entry["i"]],
                            "entry_px": round(float(entry["px"]), 2), "stop": round(float(entry["stop"]), 2),
                            "target": round(float(entry["tgt"] or (entry["px"] + 3 * d.atr.iloc[entry["i"]] * pos)), 2),
                            "ret_pct": round((d.close.iloc[-1] / entry["px"] - 1) * 100 * pos, 1)}
                           if pos != 0 else None),
            "equity": [round(float(x), 4) for x in eq],
            "buyhold": [round(float(x), 4) for x in bh],
        }
        return out

    runs = {m: run(m) for m in ("both", "long", "short")}
    runs.update({m + "_f": run(m, True) for m in ("both", "long", "short")})
    runs.update({m + "_fc": run(m, True, True) for m in ("both", "long")})
    bt = dict(runs["both_f"])          # Hauptmodus: bidirektional mit SMA200-Trendfilter
    bt["compare"] = {m: {k: runs[m][k] for k in ("strategy_return_pct", "strategy_maxdd_pct", "trades", "win_rate_pct")}
                     for m in runs}
    bt["equity_long"] = runs["long_f"]["equity"]
    bt["equity_both_raw"] = runs["both"]["equity"]
    bt["regime"] = "LONG erlaubt" if cl > float(r.sma200) else "SHORT erlaubt"
    bt["price"] = [round(float(x), 2) for x in c.iloc[start:]]
    bt["dates"] = dates[start:]
    bt["sma200"] = [None if np.isnan(x) else round(float(x), 2) for x in d.sma200.iloc[start:]]
    bt["period_bars"] = len(bt["equity"]) - 1

    # ---------------- Options-Backtest auf denselben Signalen (bidirektional + Filter)
    logret = np.log(c / c.shift())
    hv20 = (logret.rolling(20).std() * np.sqrt(252)).bfill()
    dts = pd.to_datetime(dates)
    rf = MC["risk_free"]
    idx = {dd: k for k, dd in enumerate(dates)}

    def vol_for(j, cp):
        v = max(float(hv20.iloc[j]), 0.20)
        return v + (OPT["put_skew_vol"] if cp == "P" else 0)

    def open_leg(j, cp, S):
        sig = vol_for(j, cp); T = OPT["target_dte"] / 365
        K = strike_for_delta(S, T, rf, sig, cp, OPT["target_delta"])
        prem, dl = bs(S, K, T, rf, sig, cp)
        n = OPT["notional_usd"] / (100 * S * abs(dl))
        n = min(n, OPT["max_premium_usd"] / (100 * prem))
        expiry = dts[j] + pd.Timedelta(days=OPT["target_dte"])
        return {"K": K, "exp": expiry, "n": n, "px_in": prem * (1 + OPT["cost_pct"] / 100), "dl": dl}

    def leg_value(leg, j, S, cp):
        T = (leg["exp"] - dts[j]).days / 365
        return bs(S, leg["K"], T, rf, vol_for(j, cp), cp)[0]

    def solve_S(target_v, leg, j, cp, s_a, s_b):
        """Basiswertkurs zwischen s_a und s_b, bei dem die Option target_v wert ist (Bisektion)."""
        fa = leg_value(leg, j, s_a, cp) - target_v
        for _ in range(50):
            m = (s_a + s_b) / 2; fm = leg_value(leg, j, m, cp) - target_v
            if (fm > 0) == (fa > 0): s_a, fa = m, fm
            else: s_b = m
        return (s_a + s_b) / 2

    def options_bt(run_res, tp=None, sl=None):
        """Optionen auf denselben Signalen. tp/sl in % der ersten Prämie (None/0 = aus).
        Prüfung je Tag: Eröffnung (Gap -> Füllung zum Eröffnungswert), dann ungünstiges Tagesextrem
        (Stop-Loss zuerst, konservativ), dann günstiges Extrem (Gewinnmitnahme) -> Füllung zur Schwelle.
        Nach Gewinnmitnahme/Stop-Loss bleibt die Option flat bis zum nächsten Signal-Trade."""
        tp = tp or None; sl = sl or None
        opt_trades, daily_opt, daily_stk = [], np.zeros(len(d)), np.zeros(len(d))
        all_trades = list(run_res["trade_log"])
        ot = run_res["open_trade"]
        if ot:
            all_trades.append({**ot, "exit_date": dates[-1], "exit_px": float(d.close.iloc[-1]),
                               "reason": "offen", "open": True})
        cost = OPT["cost_pct"] / 100
        for t in all_trades:
            cp = "C" if t["dir"] == "Long" else "P"
            a_, b_ = idx[t["entry_date"]], idx[t["exit_date"]]
            S0 = float(d.close.iloc[a_]); leg = open_leg(a_, cp, S0)
            first = dict(leg); pnl, rolls, prem_paid = 0.0, 0, leg["n"] * 100 * leg["px_in"]
            base = prem_paid
            prev_val = leg["px_in"]
            stk_n = OPT["notional_usd"] / S0; prev_S = S0
            done, reason, s_out, x_date, is_open = False, t["reason"], float(t["exit_px"]), t["exit_date"], bool(t.get("open"))
            for j in range(a_ + 1, b_ + 1):
                S = float(t["exit_px"]) if j == b_ else float(d.close.iloc[j])
                if not done and (tp or sl):
                    ratio = lambda v: (pnl + leg["n"] * 100 * (v - leg["px_in"])) / base
                    v_thr = lambda pct: leg["px_in"] + (base * pct / 100 - pnl) / (leg["n"] * 100)
                    So = float(opens[j])
                    worst = float(d.low.iloc[j]) if cp == "C" else float(d.high.iloc[j])
                    best_ = float(d.high.iloc[j]) if cp == "C" else float(d.low.iloc[j])
                    if j == b_ and t["reason"] == "ATR-Stop":      # Tag des Basiswert-Stops: nur bis zum Stopkurs
                        worst = float(t["exit_px"])
                    vo = leg_value(leg, j, So, cp)
                    hit = None
                    if sl and ratio(vo) <= -sl / 100: hit = ("SL", vo, So)
                    elif tp and ratio(vo) >= tp / 100: hit = ("TP", vo, So)
                    elif sl and ratio(leg_value(leg, j, worst, cp)) <= -sl / 100:
                        vt = v_thr(-sl); hit = ("SL", vt, solve_S(vt, leg, j, cp, So, worst))
                    elif tp and ratio(leg_value(leg, j, best_, cp)) >= tp / 100:
                        vt = v_thr(tp); hit = ("TP", vt, solve_S(vt, leg, j, cp, So, best_))
                    if hit:
                        kind, vfill, s_hit = hit
                        v_exit = vfill * (1 - cost)
                        daily_opt[j] += leg["n"] * 100 * (v_exit - prev_val); pnl += leg["n"] * 100 * (v_exit - leg["px_in"])
                        done, s_out, x_date, is_open = True, s_hit, dates[j], False
                        reason = f"Gewinnmitnahme +{tp:g} %" if kind == "TP" else f"Stop-Loss −{sl:g} %"
                if not done:
                    v = leg_value(leg, j, S, cp)
                    if j == b_:
                        v_exit = v * (1 - cost)
                        daily_opt[j] += leg["n"] * 100 * (v_exit - prev_val); pnl += leg["n"] * 100 * (v_exit - leg["px_in"])
                    else:
                        daily_opt[j] += leg["n"] * 100 * (v - prev_val); prev_val = v
                        if (leg["exp"] - dts[j]).days < OPT["roll_dte"]:     # Rollen
                            v_out = v * (1 - cost)
                            pnl += leg["n"] * 100 * (v_out - leg["px_in"])
                            daily_opt[j] += leg["n"] * 100 * (v_out - v)
                            leg = open_leg(j, cp, S); rolls += 1
                            prem_paid += leg["n"] * 100 * leg["px_in"]; prev_val = leg["px_in"]
                            daily_opt[j] -= leg["n"] * 100 * (leg["px_in"] - leg["px_in"] / (1 + cost))
                daily_stk[j] += stk_n * (S - prev_S) * (1 if cp == "C" else -1); prev_S = S
            stock_pnl = OPT["notional_usd"] * t["ret_pct"] / 100
            opt_trades.append({
                "dir": t["dir"], "type": "Call" if cp == "C" else "Put",
                "entry_date": t["entry_date"], "exit_date": x_date, "reason": reason,
                "underlying_in": round(S0, 2), "underlying_out": round(s_out, 2),
                "strike": first["K"], "expiry": first["exp"].strftime("%Y-%m-%d"),
                "contracts": round(first["n"], 1), "prem_in": round(first["px_in"], 2),
                "delta_in": round(first["dl"], 2), "rolls": rolls,
                "premium_usd": round(prem_paid, 0), "pnl_usd": round(pnl, 0),
                "ret_on_premium_pct": round(pnl / base * 100, 1) if base else None,
                "stock_pnl_usd": round(stock_pnl, 0), "open": is_open,
                "signal_open": bool(t.get("open")),
            })
        cum_opt = np.cumsum(daily_opt)[start:]; cum_stk = np.cumsum(daily_stk)[start:]
        closed = [x for x in opt_trades if not x["open"]]
        def mdd_usd(x, base=OPT["notional_usd"]):
            eqx = base + x; return float((eqx / np.maximum.accumulate(eqx) - 1).min())
        return {
            "settings": {**OPT, "take_profit_pct": tp, "stop_loss_pct": sl}, "trades": opt_trades,
            "total_pnl_usd": round(float(sum(x["pnl_usd"] for x in closed)), 0),
            "total_stock_pnl_usd": round(float(sum(x["stock_pnl_usd"] for x in opt_trades if not x["signal_open"])), 0),
            "win_rate_pct": round(100 * float(np.mean([x["pnl_usd"] > 0 for x in closed])), 0) if closed else None,
            "max_dd_pct_on_10k": round(mdd_usd(cum_opt) * 100, 1),
            "stock_max_dd_pct_on_10k": round(mdd_usd(cum_stk) * 100, 1),
            "cum_opt": [round(float(x), 0) for x in cum_opt],
            "cum_stk": [round(float(x), 0) for x in cum_stk],
        }

    def option_plan(mode, run_res, rev=False):
        sig_dir = 1 if (pct >= THR or (rev and rev_up[-1])) else (-1 if (pct <= -THR or (rev and rev_dn[-1])) else 0)
        fl = float(FILT.iloc[-1]) if FILT is not None and not np.isnan(FILT.iloc[-1]) else None
        up_ok = fl is None or cl > fl
        dn_ok = (FILT is None) or (fl is not None and cl < fl)
        short_ok = mode == "both" and rule["mode"] == "both"
        allowed = rule.get("trade", True) and ((sig_dir == 1 and up_ok) or (sig_dir == -1 and dn_ok and short_ok))
        flt = f"SMA-{rule['filt']}-Filter" if rule["filt"] else "Filter"
        plan = {"action": "Keine neue Optionsposition", "dir": None,
                "reason": ("Nur beobachten: Für diesen Wert hat keine geprüfte Regel eine Trefferquote ≥ 50 % mit positivem Ertrag erreicht."
                           if not rule.get("trade", True) else
                           ("Verkaufssignal – für diesen Wert wird nur long gehandelt: bestehende Calls schließen, keine Puts."
                            if sig_dir == -1 and rule["mode"] == "long" else
                            ("Short-Signal – im Modus „Nur Long“ wird nicht short gehandelt, Position bleibt flat."
                             if sig_dir == -1 and mode == "long" else
                             f"Signal unter der Schwelle ±{THR:g} % oder vom {flt} gesperrt.")))}
        if sig_dir != 0 and allowed:
            cp = "C" if sig_dir == 1 else "P"
            if "option_chain" in MC:
                best, rows = pick_live_option(MC, cp, cl); modelled = False
                exp, dte = MC["option_chain"]["expiry"], MC["option_chain"]["dte_from_asof"]
            else:
                # Modelliert aus ATM-IV (IBKR) – Kette am Handelstag live prüfen
                exp, dte = MC["model_expiry"], MC["model_dte"]; T0 = dte / 365
                ivm = MC["iv_annual"] + (OPT["put_skew_vol"] if cp == "P" else 0)
                inc = 0.5 if cl < 25 else (1 if cl < 200 else 5)
                K = strike_for_delta(cl, T0, MC["risk_free"], ivm, cp, OPT["target_delta"])
                K = round(K / inc) * inc
                prem, dl = bs(cl, K, T0, MC["risk_free"], ivm, cp)
                best = {"strike": K, "bid": None, "ask": None, "mid": round(prem, 2), "oi": None,
                        "iv": round(ivm * 100, 1), "delta": round(dl, 3), "spread_pct": None}
                rows, modelled = [], True
            n = max(1, round(OPT["notional_usd"] / (100 * cl * abs(best["delta"]))))
            while n > 1 and n * 100 * best["mid"] > OPT["max_premium_usd"]:
                n -= 1
            T = dte / 365; ivd = best["iv"] / 100
            stop_u = run_res["open_trade"]["stop"] if run_res.get("open_trade") else (levels["short_stop"] if cp == "P" else levels["long_stop"])
            tgt_u = levels["short_target"] if cp == "P" else levels["long_target"]
            v_stop = bs(stop_u, best["strike"], T - 10 / 365, MC["risk_free"], ivd, cp)[0]
            v_tgt = bs(tgt_u, best["strike"], T - 20 / 365, MC["risk_free"], ivd, cp)[0]
            plan = {
                "action": f"KAUF {n} × {ticker} {pd.Timestamp(exp).strftime('%d.%m.%Y')} {best['strike']:g} {'Call' if cp == 'C' else 'Put'}",
                "modelled": modelled, "thin_market": MC.get("thin_options", False),
                "dir": "Long" if cp == "C" else "Short", "type": "Call" if cp == "C" else "Put",
                "expiry": exp, "dte": dte,
                "strike": best["strike"], "contracts": n, "bid": best["bid"], "ask": best["ask"],
                "limit": round(best["mid"] + 0.02, 2), "iv": best["iv"], "delta": best["delta"],
                "oi": best["oi"], "premium_usd": round(n * 100 * best["mid"], 0),
                "delta_shares": round(n * 100 * best["delta"], 0),
                "underlying_stop": stop_u, "underlying_target": tgt_u,
                "opt_value_at_stop": round(v_stop, 2), "opt_value_at_target": round(v_tgt, 2),
                "roll_date": (pd.Timestamp(exp) - pd.Timedelta(days=OPT["roll_dte"])).strftime("%Y-%m-%d"),
                "candidates": rows,
            }
        return plan

    bt["modes"] = {}
    for key, rk, label in (("long", "long_f", "Nur Long"), ("both", "both_f", "Long & Short"),
                           ("long_c", "long_fc", "Nur Long · Score oder Umkehr"),
                           ("both_c", "both_fc", "Long & Short · Score oder Umkehr")):
        rr = runs[rk]
        bt["modes"][key] = {
            "label": label,
            **{k: rr[k] for k in ("strategy_return_pct", "strategy_maxdd_pct", "buyhold_return_pct",
                                  "buyhold_maxdd_pct", "trades", "long_trades", "short_trades",
                                  "win_rate_pct", "trade_log", "events", "open_trade", "equity")},
            "options": options_bt(rr, OPT["take_profit_pct"], OPT["stop_loss_pct"]),
            "options_plain": {k: v for k, v in options_bt(rr).items() if k in ("total_pnl_usd", "win_rate_pct", "max_dd_pct_on_10k")},
            "option_plan": option_plan(key.split("_")[0], rr, key.endswith("_c")),
        }
    bt["options"] = bt["modes"]["both"]["options"]
    bt["option_plan"] = bt["modes"]["both"]["option_plan"]
    bt["osim"] = {"o": [round(float(x), 4) for x in opens], "h": [round(float(x), 4) for x in d.high],
                  "l": [round(float(x), 4) for x in d.low], "c": [round(float(x), 4) for x in d.close],
                  "hv": [round(float(x), 5) for x in hv20], "dates_all": list(dates), "rf": rf, "start": start}
    bt["reversal_today"] = "UP" if rev_up[-1] else ("DN" if rev_dn[-1] else None)
    bt["reversal_last"] = [{"date": dates[k], "dir": "UP" if rev_up[k] else "DN", "px": round(float(c[k]), 2)}
                           for k in range(start, len(d)) if rev_up[k] or rev_dn[k]][-5:]

    hist_n = 130
    out = {
        "ticker": ticker, "name": MC.get("name", ticker), "group": MC.get("group", ""),
        "asof": MC["asof"],
        "component_texts": comp_txt,
        "signal": signal, "score_pct": round(pct, 1),
        "rule": rule, "rule_text": rule_text(rule), "rule_set": RULE_SET,
        "score_raw": round(total, 2), "score_max": round(max_total, 2),
        "components": {k: {"score": round(v, 2), "weight": WEIGHTS[k]} for k, v in comp.items()},
        "indicators": {
            "close": float(r.close), "rsi14": round(float(r.rsi), 1),
            "macd": round(float(r.macd), 3), "macd_signal": round(float(r.macd_sig), 3),
            "macd_hist": round(float(r.macd_hist), 3),
            "adx14": round(float(r.adx), 1), "plus_di": round(float(r.pdi), 1), "minus_di": round(float(r.ndi), 1),
            "atr14": round(a, 2), "atr_pct": round(a / float(r.close) * 100, 2),
            "trendflex20": round(float(r.trendflex), 2), "reflex20": round(float(r.reflex), 2),
            "coppock_daily": round(float(r.coppock_d), 2),
            "coppock_monthly_equiv": round(float(r.coppock_m), 1) if not np.isnan(r.coppock_m) else None,
            "coppock_monthly_prev": round(float(d.coppock_m.iloc[-22]), 1) if not np.isnan(d.coppock_m.iloc[-22]) else None,
            "gsr": round(gsr, 1) if gsr else None, "gsr_trend_10w": round(float(extra["gsr_trend_10w"]), 1) if gsr else None,
            "pc_today": round(MC["pc_volume_today"], 2), "pc_avg": round(MC["pc_volume_avg"], 2),
            "iv": round(MC["iv_annual"] * 100, 1), "hv30": round(MC["hv_30d_annual"] * 100, 1),
            "iv_pctl_52w": round(MC["iv_percentile_52w"] * 100, 0),
        },
        "levels": levels, "context": MC, "backtest": bt,
        "history": {
            "close": [round(float(x), 2) for x in c.iloc[-hist_n:]],
            "sma50": [round(float(x), 2) for x in d.sma50.iloc[-hist_n:]],
            "trendflex": [round(float(x), 2) for x in d.trendflex.iloc[-hist_n:]],
            "reflex": [round(float(x), 2) for x in d.reflex.iloc[-hist_n:]],
            "macd_hist": [round(float(x), 3) for x in d.macd_hist.iloc[-hist_n:]],
            "rsi": [round(float(x), 1) for x in d.rsi.iloc[-hist_n:]],
            "adx": [round(float(x), 1) for x in d.adx.iloc[-hist_n:]],
            "gsr": [round(float(x), 1) for x in gsr_series] if gsr_series is not None else [],
        },
    }
    return out


# --------------------------------------------------------------------------
# Mehrere Werte
# --------------------------------------------------------------------------
HOLIDAYS = pd.to_datetime(["2024-11-28", "2024-12-25", "2025-01-01", "2025-01-09", "2025-01-20",
    "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01",
    "2025-11-27", "2025-12-25", "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24"])


def monthly_expiry(asof, target=OPT["target_dte"], lo=80, hi=130):
    """Monatsverfall (3. Freitag) mit Restlaufzeit 80–130 Tage, am nächsten an 100 Tagen."""
    a = pd.Timestamp(asof); best = None
    for k in range(1, 8):
        m = (a + pd.offsets.MonthBegin(k)).normalize()
        fr = [d for d in pd.date_range(m, m + pd.offsets.MonthEnd(0)) if d.weekday() == 4][2]
        if fr.strftime("%Y-%m-%d") in [x.strftime("%Y-%m-%d") for x in HOLIDAYS]:
            fr = fr - pd.Timedelta(days=1)
        dte = (fr - a).days
        if lo <= dte <= hi and (best is None or abs(dte - target) < abs(best[1] - target)):
            best = (fr.strftime("%Y-%m-%d"), dte)
    return best or ("2027-01-15", max((pd.Timestamp("2027-01-15") - a).days, 1))

def trading_days(first, last):
    return [x.strftime("%Y-%m-%d") for x in pd.bdate_range(first, last) if x not in HOLIDAYS]

def macro_base():
    keys = ("asof", "fed_funds_range", "fed_direction", "us10y", "us10y_real", "us10y_real_4w_change", "risk_free")
    return {k: MARKET_CONTEXT[k] for k in keys}

def run_all():
    results = {}
    # SLV – volles Profil, 2 Jahre Historie, echte Optionskette
    raw = json.load(open("slv_daily.json")); opens = json.load(open("slv_open.json"))
    slv_dates = trading_days(raw["first"], raw["last"])
    mc = dict(MARKET_CONTEXT, name="iShares Silver Trust", group="Edelmetall")
    results["SLV"] = analyze("SLV", raw, opens, slv_dates, mc, PROFILES["SLV"], {"gld_weekly_close": raw["gld_weekly_close"]})
    meta = json.load(open("data/meta.json"))
    tf_cache = {"SLV": results["SLV"]["indicators"]["trendflex20"]}
    order = ["GLD", "GDX", "GDXJ", "SIL", "SILJ", "CPER", "URA", "USO", "UNG", "MP", "NB"]
    for t in order:
        dd = json.load(open(f"data/{t}.json")); m = meta[t]
        dates = dd["t"] if "t" in dd else slv_dates[-len(dd["c"]):]
        if RULE_SET == "legacy" and dates[0] < LEGACY_START:         # Webseite: Umfang wie bisher
            k0 = next(i for i, x in enumerate(dates) if x >= LEGACY_START)
            dd = {k: dd[k][k0:] for k in ("o", "h", "l", "c")}; dates = dates[k0:]
        mc = dict(macro_base(), name=m["name"], group=m["group"],
                  pc_volume_today=m["pc"][1] / m["pc"][0], pc_volume_avg=m["pc_avg"][1] / m["pc_avg"][0],
                  iv_annual=m["iv"], hv_30d_annual=m["hv"], iv_percentile_52w=m["ivp"],
                  model_expiry=monthly_expiry(MARKET_CONTEXT["asof"])[0], model_dte=monthly_expiry(MARKET_CONTEXT["asof"])[1],
                  thin_options=(m["pc_avg"][0] + m["pc_avg"][1]) < 5000)
        extra = {}
        if "metal" in PROFILES[t]:
            extra = {"metal": m["metal"], "metal_trendflex": tf_cache[m["metal"]]}
        raw_t = {"close": dd["c"], "high": dd["h"], "low": dd["l"]}
        results[t] = analyze(t, raw_t, dd["o"], dates, mc, PROFILES[t], extra)
        if t == "GLD":
            tf_cache["GLD"] = results[t]["indicators"]["trendflex20"]
    return results


def main():
    res = run_all()
    json.dump(res["SLV"], open("signal.json", "w"), indent=1, ensure_ascii=False)
    json.dump(res, open("signals_all.json", "w"), ensure_ascii=False)
    for t, o in res.items():
        m = o["backtest"]["modes"]
        print(f"{t:5s} {o['signal']:18s} {o['score_pct']:+6.1f}%  L {m['long']['strategy_return_pct']:+6.1f}%  "
              f"L&S {m['both']['strategy_return_pct']:+6.1f}%  B&H {m['both']['buyhold_return_pct']:+6.1f}%  "
              f"Opt L&S {m['both']['options']['total_pnl_usd']:+7.0f}$  | {m['both']['option_plan']['action']}")

if __name__ == "__main__":
    main()
