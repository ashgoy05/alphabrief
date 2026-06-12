"""
AlphaBrief - Daily Stock Research Bot
Runs every morning at 7 AM CT via GitHub Actions
5 sheets: Budget, Watchlist, Bought, Buying History, SIP
Tracks weekly direct buy budget — stops suggesting if budget used up
"""

import os, json, smtplib, requests, re
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2 import service_account
from googleapiclient.discovery import build

GDRIVE_FILE_ID  = "18aobOtBNbYqhiuP1X13z8VRSwYRnWwVEzV7Ec6zhhtQ"
TO_EMAIL        = "ashishgoyal.ietc@gmail.com"
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
    data = {}
    for sheet in ["Budget", "Watchlist", "Bought", "Buying History", "SIP"]:
        rows = svc.spreadsheets().values().get(
            spreadsheetId=GDRIVE_FILE_ID, range=f"{sheet}!A1:Z200"
        ).execute().get("values", [])
        if rows:
            h = rows[0]
            data[sheet] = [dict(zip(h, r + [""]*(len(h)-len(r)))) for r in rows[1:] if any(c.strip() for c in r)]
        else:
            data[sheet] = []
    print(f"Read: Budget={len(data['Budget'])}, Watchlist={len(data['Watchlist'])}, Bought={len(data['Bought'])}, History={len(data['Buying History'])}, SIP={len(data['SIP'])}")
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

def get_weekly_direct_spend(data):
    """
    Calculate how much direct buy budget has been used THIS week.
    Only counts rows where Type = 'Claude Suggested' (not SIP).
    Week resets every Monday.
    """
    week_start   = get_week_start()
    spent        = 0.0
    buys_made    = []

    for h in data.get("Buying History", []):
        buy_type = str(h.get("Type", "")).strip().lower()
        # Skip SIP rows — they don't count toward direct buy budget
        if "sip" in buy_type:
            continue
        d = parse_date(h.get("Date", ""))
        if d and d >= week_start:
            amt = safe_float(h.get("Amount", 0))
            spent += amt
            buys_made.append({
                "symbol": h.get("Fund Code","?"),
                "amount": amt,
                "date":   h.get("Date","?")
            })
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
                dips.append({
                    "symbol":  sym,
                    "name":    h.get("Fund Name", sym),
                    "pnl_pct": pnl,
                    "current": safe_float(h.get("Current Nav", 0)),
                    "avg_buy": safe_float(h.get("Avg. Nav", 0)),
                    "quality": quality,
                })
    return sorted(dips, key=lambda x: x["pnl_pct"])

def parse_market_cap(v):
    """Convert '$2.27B' / '$128M' to float in billions"""
    try:
        v = str(v).replace(",","").replace("$","").strip()
        if "T" in v: return float(v.replace("T","")) * 1000
        if "B" in v: return float(v.replace("B",""))
        if "M" in v: return float(v.replace("M","")) / 1000
        return float(v) / 1e9
    except: return 10.0

def score_stock(s):
    """
    Score 0-100 using all 30 watchlist columns.
    4 categories: Growth (40pts) + Quality (25pts) + Smart Money (20pts) + Hidden Gem (15pts)
    """
    score = 0
    reasons = []

    # ── GROWTH SIGNAL (40 pts) ───────────────────────────────────────
    eps = safe_float(s.get("EPS Growth (Proj This Yr vs. Last Yr)", 0))
    if   eps > 200: pts = 20
    elif eps > 100: pts = 16
    elif eps > 50:  pts = 12
    elif eps > 20:  pts = 8
    elif eps > 0:   pts = 4
    else:           pts = 0
    score += pts
    if pts >= 12: reasons.append(f"EPS +{eps:.0f}% 🚀")

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
    if pts >= 6: reasons.append(f"+{p52:.0f}% 52wk")

    # ── QUALITY & FUNDAMENTALS (25 pts) ─────────────────────────────
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
    if peg and peg < 1.0: reasons.append(f"PEG {peg:.2f} — cheap vs growth!")

    # ── SMART MONEY SIGNAL (20 pts) ──────────────────────────────────
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
    if pts >= 4: reasons.append(f"Analyst score {s.get('Equity Summary Score Change (1 Month)','')} ↑")

    lseg = safe_float(s.get("LSEG I/B/E/S Estimates", 0))
    score += 3 if lseg >= 3.0 else 2 if lseg >= 2.0 else 1

    # ── HIDDEN GEM BONUS (15 pts) ────────────────────────────────────
    mc = parse_market_cap(s.get("Market Capitalization","10B"))
    if   mc < 1:  pts = 6   # micro/small — most room to grow
    elif mc < 5:  pts = 4   # small/mid
    elif mc < 50: pts = 2   # mid
    else:         pts = 1   # large
    score += pts
    if mc < 2: reasons.append(f"Small cap ${mc:.1f}B — room to grow 💎")

    val = safe_float(s.get("S&P Global Market Intelligence Valuation", 0))
    score += 5 if val >= 90 else 3 if val >= 70 else 1
    if val >= 90: reasons.append(f"Undervalued ({val}/100)")

    beta = safe_float(s.get("Beta (1 Year Annualized)", 1))
    score += 4 if 0.8 <= beta <= 1.5 else 2 if beta <= 2.5 else 1

    final = min(100, score)
    signal = "STRONG BUY" if final >= 80 else "BUY" if final >= 65 else "WATCH" if final >= 50 else "SKIP"

    return {
        "score":    final,
        "signal":   signal,
        "reasons":  reasons[:4],
        "eps":      eps,
        "p52":      p52,
        "quality":  q,
        "mc":       mc,
        "sector":   s.get("Sector",""),
        "industry": s.get("Industry",""),
        "price":    safe_float(s.get("Security Price",0)),
        "beta":     beta,
        "peg":      peg,
        "symbol":   s.get("Symbol",""),
        "name":     s.get("Company Name",""),
    }

