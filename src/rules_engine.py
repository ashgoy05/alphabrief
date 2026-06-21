"""
AlphaBrief - Rules Engine
=========================
Reads the 'Rules' tab (the single source of truth for every threshold) and
enforces the buy / sell / sizing playbook against the 'Watchlist' (candidates)
and 'Bought' (holdings) tabs.

Design: the spreadsheet is the data source; this module is the engine. Change a
number in the Rules tab and behaviour changes on the next run - no code edits.

It reuses the same googleapiclient `service` object AlphaBrief already builds,
and the same short-row parsing pattern used elsewhere in the project.

Quick wire-in (in your main script, after you compute scores):
    from rules_engine import enforce
    report = enforce(service, SPREADSHEET_ID, scores)   # scores = {symbol: 0-100}
    # then feed `report` into the dashboard template

Auto gates (engine enforces): score threshold, EPS-growth floor, PEG ceiling,
smart-money check, single-name cap, theme cap, dip-buy, cut-loss, take-profit.
Human gates (engine surfaces as a checklist, you confirm): moat, catalyst,
thesis-in-2-lines, "thesis broke", etc. Those can't be judged by code - the
engine prints them next to each BUY-OK so you never skip them.
"""

import datetime
from typing import Dict, List, Optional

_RANGE = "{tab}!A1:Z200"
_REF_KEYS = ("buy_check", "sell_trigger", "allocation")


def _read_tab(service, sid: str, tab: str) -> List[dict]:
    res = service.spreadsheets().values().get(
        spreadsheetId=sid, range=_RANGE.format(tab=tab)).execute()
    vals = res.get("values", [])
    if not vals:
        return []
    head = vals[0]
    return [dict(zip(head, r + [""] * (len(head) - len(r)))) for r in vals[1:]]


