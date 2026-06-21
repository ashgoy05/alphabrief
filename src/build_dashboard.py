"""
build_dashboard.py  --  the "output" stage.

Drops docs/data.json (what the website reads) and appends
docs/track_record.json (the graded history). The GitHub Action then commits
docs/ and Pages serves it.

Single brain: this stage reads docs/rules.json (written by rules_engine.enforce
in main) and treats it as the authoritative verdict. The dashboard no longer
makes its own cut/hold call - it defers to the engine, so "Cut" and "add the
dip" can never disagree again.
"""

from __future__ import annotations
import datetime as dt
import json, os
from zoneinfo import ZoneInfo

import features as F

DOCS = "docs"
TRACK_LOG = f"{DOCS}/track_record.json"
RULES_JSON = f"{DOCS}/rules.json"
CHICAGO = ZoneInfo("America/Chicago")

# most-severe-first: when a holding trips several rules, this one wins the badge
_SEVERITY = {"SELL-REVIEW": 0, "TRIM-SIZE": 1, "TRIM-PROFIT": 2, "ADD-DIP": 3}
# engine actions that justify the dashboard showing a sell/trim card
_CUT_LIKE = {"SELL-REVIEW", "TRIM-SIZE", "TRIM-PROFIT"}


def _load_rules_report(path=RULES_JSON):
    """The engine's output for today; None if it hasn't run / isn't present."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _engine_verdicts(report):
    """ticker -> single most-severe engine action, e.g. {'PLTR': 'ADD-DIP'}."""
    verdicts = {}
    for h in (report or {}).get("holding_actions", []):
        tk = str(h.get("symbol", "")).upper()
        acts = [a for a, _ in h.get("actions", []) if a]
        if tk and acts:
            verdicts[tk] = sorted(acts, key=lambda a: _SEVERITY.get(a, 9))[0]
    return verdicts


def write_dashboard(
    *,
    positions,            # WIRE: from your Bought tab + live prices.
    cash,                 # WIRE: uninvested cash in the IRA
    goal,                 # WIRE: your target (read from Budget 'Goal' row; e.g. 1000000)
    buys,                 # WIRE: your scored buy calls
    watchlist,            # WIRE: scored watchlist rows
    targets=None,         # WIRE(optional): {ticker: analyst_target}
    earnings_dates=None,  # WIRE(optional): {ticker:(name,'YYYY-MM-DD',when)}
    macro_snapshot=None,  # WIRE(optional): {'10-yr Treasury':(val,chg,'down'), ...}
    market_status="pre-open",
    briefing=None,        # WIRE(optional): your AI morning brief (plain text)
    external_total=0,     # STEP-2 HOOK: combined value of your OTHER accounts
                          #   (India book, MFs, 401k, HSA, NPS...). Defaults to 0
                          #   so today nothing changes; wire it up to roll this
                          #   dashboard into a single $1M total-net-worth view.
):
    today = dt.datetime.now(CHICAGO).date()
    owned = {p["ticker"] for p in positions}
    prices = {p["ticker"]: p.get("price") for p in positions}
    prices.update({b["ticker"]: b.get("price") for b in buys})

    F.position_flags(positions, targets)

    # ---- single brain: defer all sell/trim verdicts to the rules engine ----
    report = _load_rules_report()
    verdicts = _engine_verdicts(report)

    # tag each position with the engine's authoritative verdict
    for p in positions:
        v = verdicts.get(str(p.get("ticker", "")).upper())
        if v:
            p["verdict"] = v

    sells = F.sell_signals(positions, targets)
    if verdicts:  # only override when the engine actually produced output
        sells = [s for s in sells
                 if verdicts.get(str(s.get("ticker", "")).upper()) in _CUT_LIKE]

    portfolio = F.compute_portfolio(positions, cash, goal)

    data = {
        "generated_at": dt.datetime.now(CHICAGO).isoformat(timespec="seconds"),
        "market_status": market_status,
        "briefing": briefing,
        "portfolio": portfolio,
        "calls": {
            "buys": buys,
            "sells": sells,
        },
        "positions": positions,
        "watchlist": watchlist,
        "earnings": F.earnings_this_week(earnings_dates or {}, owned, today),
        "macro": F.macro_block(macro_snapshot) if macro_snapshot else None,
        "track_record": _track(buys, prices, today),
        # STEP-2 HOOK: external accounts ride along here until compute_portfolio
        # is taught to fold them into Account Value for the combined $1M view.
        "external_accounts": {"total": external_total} if external_total else None,
        # surface the engine's own summary so the page can show today's actions
        "rules": report,
    }

    F._save(f"{DOCS}/data.json", data)
    return data


def _track(buys, prices, today):
    F.append_track_record(TRACK_LOG, buys, prices, today)
    return F.summarize_track_record(TRACK_LOG, prices, today=today)


# ----------------------------------------------------------- demo / self-test
if __name__ == "__main__":
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
        positions=demo_positions, cash=184.20, goal=1000000,
        buys=demo_buys, watchlist=demo_watch,
        targets={"SNDK": 55},
        earnings_dates={"SNDK": ("SanDisk", "2026-06-25", "after close")},
        macro_snapshot={"10-yr Treasury": ("4.19%", "-0.05", "down"),
                        "WTI Crude": ("$62.80", "-1.30", "down"),
                        "VIX": ("15.6", "-0.70", "down")},
    )
    import json as _j
    print(_j.dumps(out, indent=2)[:900], "\n...")