def get_market_news():
    today  = datetime.now().strftime("%B %d %Y")
    prompt = f'Search stock market news {today}. Return ONLY JSON: {{"market_summary":"one sentence","top_stories":["s1","s2","s3"],"sectors_moving":"one sentence"}}'
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key":os.environ["ANTHROPIC_API_KEY"],"anthropic-version":"2023-06-01","content-type":"application/json"},
        json={"model":ANTHROPIC_MODEL,"max_tokens":400,"tools":[{"type":"web_search_20250305","name":"web_search"}],"messages":[{"role":"user","content":prompt}]},
        timeout=45
    )
    r.raise_for_status()
    for block in r.json().get("content",[]):
        if block.get("type") == "text":
            try:
                m = re.search(r'\{.*\}', block["text"], re.DOTALL)
                if m: return json.loads(m.group())
            except: pass
            return {"market_summary": block["text"][:150], "top_stories":[], "sectors_moving":""}
    return {"market_summary":"Markets open.","top_stories":[],"sectors_moving":""}

def clean_brief(text):
    skip = ["let me search","let me look","i need to","i'll search","now let me",
            "now i have","let me compile","searching for","looking up","i will search",
            "let me find","i'm going to","let me check","based on my search","i found that"]
    lines = [l for l in text.split("\n") if not any(p in l.lower() for p in skip) and l.strip() not in ["---","***","___"]]
    return re.sub(r'\n{3,}','\n\n',"\n".join(lines)).strip()

