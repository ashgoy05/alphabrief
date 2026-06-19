"""
AlphaBrief feature logic.

These are pure-ish helpers that turn the data you already read from the
Daily Tracker (plus current prices) into the blocks the dashboard renders.
They don't replace your scoring/research in main.py -- they sit next to it
and add the "when to NOT buy / get out / how's the whole account doing" half.

Wire-in points are marked WIRE: in build_dashboard.py.
"""

from __future__ import annotations
import json, os, datetime as dt
from collections import defaultdict


# ---------------------------------------------------------------- portfolio
def compute_portfolio(positions, cash, goal):
    """positions: list of dicts with entry, price, shares (+ optional sector).
    Returns the portfolio block incl. concentration warnings."""
    invested = sum((p.get("price") or 0) * (p.get("shares") or 0) for p in positions)
    cost = sum((p.get("entry") or 0) * (p.get("shares") or 0) for p in positions)
    total_value = invested + (cash or 0)
    gain = invested - cost
    gain_pct = (gain / cost * 100) if cost else 0.0
    day_change = sum(
        (p.get("price") or 0) * (p.get("shares") or 0) * (p.get("change_pct") or 0) / 100
        for p in positions
    )
    day_pct = (day_change / invested * 100) if invested else 0.0

    return {
        "goal": goal,
        "total_value": round(total_value, 2),
        "cash": round(cash or 0, 2),
        "invested": round(invested, 2),
        "cost_basis": round(cost, 2),
        "total_gain": round(gain, 2),
        "total_gain_pct": round(gain_pct, 2),
        "day_change": round(day_change, 2),
        "day_change_pct": round(day_pct, 2),
        "concentration_warnings": concentration_warnings(positions, invested),
    }


def concentration_warnings(positions, invested, sector_warn=35, name_warn=25):
    """Flag if one sector or one name is too big a slice of what's invested.
    The single biggest danger of picking individual names in a retirement
    account is quietly ending up over-concentrated. This catches it."""
    if not invested:
        return []
    out = []

    by_sector = defaultdict(float)
    for p in positions:
        sec = p.get("sector") or "Unclassified"
        by_sector[sec] += (p.get("price") or 0) * (p.get("shares") or 0)
    for sec, val in sorted(by_sector.items(), key=lambda x: -x[1]):
        pct = round(val / invested * 100)
        if pct >= sector_warn and sec != "Unclassified":
            out.append({"label": sec, "pct": pct, "level": "warn"})

    biggest = max(
        positions,
        key=lambda p: (p.get("price") or 0) * (p.get("shares") or 0),
        default=None,
    )
    if biggest:
        val = (biggest.get("price") or 0) * (biggest.get("shares") or 0)
        pct = round(val / invested * 100)
        if pct >= name_warn:
            out.append({"label": f"Single name \u2014 {biggest['ticker']}",
                        "pct": pct, "level": "info"})
    return out


# ---------------------------------------------------------------- sell side
def sell_signals(positions, targets=None, run_up=25, stop=-15):
    """The counterpart to your dip-buy logic. Flag a held name to TRIM when it
    has run far past your entry or sits above analyst target, and to CUT when
    it has broken down past your stop. targets: {ticker: consensus_target}."""
    targets = targets or {}
    out = []
    for p in positions:
        tk = p["ticker"]
        entry, price = p.get("entry"), p.get("price")
        if not entry or not price:
            continue
        gain = (price - entry) / entry * 100
        tgt = targets.get(tk)

        if gain <= stop:
            out.append({
                "ticker": tk, "name": p.get("name", ""), "signal": "Cut",
                "price": price, "gain_pct": round(gain, 1),
                "reason": f"Down {abs(round(gain))}% past your entry \u2014 below your "
                          f"stop. Thesis isn't working; freeing the cash beats hoping.",
            })
        elif tgt and price >= tgt:
            out.append({
                "ticker": tk, "name": p.get("name", ""), "signal": "Trim",
                "price": price, "gain_pct": round(gain, 1),
                "reason": f"Trading above where analysts peg it (~${tgt:.0f}). "
                          f"The easy part of this move is behind it.",
            })
        elif gain >= run_up:
            out.append({
                "ticker": tk, "name": p.get("name", ""), "signal": "Trim",
                "price": price, "gain_pct": round(gain, 1),
                "reason": f"Up {round(gain)}% past your entry. Taking some off keeps "
                          f"the win and trims the single-name risk.",
            })
    return out