def _num(x, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(str(x).replace(",", "").replace("%", "").replace("$", "").strip())
    except (ValueError, AttributeError):
        return default


def _days_since(date_str: str) -> Optional[int]:
    """Days between today and a stamped date; handles MM/DD/YYYY and ISO."""
    s = (date_str or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            d = datetime.datetime.strptime(s, fmt).date()
            return (datetime.date.today() - d).days
        except ValueError:
            continue
    return None


def load_rules(service, sid: str, tab: str = "Rules"):
    """Return (cfg, ref): cfg = {key: value} thresholds, ref = {category: [items]}."""
    cfg: Dict[str, str] = {}
    ref: Dict[str, List[str]] = {}
    for row in _read_tab(service, sid, tab):
        key = (row.get("Key") or "").strip()
        val = (row.get("Value") or "").strip()
        if not key:
            continue
        if key in _REF_KEYS:
            ref.setdefault(key, []).append(val)
        else:
            cfg[key] = val
    return cfg, ref


def _cfg(cfg: Dict[str, str], key: str, default: float) -> float:
    v = _num(cfg.get(key, ""), None)
    return v if v is not None else default


def evaluate_candidate(row: dict, cfg: Dict[str, str], score: Optional[float]) -> dict:
    """Buy-side gate for one Watchlist row. `score` = computed AlphaBrief 0-100."""
    reasons: List[str] = []
    ok = True
    thr = _cfg(cfg, "score_threshold", 50)
    if score is None or score < thr:
        ok = False
        reasons.append(f"score {score} < {thr:.0f}")

    eps = _num(row.get("EPS Growth (Proj This Yr vs. Last Yr)", ""))
    min_eps = _cfg(cfg, "min_eps_growth_pct", 20)
    if eps is not None and eps < min_eps:
        ok = False
        reasons.append(f"EPS growth {eps:.0f}% < {min_eps:.0f}%")

    peg = _num(row.get("PEG Ratio", ""))
    max_peg = _cfg(cfg, "max_peg", 2.5)
    if peg is not None and peg > max_peg:
        ok = False
        reasons.append(f"PEG {peg:.1f} > {max_peg:.1f}")

    if cfg.get("require_smart_money", "Y").strip().upper() == "Y":
        trend = _num(row.get("Institutional Ownership (Last vs. Prior Qtr)", ""))
        if trend is not None and trend < 0:
            ok = False
            reasons.append("smart money trimming")

    if ok:
        action = "BUY-OK"
    elif score is not None and score >= thr - 10:
        action = "WATCH"
    else:
        action = "SKIP"
    return {"symbol": row.get("Symbol", ""), "score": score,
            "action": action, "reasons": reasons}


def evaluate_holding(row: dict, cfg: Dict[str, str], port_value: float,
                     last_action: str = "", cooldown_days: float = 0) -> List[tuple]:
    """Sell / sizing gate for one Bought row. Returns list of (action, why).

    If you've stamped a 'Last Action' date within `cooldown_days`, the holding
    goes quiet - so a position you already trimmed or reviewed-and-held won't
    re-flag every morning just because its P&L % is unchanged.
    """
    days = _days_since(last_action)
    if days is not None and cooldown_days and 0 <= days <= cooldown_days:
        return []  # acted recently - stay quiet until the cooldown lapses

    out: List[tuple] = []
    pnl = _num(row.get("P&L %", ""))
    cur = _num(row.get("Current Amount", ""), 0) or 0

    cutloss = _cfg(cfg, "cutloss_review_pct", -20)
    dip = _cfg(cfg, "dip_buy_trigger_pct", -5)
    tp = _cfg(cfg, "take_profit_trim_pct", 100)
    cap1 = _cfg(cfg, "max_single_name_pct", 10)

    if pnl is not None:
        if pnl <= cutloss:
            out.append(("SELL-REVIEW", f"down {pnl:.0f}% - check thesis or cut"))
        elif pnl <= dip:
            out.append(("ADD-DIP", f"down {pnl:.0f}% - add a tranche if thesis intact"))
        if pnl >= tp:
            out.append(("TRIM-PROFIT", f"up {pnl:.0f}% - trim into strength"))

    if port_value and (cur / port_value * 100) > cap1:
        out.append(("TRIM-SIZE", f"{cur / port_value * 100:.0f}% of book > {cap1:.0f}% cap"))
    return out


def enforce(service, sid: str, scores: Optional[Dict[str, float]] = None) -> dict:
    """Run every gate and return a structured report for the dashboard."""
    scores = scores or {}
    cfg, ref = load_rules(service, sid)
    watch = _read_tab(service, sid, "Watchlist")
    held = _read_tab(service, sid, "Bought")

    port_value = sum((_num(r.get("Current Amount", ""), 0) or 0) for r in held)

    # sector exposure: map symbol -> sector from Watchlist, sum holdings by sector
    sector_of = {r.get("Symbol", ""): (r.get("Sector", "") or "Unknown") for r in watch}
    sector_val: Dict[str, float] = {}
    for r in held:
        sec = sector_of.get(r.get("Fund Code", ""), "Unknown")
        sector_val[sec] = sector_val.get(sec, 0) + (_num(r.get("Current Amount", ""), 0) or 0)
    cap_theme = _cfg(cfg, "max_theme_pct", 25)
    theme_breaches = [
        f"{sec} = {(val / port_value * 100):.0f}% of book > {cap_theme:.0f}% cap"
        for sec, val in sector_val.items()
        if sec and sec != "Unknown" and port_value and (val / port_value * 100) > cap_theme
    ]

    buy_candidates = [
        b for r in watch
        for b in [evaluate_candidate(r, cfg, scores.get(r.get("Symbol", "")))]
        if b["action"] == "BUY-OK"
    ]

    cooldown = _cfg(cfg, "review_cooldown_days", 14)
    holding_actions = []
    for r in held:
        acts = evaluate_holding(r, cfg, port_value, r.get("Last Action", ""), cooldown)
        if acts:
            holding_actions.append({
                "symbol": r.get("Fund Code", ""),
                "pnl_pct": r.get("P&L %", ""),
                "account": r.get("Account Type", ""),
                "actions": acts,
            })

    return {
        "portfolio_value": round(port_value, 2),
        "buy_candidates": buy_candidates,
        "holding_actions": holding_actions,
        "theme_breaches": theme_breaches,
        "target_holdings": int(_cfg(cfg, "target_holdings", 18)),
        "current_holdings": len(held),
        "buy_checklist": ref.get("buy_check", []),
        "sell_triggers": ref.get("sell_trigger", []),
        "allocation_order": ref.get("allocation", []),
        "config": cfg,
    }


if __name__ == "__main__":
    print("rules_engine: import and call enforce(service, SPREADSHEET_ID, scores).")