def build_prompt(data, news):
    today         = datetime.now().strftime("%A, %B %d, %Y")
    today_wed     = is_wednesday()
    bought        = data.get("Bought",[])
    sip_list      = data.get("SIP",[])
    owned         = get_owned_symbols(data)
    sip_budget, direct_budget = get_budget(data)
    portfolio_val = get_portfolio_value(bought)
    sip_alerts    = get_sip_alerts(data)
    dip_opps      = get_dip_opportunities(data)

    spent_this_week, buys_this_week = get_weekly_direct_spend(data)
    budget_remaining = max(0, direct_budget - spent_this_week)
    budget_exhausted = budget_remaining <= 0

    print(f"Budget: ${direct_budget} | Spent: ${spent_this_week:.2f} | Remaining: ${budget_remaining:.2f}")

    # Score ALL watchlist stocks using 30-column framework
    watch_raw = [s for s in data.get("Watchlist",[]) if s.get("Symbol","").upper() not in owned]
    scored    = sorted([{**s, **score_stock(s)} for s in watch_raw], key=lambda x: x["score"], reverse=True)

    # Top picks by signal
    strong_buys = [s for s in scored if s["signal"] == "STRONG BUY"][:3]
    buys        = [s for s in scored if s["signal"] == "BUY"][:3]
    hidden_gems = [s for s in scored if s["mc"] < 2 and s["score"] >= 55][:3]  # small caps

    print(f"Scored {len(scored)} fresh stocks | Strong Buy: {len(strong_buys)} | Buy: {len(buys)} | Hidden gems: {len(hidden_gems)}")

    def fmt_stock(s):
        reasons_str = " | ".join(s.get("reasons",[]))
        return (f"  {s['symbol']} ({s.get('Company Name',s['symbol'])[:22]}) "
                f"SCORE:{s['score']}/100 SIGNAL:{s['signal']}\n"
                f"    Price:${s['price']:.2f} | Mkt Cap:${s['mc']:.1f}B | Sector:{s['sector']} | Industry:{s['industry'][:25]}\n"
                f"    EPS Growth:{s['eps']:.0f}% | 52wk:{s['p52']:.0f}% | Quality:{s['quality']:.0f}/100 | Beta:{s['beta']:.2f}"
                + (f" | PEG:{s['peg']:.2f}" if s['peg'] else "") + "\n"
                f"    WHY: {reasons_str}")

    top_picks_str = "\n".join([fmt_stock(s) for s in (strong_buys + buys)[:6]])
    gems_str      = "\n".join([fmt_stock(s) for s in hidden_gems]) if hidden_gems else "  None this week"

    dip_str = "\n".join([
        f"  {d['symbol']} ({d['name'][:18]}) — down {abs(d['pnl_pct']):.1f}% | avg buy ${d['avg_buy']:.2f} | now ${d['current']:.2f} | quality {d['quality']:.0f}/100"
        for d in dip_opps
    ]) or "  None today"

    buys_str = "\n".join([f"  {b['symbol']} ${b['amount']:.2f} on {b['date']}" for b in buys_this_week]) or "  None"
    sp       = "\n".join([f"  {s.get('Fund Code','?')} ${s.get('Amount','?')}/wk" for s in sip_list])
    b_str    = "\n".join([f"  {s.get('Fund Code','?')} P&L:{s.get('P&L %','?')}%" for s in sorted(bought, key=lambda x: safe_float(x.get("P&L %",0)), reverse=True)[:12]])
    news_str = f"Market: {news.get('market_summary','')} | Sectors: {news.get('sectors_moving','')} | {' | '.join(news.get('top_stories',[]))}"

    budget_block = f"""BUDGET THIS WEEK: ${direct_budget}/week
Spent so far: ${spent_this_week:.2f} ({buys_str})
Remaining: ${budget_remaining:.2f}
{"⚠️ BUDGET EXHAUSTED — do NOT suggest new buys. Show what to prepare for next week instead." if budget_exhausted else f"✅ ${budget_remaining:.0f} available to invest today"}"""

    sip_block = ""
    if today_wed:
        sip_block = f"\nTODAY IS WEDNESDAY (SIP DAY):\n{sp}\nAlerts: {', '.join(sip_alerts) if sip_alerts else 'All healthy'}"
    elif sip_alerts:
        sip_block = f"\nSIP ALERT: {', '.join(sip_alerts)}"

    # Build buy section separately to avoid nested f-string issues
    if budget_exhausted:
        buy_section = (
            "NO BUYS THIS WEEK — BUDGET USED\n"
            f"Already spent ${spent_this_week:.0f} this week. "
            "List 2 stocks to research this weekend and buy next Monday."
        )
    else:
        buy_section = (
            f"BEST BUY TODAY (${budget_remaining:.0f} budget)\n"
            "Pick the #1 stock from the scored list. Choose STRONG BUY first, then BUY, then hidden gem.\n"
            "**TICKER** — Company name (what this company actually does in one line)\n"
            "- Why it could make money: explain the growth story simply\n"
            "- Score breakdown: mention the 2-3 biggest reasons it scored high\n"
            f"- How much to invest: $[amount within ${budget_remaining:.0f}] — buy now or wait for price dip to $X\n"
            "- One risk: what could go wrong in plain words\n\n"
            "HIDDEN GEM PICK (high risk, could 2-5x in 2-3 years)\n"
            "Pick the best small-cap from the hidden gems list above.\n"
            "**TICKER** — what they do\n"
            "- Why it could 2-5x: the big growth story\n"
            "- Score: [score]/100 — [2 key reasons]\n"
            "- How much: $[small amount — it's risky]"
        )

    budget_rule = "DO NOT suggest any buys — budget exhausted this week" if budget_exhausted else f"Suggest buys within ${budget_remaining:.0f} remaining budget"

    return f"""You are a sharp stock analyst helping a RISK-TAKING growth investor find stocks that can make them rich. Write in simple plain English — no jargon, like texting a smart friend. If you use a finance term explain it simply.

DATE: {today}
PORTFOLIO VALUE: ${portfolio_val:,.0f}

{budget_block}

TODAY'S MARKET: {news_str}

TOP SCORED STOCKS (pre-ranked using 30 data points each):
Scoring: Growth(40pts) + Quality(25pts) + Smart Money(20pts) + Hidden Gem(15pts) = 100pts max
{top_picks_str}

HIDDEN GEMS (small cap under $2B — most potential to 10x):
{gems_str}

DIP-BUY WATCH (already own, dropped more than 5% — might be good to add more):
{dip_str}

CURRENT HOLDINGS:
{b_str}
{sip_block}

RULES:
- Only recommend stocks from the scored list above
- {budget_rule}
- Can suggest adding more to a dip stock even if recently bought
- Risk taker — bold picks are welcome
- Explain WHY each stock could make money in simple words
- Use ** around ticker symbols to bold them

Write ONLY the brief below. No thinking out loud. Start directly:

WHAT'S HAPPENING TODAY
2 plain sentences — market situation and what it means for this portfolio.

{buy_section}

ONE THING TO WATCH
One news story that could move stocks this week. 2 simple sentences.

Under 320 words total."""

