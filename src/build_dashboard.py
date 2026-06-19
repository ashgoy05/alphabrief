"""
build_dashboard.py  --  the new "output" stage.

Where your pipeline used to compose-and-send an email, it now calls
write_dashboard() to drop docs/data.json (what the website reads) and
append docs/track_record.json (the graded history). The GitHub Action
then commits docs/ and Pages serves it.

KEEP your existing main.py logic that:
  - reads the Daily Tracker tabs (Budget, Watchlist, Bought, Buying History, SIP)
  - runs your 100-pt scoring + web research
  - decides buys / SIP / dip-buys
Then hand the results to write_dashboard() at the WIRE: points below.
"""

from __future__ import annotations
import datetime as dt
from zoneinfo import ZoneInfo

import features as F

DOCS = "docs"
TRACK_LOG = f"{DOCS}/track_record.json"
CHICAGO = ZoneInfo("America/Chicago")


def write_dashboard(
    *,
    positions,            # WIRE: from your Bought tab + live prices.
                          #   each: {ticker,name,shares,entry,price,change_pct,sector?}
    cash,                 # WIRE: uninvested cash in the IRA
    goal,                 # WIRE: your target, e.g. 10000
    buys,                 # WIRE: your scored buy calls
                          #   each: {ticker,name,score,action,budget,price,change_pct,why}
    watchlist,            # WIRE: scored watchlist rows
                          #   each: {ticker,name,score,growth,quality,smart_money,gem,price,change_pct}
    targets=None,         # WIRE(optional): {ticker: analyst_target} -> sharper sell signals
    earnings_dates=None,  # WIRE(optional): {ticker:(name,'YYYY-MM-DD',when)}
    macro_snapshot=None,  # WIRE(optional): {'10-yr Treasury':(val,chg,'down'), ...}
    market_status="pre-open",
):
    today = dt.datetime.now(CHICAGO).date()
    owned = {p["ticker"] for p in positions}
    prices = {p["ticker"]: p.get("price") for p in positions}
    prices.update({b["ticker"]: b.get("price") for b in buys})

    F.position_flags(positions, targets)

    data = {
        "generated_at": dt.datetime.now(CHICAGO).isoformat(timespec="seconds"),
        "market_status": market_status,
        "portfolio": F.compute_portfolio(positions, cash, goal),
        "calls": {
            "buys": buys,
            "sells": F.sell_signals(positions, targets),
        },
        "positions": positions,
        "watchlist": watchlist,
        "earnings": F.earnings_this_week(earnings_dates or {}, owned, today),
        "macro": F.macro_block(macro_snapshot) if macro_snapshot else None,
        "track_record": _track(buys, prices, today),
    }

    F._save(f"{DOCS}/data.json", data)
    return data


def _track(buys, prices, today):
    F.append_track_record(TRACK_LOG, buys, prices, today)
    return F.summarize_track_record(TRACK_LOG, prices, today=today)


# ----------------------------------------------------------- demo / self-test
if __name__ == "__main__":
    # Runs with fake data so you can eyeball the JSON shape without the sheet.
    demo_positions = [
        {"ticker": "SNDK", "name": "SanDisk", "shares": 38, "entry": 48.30,
         "price": 61.80, "change_pct": 0.9, "sector": "Semiconductors"},
        {"ticker": "CARE", "name": "Carter's", "shares": 21, "entry": 42.10,
         "price": 39.20, "change_pct": -1.1, "sector": "Consumer"},
        {"ticker": "PSX", "name": "Phillips 66", "shares": 12, "entry": 138.60,
         "price": 142.30, "change_pct": 0.4, "sector": "Energy / Industrials"},
    ]
    demo_buys = [{"ticker": "GEV", "name": "GE Vernova", "score": 87, "action": "Buy",
                  "budget": 600, "price": 512.40, "change_pct": 1.2,
                  "why": "Grid + power buildout keeps orders flowing."}]
    demo_watch = [{"ticker": "GEV", "name": "GE Vernova", "score": 87, "growth": 36,
                   "quality": 22, "smart_money": 18, "gem": 11,
                   "price": 512.40, "change_pct": 1.2}]
    out = write_dashboard(
        positions=demo_positions, cash=184.20, goal=10000,
        buys=demo_buys, watchlist=demo_watch,
        targets={"SNDK": 55},
        earnings_dates={"SNDK": ("SanDisk", "2026-06-25", "after close")},
        macro_snapshot={"10-yr Treasury": ("4.19%", "-0.05", "down"),
                        "WTI Crude": ("$62.80", "-1.30", "down"),
                        "VIX": ("15.6", "-0.70", "down")},
    )
    import json
    print(json.dumps(out, indent=2)[:900], "\n...")
