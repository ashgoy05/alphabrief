"""
AlphaBrief - Daily Stock Research Bot
Runs every morning at 7 AM CT via GitHub Actions
6 sheets: Budget, Watchlist, Bought, Buying History, SIP, Rules
Now publishes a dashboard (docs/data.json) instead of emailing,
and enforces the buy/sell/sizing playbook (docs/rules.json) via rules_engine.
"""

import os, json, requests, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from googleapiclient.discovery import build

from build_dashboard import write_dashboard
from rules_engine import enforce

GDRIVE_FILE_ID  = "18aobOtBNbYqhiuP1X13z8VRSwYRnWwVEzV7Ec6zhhtQ"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

def safe_float(v):
    try: return float(str(v).replace(",","").replace("%","").replace("--","0").replace("#N/A","0") or 0)
    except: return 0.0

def parse_date(d):
    for fmt in ['%m/%d/%Y','%Y-%m-%d','%d/%m/%Y']:
        try: return datetime.strptime(str(d).strip(), fmt)
        except: pass
    return None

def get_week_start():
    today = datetime.now()
    return (today - timedelta(days=today.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)

def read_drive_sheet():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    svc = build("sheets", "v4", credentials=creds)
    # internal key -> possible tab names (first that exists with data wins).
    # Rename-proof: handles Watchlist <-> USA Stocks Watchlist, Bought <-> Already Bought, etc.
    tab_aliases = {
        "Budget":         ["Budget"],
        "Watchlist":      ["USA Stocks Watchlist", "Watchlist", "US Stocks Watchlist", "US Watchlist"],
        "Bought":         ["Already Bought", "Bought", "Holdings"],
        "Buying History": ["Buying History", "History"],
        "SIP":            ["SIP"],
        "Rules":          ["Rules"],
    }
    data = {}
    for key, names in tab_aliases.items():
        rows, used = [], None
        for tab_name in names:
            try:
                fetched = svc.spreadsheets().values().get(
                    spreadsheetId=GDRIVE_FILE_ID, range=f"{tab_name}!A1:Z200"
                ).execute().get("values", [])
            except Exception:
                continue
            if fetched:
                rows, used = fetched, tab_name
                break
        if rows:
            h = rows[0]
            data[key] = [dict(zip(h, r + [""]*(len(h)-len(r)))) for r in rows[1:] if any(c.strip() for c in r)]
            if used and used != names[0]:
                print(f"  ({key}: matched tab '{used}')")
        else:
            data[key] = []
            print(f"  (no tab found for '{key}' - tried {names})")
    print(f"Read: Budget={len(data['Budget'])}, Watchlist={len(data['Watchlist'])}, Bought={len(data['Bought'])}, History={len(data['Buying History'])}, SIP={len(data['SIP'])}, Rules={len(data['Rules'])}")
    return data

def get_portfolio_value(bought):
    return sum(safe_float(s.get("Current Amount", 0)) for s in bought)

def get_budget(data):
    sip_budget = 250
    direct_budget = 100
    for row in data.get("Budget", []):
        vals = list(row.values())
        label = str(vals[0] if vals else "").strip().lower()
        amt   = safe_float(vals[1] if len(vals) > 1 else 0)
        if "sip" in label:    sip_budget    = amt
        if "direct" in label: direct_budget = amt
    return sip_budget, direct_budget

def get_cash(data):
    """Uninvested cash in the IRA. Add a 'Cash | <amount>' row to the Budget
    tab to reflect it; defaults to 0 if absent."""
    for row in data.get("Budget", []):
        vals = list(row.values())
        label = str(vals[0] if vals else "").strip().lower()
        if "cash" in label:
            return safe_float(vals[1] if len(vals) > 1 else 0)
    return 0.0

def get_goal(data):
    """Your target value. Add a 'Goal' (or 'Target') row to the Budget tab to
    set it; defaults to 10000."""
    for row in data.get("Budget", []):
        vals = list(row.values())
        label = str(vals[0] if vals else "").strip().lower()
        if "goal" in label or "target" in label:
            return safe_float(vals[1] if len(vals) > 1 else 0) or 10000
    return 10000

def get_external_total(data):
    """Combined value of all your OTHER accounts (India book, MFs, 401k, HSA,
    NPS...) - everything OUTSIDE this US trading sheet. Add an 'Other Accounts'
    row to the Budget tab (label in col A, amount in col B); defaults to 0.
    This is added to Account Value WITHOUT double-counting the US sleeve."""
    for row in data.get("Budget", []):
        vals = list(row.values())
        label = str(vals[0] if vals else "").strip().lower()
        if "other account" in label or "external" in label or "net worth" in label:
            return safe_float(vals[1] if len(vals) > 1 else 0)
    return 0.0

def get_weekly_direct_spend(data):
    week_start   = get_week_start()
    spent        = 0.0
    buys_made    = []
    for h in data.get("Buying History", []):
        buy_type = str(h.get("Type", "")).strip().lower()
        if "sip" in buy_type:
            continue
        d = parse_date(h.get("Date", ""))
        if d and d >= week_start:
            amt = safe_float(h.get("Amount", 0))
            spent += amt
            buys_made.append({"symbol": h.get("Fund Code","?"), "amount": amt, "date": h.get("Date","?")})
    return spent, buys_made

def get_owned_symbols(data):
    owned = set()
    for s in data.get("Bought",         []): owned.add(s.get("Fund Code","").strip().upper())
    for s in data.get("Buying History", []): owned.add(s.get("Fund Code","").strip().upper())
    for s in data.get("SIP",            []): owned.add(s.get("Fund Code","").strip().upper())
    owned.discard("")
    return owned

def is_wednesday():
    return datetime.now().weekday() == 2

def market_status():
    et = datetime.now(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return "closed"
    mins = et.hour*60 + et.minute
    if mins < 570:  return "pre-open"   # before 9:30 ET
    if mins < 960:  return "open"       # before 16:00 ET
    return "closed"

def get_sip_alerts(data):
    bought_map = {s.get("Fund Code","").upper(): s for s in data.get("Bought",[])}
    alerts = []
    for sip in data.get("SIP", []):
        sym = sip.get("Fund Code","").upper()
        h   = bought_map.get(sym)
        if h:
            pnl = safe_float(h.get("P&L %", 0))
            if pnl < -10:
                alerts.append(f"{sym} down {abs(pnl):.1f}% — consider adding extra this week")
            elif pnl < -5:
                alerts.append(f"{sym} down {abs(pnl):.1f}% — slight dip, monitor")
    return alerts

def get_dip_opportunities(data):
    bought_map    = {s.get("Fund Code","").upper(): s for s in data.get("Bought",[])}
    watchlist_map = {s.get("Symbol","").upper(): s   for s in data.get("Watchlist",[])}
    dips = []
    for sym, h in bought_map.items():
        pnl = safe_float(h.get("P&L %", 0))
        if pnl < -5 and sym in watchlist_map:
            w = watchlist_map[sym]
            quality = safe_float(w.get("S&P Global Market Intelligence Quality", 0))
            ess     = w.get("Equity Summary Score (ESS) from LSEG StarMine","")
            if quality >= 60 or "bullish" in ess.lower():
                dips.append({"symbol": sym, "name": h.get("Fund Name", sym), "pnl_pct": pnl,
                             "current": safe_float(h.get("Current Nav", 0)),
                             "avg_buy": safe_float(h.get("Avg. Nav", 0)), "quality": quality})
    return sorted(dips, key=lambda x: x["pnl_pct"])

def parse_market_cap(v):
    try:
        v = str(v).replace(",","").replace("$","").strip()
        if "T" in v: return float(v.replace("T","")) * 1000
        if "B" in v: return float(v.replace("B",""))
        if "M" in v: return float(v.replace("M","")) / 1000
        return float(v) / 1e9
    except: return 10.0

def score_stock(s):
    score = 0
    reasons = []

    eps = safe_float(s.get("EPS Growth (Proj This Yr vs. Last Yr)", 0))
    if   eps > 200: pts = 20
    elif eps > 100: pts = 16
    elif eps > 50:  pts = 12
    elif eps > 20:  pts = 8
    elif eps > 0:   pts = 4
    else:           pts = 0
    score += pts
    if pts >= 12: reasons.append(f"EPS +{eps:.0f}%")

    gs = safe_float(s.get("S&P Global Market Intelligence Growth Stability", 0))
    if   gs >= 90: pts = 12
    elif gs >= 70: pts = 8
    elif gs >= 50: pts = 5
    else:          pts = 2
    score += pts
    if pts >= 8: reasons.append(f"Growth stability {gs:.0f}/100")

    p52 = safe_float(s.get("Price Performance (52 Weeks)", 0))
    if   p52 > 100: pts = 8
    elif p52 > 50:  pts = 6
    elif p52 > 20:  pts = 4
    elif p52 > 0:   pts = 2
    else:           pts = 0
    score += pts
    if pts >= 6: reasons.append(f"+{p52:.0f}% over 52wk")

    q = safe_float(s.get("S&P Global Market Intelligence Quality", 0))
    if   q >= 90: pts = 10
    elif q >= 70: pts = 7
    elif q >= 50: pts = 4
    else:         pts = 1
    score += pts
    if pts >= 7: reasons.append(f"Quality {q:.0f}/100")

    fh = safe_float(s.get("S&P Global Market Intelligence Financial Health", 0))
    score += 8 if fh >= 80 else 5 if fh >= 60 else 2

    peg_raw = str(s.get("PEG Ratio","")).replace("--","").strip()
    peg = safe_float(peg_raw) if peg_raw else None
    if   peg is None: pts = 2
    elif peg < 0.5:   pts = 7
    elif peg < 1.0:   pts = 5
    elif peg < 2.0:   pts = 3
    else:             pts = 1
    score += pts
    if peg and peg < 1.0: reasons.append(f"PEG {peg:.2f} — cheap vs growth")

    ic = safe_float(s.get("Institutional Ownership (Last vs. Prior Qtr)", 0))
    if   ic > 20: pts = 10
    elif ic > 10: pts = 7
    elif ic > 5:  pts = 5
    elif ic > 0:  pts = 2
    else:         pts = 0
    score += pts
    if pts >= 7: reasons.append(f"Institutions buying +{ic:.1f}%")

    ess = str(s.get("Equity Summary Score Change (1 Month)","")).lower()
    if   "large increase"    in ess: pts = 7
    elif "moderate increase" in ess: pts = 4
    elif "stable"            in ess: pts = 2
    else:                            pts = 0
    score += pts
    if pts >= 4: reasons.append("Analyst score rising")

    lseg = safe_float(s.get("LSEG I/B/E/S Estimates", 0))
    score += 3 if lseg >= 3.0 else 2 if lseg >= 2.0 else 1

    mc = parse_market_cap(s.get("Market Capitalization","10B"))
    if   mc < 1:  pts = 6
    elif mc < 5:  pts = 4
    elif mc < 50: pts = 2
    else:         pts = 1
    score += pts
    if mc < 2: reasons.append(f"Small cap ${mc:.1f}B — room to grow")

    val = safe_float(s.get("S&P Global Market Intelligence Valuation", 0))
    score += 5 if val >= 90 else 3 if val >= 70 else 1
    if val >= 90: reasons.append(f"Undervalued ({val:.0f}/100)")

    beta = safe_float(s.get("Beta (1 Year Annualized)", 1))
    score += 4 if 0.8 <= beta <= 1.5 else 2 if beta <= 2.5 else 1

    final = min(100, score)
    signal = "STRONG BUY" if final >= 80 else "BUY" if final >= 65 else "WATCH" if final >= 50 else "SKIP"
    return {"score": final, "signal": signal, "reasons": reasons[:4], "eps": eps, "p52": p52,
            "quality": q, "mc": mc, "sector": s.get("Sector",""), "industry": s.get("Industry",""),
            "price": safe_float(s.get("Security Price",0)), "beta": beta, "peg": peg,
            "symbol": s.get("Symbol",""), "name": s.get("Company Name","")}

# ----------------------------------------------------------- AI narrative
def get_market_news():
    today  = datetime.now().strftime("%B %d %Y")
    prompt = f'Search stock market news {today}. Return ONLY JSON: {{"market_summary":"one sentence","top_stories":["s1","s2","s3"],"sectors_moving":"one sentence"}}'
    r = requests.post("https://api.anthropic.com/v1/messages",
        headers={"x-api-key":os.environ["ANTHROPIC_API_KEY"],"anthropic-version":"2023-06-01","content-type":"application/json"},
        json={"model":ANTHROPIC_MODEL,"max_tokens":400,"tools":[{"type":"web_search_20250305","name":"web_search"}],"messages":[{"role":"user","content":prompt}]},
        timeout=45)
    r.raise_for_status()
    for block in r.json().get("content",[]):
        if block.get("type") == "text":
            try:
                m = re.search(r'\{.*\}', block["text"], re.DOTALL)
                if m: return json.loads(m.group())
            except: pass
            return {"market_summary": block["text"][:150], "top_stories":[], "sectors_moving":""}
    return {"market_summary":"Markets open.","top_stories":[],"sectors_moving":""}

def get_macro():
    """Best-effort macro snapshot via the same web-search tool. Returns None on
    any failure so the dashboard just hides the panel."""
    try:
        prompt = ('Search current US market data right now. Return ONLY JSON, no prose: '
                  '{"10-yr Treasury":["4.19%","-0.05","down"],'
                  '"WTI Crude":["$62.80","-1.30","down"],'
                  '"VIX":["15.6","-0.70","down"]}  '
                  'Each value is [display_value, signed_change, "up"|"down"|"flat"].')
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":os.environ["ANTHROPIC_API_KEY"],"anthropic-version":"2023-06-01","content-type":"application/json"},
            json={"model":ANTHROPIC_MODEL,"max_tokens":400,"tools":[{"type":"web_search_20250305","name":"web_search"}],"messages":[{"role":"user","content":prompt}]},
            timeout=45)
        r.raise_for_status()
        text = "".join(b["text"] for b in r.json().get("content",[]) if b.get("type")=="text")
        obj = json.loads(re.search(r'\{.*\}', text, re.DOTALL).group())
        out = {k:(str(v[0]), str(v[1]), str(v[2])) for k,v in obj.items()
               if isinstance(v, list) and len(v) >= 3}
        return out or None
    except Exception as e:
        print("macro fetch skipped:", e)
        return None

def clean_brief(text):
    skip = ["let me search","let me look","i need to","i'll search","now let me",
            "now i have","let me compile","searching for","looking up","i will search",
            "let me find","i'm going to","let me check","based on my search","i found that"]
    lines = [l for l in text.split("\n") if not any(p in l.lower() for p in skip) and l.strip() not in ["---","***","___"]]
    return re.sub(r'\n{3,}','\n\n',"\n".join(lines)).strip()

def build_prompt(data, news):
    today      = datetime.now().strftime("%A, %B %d, %Y")
    today_wed  = is_wednesday()
    bought     = data.get("Bought",[])
    sip_list   = data.get("SIP",[])
    owned      = get_owned_symbols(data)
    sip_budget, direct_budget = get_budget(data)
    portfolio_val = get_portfolio_value(bought)
    sip_alerts = get_sip_alerts(data)
    dip_opps   = get_dip_opportunities(data)

    spent_this_week, buys_this_week = get_weekly_direct_spend(data)
    budget_remaining = max(0, direct_budget - spent_this_week)
    budget_exhausted = budget_remaining <= 0

    watch_raw = [s for s in data.get("Watchlist",[]) if s.get("Symbol","").upper() not in owned]
    scored    = sorted([{**s, **score_stock(s)} for s in watch_raw], key=lambda x: x["score"], reverse=True)
    strong_buys = [s for s in scored if s["signal"] == "STRONG BUY"][:3]
    buys        = [s for s in scored if s["signal"] == "BUY"][:3]
    hidden_gems = [s for s in scored if s["mc"] < 2 and s["score"] >= 55][:3]

    def fmt_stock(s):
        reasons_str = " | ".join(s.get("reasons",[]))
        return (f"  {s['symbol']} ({s.get('Company Name',s['symbol'])[:22]}) "
                f"SCORE:{s['score']}/100 SIGNAL:{s['signal']}\n"
                f"    Price:${s['price']:.2f} | Mkt Cap:${s['mc']:.1f}B | Sector:{s['sector']}\n"
                f"    WHY: {reasons_str}")

    top_picks_str = "\n".join([fmt_stock(s) for s in (strong_buys + buys)[:6]])
    gems_str      = "\n".join([fmt_stock(s) for s in hidden_gems]) if hidden_gems else "  None this week"
    dip_str = "\n".join([
        f"  {d['symbol']} ({d['name'][:18]}) — down {abs(d['pnl_pct']):.1f}% | avg ${d['avg_buy']:.2f} | now ${d['current']:.2f}"
        for d in dip_opps]) or "  None today"
    sp    = "\n".join([f"  {s.get('Fund Code','?')} ${s.get('Amount','?')}/wk" for s in sip_list])
    b_str = "\n".join([f"  {s.get('Fund Code','?')} P&L:{s.get('P&L %','?')}%" for s in sorted(bought, key=lambda x: safe_float(x.get("P&L %",0)), reverse=True)[:12]])
    news_str = f"Market: {news.get('market_summary','')} | Sectors: {news.get('sectors_moving','')} | {' | '.join(news.get('top_stories',[]))}"

    if budget_exhausted:
        buy_section = ("NO BUYS THIS WEEK — BUDGET USED\n"
                       f"Already spent ${spent_this_week:.0f} this week. List 2 stocks to research this weekend.")
    else:
        buy_section = (f"BEST BUY TODAY (${budget_remaining:.0f} budget)\n"
                       "Pick the #1 stock from the scored list (STRONG BUY first, then BUY, then gem).\n"
                       "**TICKER** — Company name (what they do in one line)\n"
                       "- Why it could make money: the growth story, simply\n"
                       "- Score breakdown: the 2-3 biggest reasons it scored high\n"
                       f"- How much: $[amount within ${budget_remaining:.0f}]\n"
                       "- One risk: what could go wrong, plainly\n\n"
                       "HIDDEN GEM PICK (high risk, could 2-5x)\n"
                       "Best small-cap from the gems list.\n"
                       "**TICKER** — what they do\n- Why it could 2-5x\n- Score: [score]/100\n- How much: $[small]")

    budget_rule = "DO NOT suggest buys — budget exhausted" if budget_exhausted else f"Suggest buys within ${budget_remaining:.0f}"
    sip_block = ""
    if today_wed:
        sip_block = f"\nTODAY IS WEDNESDAY (SIP DAY):\n{sp}\nAlerts: {', '.join(sip_alerts) if sip_alerts else 'All healthy'}"
    elif sip_alerts:
        sip_block = f"\nSIP ALERT: {', '.join(sip_alerts)}"

    return f"""You are a sharp stock analyst helping a RISK-TAKING growth investor. Write in simple plain English — like texting a smart friend. Explain any finance term simply.

DATE: {today}
PORTFOLIO VALUE: ${portfolio_val:,.0f}
BUDGET: ${budget_remaining:.0f} of ${direct_budget:.0f} left this week

TODAY'S MARKET: {news_str}

TOP SCORED STOCKS (Growth 40 + Quality 25 + Smart Money 20 + Hidden Gem 15 = 100):
{top_picks_str}

HIDDEN GEMS (under $2B):
{gems_str}

DIP-BUY WATCH (own it, down >5%):
{dip_str}

HOLDINGS:
{b_str}
{sip_block}

RULES:
- Only recommend stocks from the scored list
- {budget_rule}
- Risk taker — bold picks welcome; explain WHY in simple words
- Use ** around ticker symbols

Write ONLY the brief. No thinking out loud. Start directly:

WHAT'S HAPPENING TODAY
2 plain sentences — the market and what it means for this portfolio.

{buy_section}

ONE THING TO WATCH
One news story that could move stocks this week. 2 simple sentences.

Under 320 words total."""

def get_brief(prompt):
    r = requests.post("https://api.anthropic.com/v1/messages",
        headers={"x-api-key":os.environ["ANTHROPIC_API_KEY"],"anthropic-version":"2023-06-01","content-type":"application/json"},
        json={"model":ANTHROPIC_MODEL,"max_tokens":900,"messages":[{"role":"user","content":prompt}]},
        timeout=45)
    r.raise_for_status()
    text = "".join(b["text"] for b in r.json().get("content",[]) if b.get("type")=="text")
    return clean_brief(text)

# ----------------------------------------------------------- dashboard inputs
def build_positions(data):
    sector_by_sym = {s.get("Symbol","").upper(): s.get("Sector","") for s in data.get("Watchlist",[])}
    out = []
    for s in data.get("Bought", []):
        tk = s.get("Fund Code","").strip().upper()
        if not tk:
            continue
        cur = safe_float(s.get("Current Nav", 0))
        avg = safe_float(s.get("Avg. Nav", 0))
        amt = safe_float(s.get("Current Amount", 0))
        price = cur if cur > 0 else avg
        shares = round(amt / price, 3) if price > 0 else 0
        out.append({
            "ticker": tk,
            "name": s.get("Fund Name", tk),
            "shares": shares,
            "entry": avg if avg > 0 else None,
            "price": round(price, 2) if price > 0 else None,
            "value": round(amt, 2),
            "change_pct": 0,
            "gain_pct": round(safe_float(s.get("P&L %", 0)), 2),
            "sector": sector_by_sym.get(tk) or None,
        })
    return out

def build_calls_and_watchlist(data, budget_remaining):
    owned = get_owned_symbols(data)
    watch_raw = [s for s in data.get("Watchlist", []) if s.get("Symbol","").upper() not in owned]
    scored = sorted([{**s, **score_stock(s)} for s in watch_raw], key=lambda x: x["score"], reverse=True)

    watchlist = [{
        "ticker": s["symbol"], "name": s["name"] or s["symbol"],
        "score": s["score"], "price": round(s["price"], 2), "change_pct": 0,
    } for s in scored[:15] if s["symbol"]]

    buys = []
    if budget_remaining > 0:
        picks = [s for s in scored if s["signal"] in ("STRONG BUY", "BUY")][:3]
        for i, s in enumerate(picks):
            buys.append({
                "ticker": s["symbol"], "name": s["name"] or s["symbol"],
                "score": s["score"],
                "action": "Strong buy" if s["signal"] == "STRONG BUY" else "Buy",
                "budget": round(budget_remaining) if i == 0 else None,
                "price": round(s["price"], 2), "change_pct": 0,
                "why": " · ".join(s["reasons"][:3]) or f"Scored {s['score']}/100 across growth, quality and smart-money.",
            })
    return buys, watchlist

# ----------------------------------------------------------- main
def main():
    print(f"AlphaBrief starting — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')} | {datetime.now().strftime('%A')}")
    data = read_drive_sheet()
    sip_b, direct_b = get_budget(data)
    spent, _ = get_weekly_direct_spend(data)
    remaining = max(0, direct_b - spent)
    print(f"Budget: ${direct_b} | Spent: ${spent:.2f} | Remaining: ${remaining:.2f}")

    briefing = None
    try:
        news = get_market_news()
        briefing = get_brief(build_prompt(data, news))
        print("Brief preview:\n" + briefing[:200])
    except Exception as e:
        print("brief skipped:", e)

    macro = get_macro()
    positions = build_positions(data)
    buys, watchlist = build_calls_and_watchlist(data, remaining)

    # ---- rules engine: enforce the playbook on today's data -> docs/rules.json
    report = None
    try:
        scores = {s.get("Symbol",""): score_stock(s)["score"] for s in data.get("Watchlist", [])}
        report = enforce(data, scores)
        os.makedirs("docs", exist_ok=True)
        with open("docs/rules.json", "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"Rules: {len(report['buy_candidates'])} clean buys, "
              f"{len(report['holding_actions'])} holding actions, "
              f"{len(report['theme_breaches'])} theme breaches -> docs/rules.json")
        for h in report["holding_actions"]:
            tags = ", ".join(a for a, _ in h["actions"])
            print(f"   {h['symbol']}: {tags}")
        for b in report["theme_breaches"]:
            print(f"   THEME: {b}")
    except Exception as e:
        print("rules engine skipped:", e)

    write_dashboard(
        positions=positions,
        cash=get_cash(data),
        goal=get_goal(data),
        external_total=get_external_total(data),
        buys=buys,
        watchlist=watchlist,
        targets=None,            # no analyst targets in the sheet -> trims fire on run-up/stop only
        earnings_dates=None,     # add a source later to light up "Reporting soon"
        macro_snapshot=macro,
        market_status=market_status(),
        briefing=briefing,
    )
    print(f"Dashboard written — {len(positions)} positions, {len(buys)} buys, {len(watchlist)} watchlist.")
    print("Done.")

if __name__ == "__main__":
    main()