def get_brief(prompt):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key":os.environ["ANTHROPIC_API_KEY"],"anthropic-version":"2023-06-01","content-type":"application/json"},
        json={"model":ANTHROPIC_MODEL,"max_tokens":900,"messages":[{"role":"user","content":prompt}]},
        timeout=45
    )
    r.raise_for_status()
    text = "".join(b["text"] for b in r.json().get("content",[]) if b.get("type")=="text")
    return clean_brief(text)

def build_email(brief, data):
    today         = datetime.now().strftime("%A, %B %d, %Y")
    bought        = sorted(data.get("Bought",[]), key=lambda s: safe_float(s.get("P&L %",0)), reverse=True)
    history       = data.get("Buying History",[])
    portfolio_val = get_portfolio_value(bought)
    sip_budget, direct_budget = get_budget(data)
    spent_this_week, buys_this_week = get_weekly_direct_spend(data)
    budget_remaining = max(0, direct_budget - spent_this_week)
    budget_pct = min(100, (spent_this_week / direct_budget * 100)) if direct_budget > 0 else 0
    dip_opps   = get_dip_opportunities(data)

    # Budget bar color
    bar_color = "#00c96e" if budget_remaining > 50 else "#fbbf24" if budget_remaining > 0 else "#ff5252"
    bar_label = f"${budget_remaining:.0f} remaining" if budget_remaining > 0 else "BUDGET USED — no buys this week"

    # Holdings rows
    rows = ""
    for s in bought[:8]:
        sym   = s.get("Fund Code","?")
        name  = s.get("Fund Name","?")[:18]
        pnl   = safe_float(s.get("P&L %",0))
        col   = "#00c96e" if pnl >= 0 else "#ff5252"
        arrow = "▲" if pnl >= 0 else "▼"
        rows += f'<tr style="border-bottom:1px solid #0d150d"><td style="padding:8px 14px;font-weight:700;color:#fff;font-size:12px;font-family:\'Courier New\',monospace">{sym}</td><td style="padding:8px 14px;color:#6b7280;font-size:11px">{name}</td><td style="padding:8px 14px;text-align:right;font-weight:700;color:{col};font-size:12px">{arrow} {abs(pnl):.1f}%</td></tr>'

    # This week's buys
    week_rows = ""
    for b in buys_this_week:
        week_rows += f'<tr style="border-bottom:1px solid #0d1a0d"><td style="padding:7px 14px;font-weight:700;color:#fff;font-size:12px;font-family:\'Courier New\',monospace">{b["symbol"]}</td><td style="padding:7px 14px;text-align:right;color:#00c96e;font-size:11px">${b["amount"]:.2f}</td><td style="padding:7px 14px;text-align:right;color:#475569;font-size:11px">{b["date"]}</td></tr>'

    # Dip rows
    dip_rows = ""
    for d in dip_opps[:3]:
        dip_rows += f'<tr style="border-bottom:1px solid #1a0d0d"><td style="padding:7px 14px;font-weight:700;color:#fff;font-size:12px;font-family:\'Courier New\',monospace">{d["symbol"]}</td><td style="padding:7px 14px;color:#6b7280;font-size:11px">{d["name"][:16]}</td><td style="padding:7px 14px;text-align:right;font-weight:700;color:#ff5252;font-size:12px">▼ {abs(d["pnl_pct"]):.1f}%</td></tr>'

    def line_to_html(line):
        line = line.strip()
        if not line: return '<div style="height:5px"></div>'
        if line and line[0] in "☀🛒💎📅📰🚫":
            return f'<div style="margin:18px 0 7px;padding:8px 14px;background:#071407;border-left:3px solid #00c96e;border-radius:0 5px 5px 0"><span style="color:#00c96e;font-size:13px;font-weight:700;font-family:\'Courier New\',monospace">{line}</span></div>'
        if line.startswith("**"):
            fmt = re.sub(r'\*\*(.+?)\*\*', r'<strong style="color:#fff;font-size:14px">\1</strong>', line)
            return f'<p style="color:#e2e8f0;font-size:13px;margin:8px 0 3px">{fmt}</p>'
        if line.startswith("•"):
            labels = {"• why now:":("#60a5fa","WHY NOW"),"• news:":("#fbbf24","NEWS"),"• how much:":("#00c96e","HOW MUCH"),"• watch out for:":("#f87171","WATCH OUT")}
            for key,(col,label) in labels.items():
                if line.lower().startswith(key):
                    return f'<div style="margin:3px 0 3px 10px;padding:4px 10px;background:#080e08;border-radius:4px"><span style="color:{col};font-size:10px;font-weight:700;font-family:\'Courier New\',monospace;letter-spacing:1px">{label}: </span><span style="color:#cbd5e1;font-size:13px">{line[len(key):].strip()}</span></div>'
            return f'<p style="color:#94a3b8;font-size:13px;margin:3px 0 3px 14px;line-height:1.6">{line}</p>'
        return f'<p style="color:#cbd5e1;font-size:13px;line-height:1.8;margin:4px 0">{line}</p>'

    brief_html = "\n".join(line_to_html(l) for l in brief.split("\n"))
    wed_badge  = '<span style="background:#fbbf24;color:#000;font-size:9px;padding:2px 6px;border-radius:3px;margin-left:8px;font-family:\'Courier New\',monospace">SIP DAY</span>' if is_wednesday() else ''

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:16px 0;background:#030608;font-family:Georgia,serif">
<div style="max-width:600px;margin:0 auto;background:#05080d;border-radius:10px;overflow:hidden;box-shadow:0 0 30px rgba(0,201,110,0.07)">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#040d18,#041208);padding:20px 24px 16px;border-bottom:2px solid #00c96e">
    <table style="width:100%"><tr>
      <td>
        <div style="font-family:'Courier New',monospace;font-size:22px;font-weight:900;letter-spacing:5px;color:#00c96e">ALPHA<span style="color:#60a5fa">BRIEF</span>{wed_badge}</div>
        <div style="font-size:9px;color:#374151;letter-spacing:2px;margin-top:3px;font-family:'Courier New',monospace">{today.upper()}</div>
      </td>
      <td style="text-align:right;vertical-align:middle">
        <div style="font-size:9px;color:#374151;font-family:'Courier New',monospace">PORTFOLIO</div>
        <div style="font-size:18px;font-weight:700;color:#fbbf24;font-family:'Courier New',monospace">${portfolio_val:,.0f}</div>
      </td>
    </tr></table>
  </div>

  <!-- Weekly Budget Bar -->
  <div style="padding:12px 24px;background:#060a0f;border-bottom:1px solid #0d1520">
    <div style="display:flex;justify-content:space-between;margin-bottom:5px">
      <span style="font-size:9px;color:#374151;font-family:'Courier New',monospace;letter-spacing:1px">WEEKLY DIRECT BUY BUDGET</span>
      <span style="font-size:10px;color:{bar_color};font-family:'Courier New',monospace;font-weight:700">{bar_label}</span>
    </div>
    <div style="height:6px;background:#0d1520;border-radius:3px;overflow:hidden">
      <div style="height:100%;width:{budget_pct:.0f}%;background:{bar_color};border-radius:3px"></div>
    </div>
    <div style="display:flex;justify-content:space-between;margin-top:3px">
      <span style="font-size:9px;color:#374151;font-family:'Courier New',monospace">Spent: ${spent_this_week:.2f}</span>
      <span style="font-size:9px;color:#374151;font-family:'Courier New',monospace">Budget: ${direct_budget:.0f}/week</span>
    </div>
  </div>

  <!-- Brief -->
  <div style="padding:16px 24px 8px">{brief_html}</div>

  <div style="margin:4px 24px;height:1px;background:linear-gradient(90deg,transparent,#1a3a1a,transparent)"></div>

  <!-- This week's buys -->
  {f'''<div style="padding:14px 24px 0">
    <div style="font-size:9px;letter-spacing:2px;color:#374151;margin-bottom:7px;font-family:'Courier New',monospace">🛒 BOUGHT THIS WEEK</div>
    <table style="width:100%;border-collapse:collapse;background:#060e06;border:1px solid #0d1a0d;border-radius:6px;overflow:hidden">
      <tr style="background:#030a03"><th style="padding:6px 14px;text-align:left;font-size:9px;color:#374151;font-family:'Courier New',monospace">STOCK</th><th style="padding:6px 14px;text-align:right;font-size:9px;color:#374151;font-family:'Courier New',monospace">AMOUNT</th><th style="padding:6px 14px;text-align:right;font-size:9px;color:#374151;font-family:'Courier New',monospace">DATE</th></tr>
      {week_rows}
    </table>
  </div>''' if week_rows else ''}

  <!-- Dip opportunities -->
  {f'''<div style="padding:14px 24px 0">
    <div style="font-size:9px;letter-spacing:2px;color:#ff5252;margin-bottom:7px;font-family:'Courier New',monospace">🔴 DIP-BUY WATCH (ALREADY OWN, DOWN &gt;5%)</div>
    <table style="width:100%;border-collapse:collapse;background:#100606;border:1px solid #2a0808;border-radius:6px;overflow:hidden">
      <tr style="background:#0a0303"><th style="padding:6px 14px;text-align:left;font-size:9px;color:#5a2020;font-family:'Courier New',monospace">STOCK</th><th style="padding:6px 14px;text-align:left;font-size:9px;color:#5a2020;font-family:'Courier New',monospace">NAME</th><th style="padding:6px 14px;text-align:right;font-size:9px;color:#5a2020;font-family:'Courier New',monospace">DIP</th></tr>
      {dip_rows}
    </table>
  </div>''' if dip_rows else ''}

  <!-- Holdings -->
  <div style="padding:14px 24px">
    <div style="font-size:9px;letter-spacing:2px;color:#374151;margin-bottom:7px;font-family:'Courier New',monospace">📊 TOP 8 HOLDINGS</div>
    <table style="width:100%;border-collapse:collapse;background:#060c06;border:1px solid #0d150d;border-radius:6px;overflow:hidden">
      <tr style="background:#030803"><th style="padding:7px 14px;text-align:left;font-size:9px;color:#374151;font-family:'Courier New',monospace">STOCK</th><th style="padding:7px 14px;text-align:left;font-size:9px;color:#374151;font-family:'Courier New',monospace">NAME</th><th style="padding:7px 14px;text-align:right;font-size:9px;color:#374151;font-family:'Courier New',monospace">GAIN/LOSS</th></tr>
      {rows}
    </table>
  </div>

  <div style="padding:10px 24px;background:#030608;text-align:center;font-size:9px;color:#1f2937;font-family:'Courier New',monospace;letter-spacing:1px">
    AlphaBrief · Claude AI · {today} · Weekly budget resets every Monday
  </div>