def position_flags(positions, targets=None, dip=-5, run_up=25):
    """Per-row badge for the holdings table: 'dip-buy' or 'trim'."""
    targets = targets or {}
    for p in positions:
        entry, price = p.get("entry"), p.get("price")
        p["flag"] = None
        if not entry or not price:
            continue
        gain = (price - entry) / entry * 100
        tgt = targets.get(p["ticker"])
        if gain >= run_up or (tgt and price >= tgt):
            p["flag"] = "trim"
        elif gain <= dip:
            p["flag"] = "dip-buy"
    return positions


# ---------------------------------------------------------------- earnings
def earnings_this_week(earnings_dates, owned_tickers, today=None, days=7):
    """earnings_dates: {ticker: (name, 'YYYY-MM-DD', 'before open'|'after close')}.
    Returns only those landing in the next `days`, owned ones first.
    Plug in any source you like (Finnhub/Nasdaq cal, or a column you keep)."""
    today = today or dt.date.today()
    horizon = today + dt.timedelta(days=days)
    out = []
    for tk, (name, date_str, when) in earnings_dates.items():
        try:
            d = dt.date.fromisoformat(date_str)
        except Exception:
            continue
        if today <= d <= horizon:
            out.append({"ticker": tk, "name": name, "date": date_str,
                        "when": when, "owned": tk in owned_tickers})
    out.sort(key=lambda e: (not e["owned"], e["date"]))
    return out


# ---------------------------------------------------------------- track record
def append_track_record(log_path, buys, prices_today, today=None):
    """Log today's buy calls so we can grade them later. Idempotent per day."""
    today = (today or dt.date.today()).isoformat()
    log = _load(log_path, [])
    have = {(r["date"], r["ticker"]) for r in log}
    for b in buys:
        key = (today, b["ticker"])
        if key in have:
            continue
        log.append({"date": today, "ticker": b["ticker"],
                    "price_at_call": prices_today.get(b["ticker"], b.get("price")),
                    "score": b.get("score")})
    _save(log_path, log)
    return log


def summarize_track_record(log_path, prices_now, min_age_days=5, today=None):
    """Grade calls that have had time to play out. A call 'worked' if the name
    is up since the call. Small samples mean little -- the note says so."""
    today = today or dt.date.today()
    log = _load(log_path, [])
    evaluated = worked = 0
    rets = []
    for r in log:
        try:
            age = (today - dt.date.fromisoformat(r["date"])).days
        except Exception:
            continue
        now = prices_now.get(r["ticker"])
        call = r.get("price_at_call")
        if age < min_age_days or not now or not call:
            continue
        evaluated += 1
        ret = (now - call) / call * 100
        rets.append(ret)
        if ret > 0:
            worked += 1

    hit = (worked / evaluated) if evaluated else None
    avg = round(sum(rets) / len(rets), 1) if rets else None
    if evaluated < 8:
        note = (f"{worked} of {evaluated} graded calls are in the green so far. "
                f"Too small a sample to trust \u2014 keep watching.")
    elif hit and hit >= 0.6:
        note = f"Holding up: {worked}/{evaluated} calls green, avg {avg:+}% per pick."
    else:
        note = (f"Mixed: {worked}/{evaluated} calls green. Worth a hard look at what "
                f"the high scores have in common before leaning on them.")
    return {"calls_made": len(log), "evaluated": evaluated, "worked": worked,
            "hit_rate": round(hit, 2) if hit is not None else None,
            "avg_return": avg, "note": note}


# ---------------------------------------------------------------- macro
def macro_block(snapshot):
    """snapshot: {'10-yr Treasury': (value, change, 'up'/'down'), ...}.
    Pull these from any quote source in main.py and pass them in."""
    items = [{"label": k, "value": v[0], "change": v[1], "direction": v[2]}
             for k, v in snapshot.items()]
    easing = sum(1 for v in snapshot.values() if v[2] == "down")
    if easing >= 2:
        note = ("Yields and oil easing \u2014 the friendly backdrop for growth and tech. "
                "Lower rates make future earnings worth more today, which is what your "
                "watchlist leans on.")
    else:
        note = ("Rates or oil ticking up \u2014 a small headwind for the long-duration "
                "growth names. Nothing alarming, just less of a tailwind than yesterday.")
    return {"items": items, "note": note}


# ---------------------------------------------------------------- io
def _load(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