</div></body></html>"""

def send_email(html, budget_remaining):
    user  = os.environ["GMAIL_USER"]
    pwd   = os.environ["GMAIL_APP_PASSWORD"]
    today = datetime.now().strftime("%b %d")
    budget_tag = "✅ Budget available" if budget_remaining > 0 else "🚫 Budget used"
    wed_tag    = "💰 SIP Day + " if is_wednesday() else ""
    subject    = f"☀️ AlphaBrief — {wed_tag}{budget_tag} — {today}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = user
    msg["To"]      = TO_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, TO_EMAIL, msg.as_string())
    print(f"Email sent: {subject}")

def main():
    print(f"AlphaBrief starting — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')} | {datetime.now().strftime('%A')}")
    data  = read_drive_sheet()
    spent, buys = get_weekly_direct_spend(data)
    sip_b, direct_b = get_budget(data)
    remaining = max(0, direct_b - spent)
    print(f"Budget: ${direct_b} | Spent: ${spent:.2f} | Remaining: ${remaining:.2f}")

    news   = get_market_news()
    prompt = build_prompt(data, news)
    brief  = get_brief(prompt)
    print("Preview:\n" + brief[:300])
    html   = build_email(brief, data)
    send_email(html, remaining)
    print("Done.")

if __name__ == "__main__":
    main()
