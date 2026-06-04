"""
AlphaBrief - Daily Stock Research Bot
Runs every morning at 7 AM CT via GitHub Actions
5 sheets: Budget, Watchlist, Bought, Buying History, SIP
"""

import os, json, smtplib, requests, re
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2 import service_account
from googleapiclient.discovery import build

GDRIVE_FILE_ID  = "18aobOtBNbYqhiuP1X13z8VRSwYRnWwVEzV7Ec6zhhtQ"
TO_EMAIL        = "ashishgoyal.ietc@gmail.com"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

def safe_float(v):
    try: return float(str(v).replace(",","").replace("%","").replace("--","0") or 0)
    except: return 0.0

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
    """Read weekly budget from Budget tab"""
    sip_budget    = 250  # defaults
    direct_budget = 100
    for row in data.get("Budget", []):
        label = str(row.get("NO_HEADER", row.get("", ""))).strip().lower()
        amt   = safe_float(row.get("NO_HEADER_1", row.get("250", 0)) or list(row.values())[1] if len(row) > 1 else 0)
        if "sip" in label:       sip_budget    = amt
        if "direct" in label:    direct_budget = amt
    return sip_budget, direct_budget

def get_owned_symbols(data):
    """All symbols across Bought + Buying History + SIP"""
    owned = set()
    for s in data.get("Bought",         []): owned.add(s.get("Fund Code", s.get("Symbol","")).strip().upper())
    for s in data.get("Buying History", []): owned.add(s.get("Fund Code", s.get("Symbol","")).strip().upper())
    for s in data.get("SIP",            []): owned.add(s.get("Fund Code", s.get("Symbol","")).strip().upper())
    owned.discard("")
    return owned

def is_wednesday():
    return datetime.now().weekday() == 2  # 0=Mon, 2=Wed

def get_sip_analysis(data):
    """
    SIP check — only flag when action is needed:
    - A SIP stock is down >10% from avg buy = consider pausing or adding extra
    - A SIP stock has a major negative news catalyst
    Returns list of actionable SIP alerts only
    """
    bought_map = {s.get("Fund Code","").upper(): s for s in data.get("Bought",[])}
    alerts = []
    for sip in data.get("SIP", []):
        sym  = sip.get("Fund Code","").upper()
        name = sip.get("Fund Name","")
        amt  = safe_float(sip.get("Amount", 0))
        h    = bought_map.get(sym)
        if h:
            pnl_pct = safe_float(h.get("P&L %", 0))
            if pnl_pct < -10:
                alerts.append({"symbol": sym, "name": name, "weekly_amt": amt,
                    "pnl_pct": pnl_pct, "alert": f"Down {abs(pnl_pct):.1f}% — consider adding extra this week"})
            elif pnl_pct < -5:
                alerts.append({"symbol": sym, "name": name, "weekly_amt": amt,
                    "pnl_pct": pnl_pct, "alert": f"Down {abs(pnl_pct):.1f}% — slight dip, watch this week"})
    return alerts

def get_dip_buy_opportunities(data):
    """
    Check already-owned stocks for dip buy opportunities.
    Rule: stock is down >5% from avg buy but fundamentals still strong (in watchlist with good score)
    This is the KEY new feature — can suggest SNDK again if it dips hard
    """
    bought_map   = {s.get("Fund Code","").upper(): s for s in data.get("Bought",[])}
    watchlist_map= {s.get("Symbol","").upper(): s   for s in data.get("Watchlist",[])}
    history_map  = {}
    for s in data.get("Buying History",[]):
        sym = s.get("Fund Code","").upper()
        history_map[sym] = s

    dips = []
    for sym, h in bought_map.items():
        pnl_pct     = safe_float(h.get("P&L %", 0))
        current_nav = safe_float(h.get("Current Nav", h.get("Current Amount",0)))
        avg_nav     = safe_float(h.get("Avg. Nav", 0))
        name        = h.get("Fund Name", sym)

        # Only flag if down >5% AND stock is in watchlist (still fundamentally good)
        if pnl_pct < -5 and sym in watchlist_map:
            w = watchlist_map[sym]
            eps_growth = safe_float(w.get("EPS Growth (Proj This Yr vs. Last Yr)", 0))
            quality    = safe_float(w.get("S&P Global Market Intelligence Quality", 0))
            ess        = w.get("Equity Summary Score (ESS) from LSEG StarMine","")

            # Only suggest if still high quality
            if quality >= 60 or "bullish" in ess.lower():
                last_buy = history_map.get(sym, {}).get("Avg. Nav", avg_nav)
                dips.append({
                    "symbol": sym, "name": name,
                    "pnl_pct": pnl_pct,
                    "current_price": current_nav,
                    "avg_buy": avg_nav,
                    "eps_growth": eps_growth,
                    "quality": quality,
                    "ess": ess,
                    "last_buy_price": last_buy,
                })

    # Sort by biggest dip first
    return sorted(dips, key=lambda x: x["pnl_pct"])

def get_market_news():
    today  = datetime.now().strftime("%B %d %Y")
    prompt = f"""Search stock market news for {today}. Return ONLY this JSON:
{{
  "market_summary": "one sentence what markets did today",
  "top_stories": ["story 1", "story 2", "story 3"],
  "sectors_moving": "which sectors up or down today"
}}"""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version":"2023-06-01", "content-type":"application/json"},
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
            return {"market_summary": block["text"][:200], "top_stories":[], "sectors_moving":""}
    return {"market_summary":"Markets open today.","top_stories":[],"sectors_moving":""}

def clean_brief(text):
    skip = ["let me search","let me look","i need to look","i'll search","now let me",
            "now i have","let me compile","searching for","looking up","i will search",
            "let me find","i'm going to","let me check","based on my search","i found that"]
    lines = [l for l in text.split("\n") if not any(p in l.lower() for p in skip) and l.strip() not in ["---","***","___"]]
    return re.sub(r'\n{3,}','\n\n',"\n".join(lines)).strip()

def build_prompt(data, news):
    today         = datetime.now().strftime("%A, %B %d, %Y")
    today_is_wed  = is_wednesday()
    bought        = data.get("Bought",[])
    sip_list      = data.get("SIP",[])
    history       = data.get("Buying History",[])
    watch         = sorted(data.get("Watchlist",[]), key=lambda s: safe_float(s.get("EPS Growth (Proj This Yr vs. Last Yr)",0)), reverse=True)
    owned         = get_owned_symbols(data)
    sip_budget, direct_budget = get_budget(data)
    portfolio_val = get_portfolio_value(bought)
    sip_alerts    = get_sip_analysis(data)
    dip_opps      = get_dip_buy_opportunities(data)

    # Fresh watchlist — not owned yet
    fresh_watch = [s for s in watch if s.get("Symbol","").upper() not in owned][:15]

    # Format sections
    w = "\n".join([
        f"  {s.get('Symbol','?')} | {s.get('Company Name','?')[:25]} | "
        f"Earnings growth:{s.get('EPS Growth (Proj This Yr vs. Last Yr)','?')}% | "
        f"Quality:{s.get('S&P Global Market Intelligence Quality','?')}/100 | "
        f"Inst.buying:{s.get('Institutional Ownership (Last vs. Prior Qtr)','?')}% | "
        f"Analyst trend:{s.get('Equity Summary Score Change (1 Month)','?')}"
        for s in fresh_watch
    ])

    b = "\n".join([
        f"  {s.get('Fund Code','?')} | P&L:{s.get('P&L %','?')}% | Value:${s.get('Current Amount','?')}"
        for s in bought[:20]
    ])

    h = "\n".join([
        f"  {s.get('Fund Code','?')} bought at ${s.get('Avg. Nav','?')} on {s.get('Date','?')}"
        for s in history
    ]) or "  None yet"

    sp = "\n".join([f"  {s.get('Fund Code','?')} — ${s.get('Amount','?')}/week" for s in sip_list])

    dip_str = "\n".join([
        f"  {d['symbol']} ({d['name'][:20]}) — down {abs(d['pnl_pct']):.1f}% | "
        f"bought at ${d['avg_buy']} | now ${d['current_price']:.2f} | "
        f"quality:{d['quality']} | EPS growth:{d['eps_growth']:.0f}%"
        for d in dip_opps
    ]) or "  None today"

    sip_alert_str = "\n".join([
        f"  {a['symbol']} — {a['alert']} (currently ${a['weekly_amt']}/week)"
        for a in sip_alerts
    ]) or "  All SIPs healthy — no action needed"

    news_str = f"Market today: {news.get('market_summary','')}\nStories: {' | '.join(news.get('top_stories',[]))}\nSectors: {news.get('sectors_moving','')}"

    # SIP section instructions — only show on Wednesday OR if alerts exist
    sip_instructions = ""
    if today_is_wed:
        sip_instructions = f"""
📅 SIP WEDNESDAY CHECK
Today is Wednesday — SIP execution day. Check each SIP stock:
- Any that dropped a lot this week = good chance to add a little extra beyond the usual amount
- Any with bad news = mention it
- Otherwise just confirm all SIPs are good to go as planned
SIP budget this week: ${sip_budget}
SIP alerts: {sip_alert_str}"""
    elif sip_alerts:
        sip_instructions = f"""
📅 SIP ALERT (action needed before Wednesday)
Some SIP stocks need attention before Wednesday's execution:
{sip_alert_str}"""

    return f"""You are helping a regular investor with their morning stock update. Simple plain English, like texting a friend. No jargon.

Today: {today}
Portfolio value: ${portfolio_val:,.0f}
Weekly budget: SIP=${sip_budget} (runs Wednesday) | Direct buy=${direct_budget}

TODAY'S MARKET NEWS:
{news_str}

NEW STOCKS TO BUY (not owned yet — from watchlist):
{w}

DIP-BUY OPPORTUNITIES (already own these but they've dropped — could add more):
{dip_str}

CURRENT HOLDINGS:
{b}

RECENTLY BOUGHT:
{h}

SIP (auto-buys every Wednesday):
{sp}

RULES:
- Weekly direct buy budget is ${direct_budget} — suggest how to use it
- Do NOT suggest selling anything this week
- Risk taker who likes high growth
- For dip-buys: ONLY suggest adding more to an already-owned stock if it dropped >5% AND still has strong fundamentals — this is fine even if recently bought

Write ONLY the brief, no thinking out loud:

☀️ WHAT'S HAPPENING TODAY
2 simple sentences. Market situation and what it means for their portfolio.

🛒 BEST STOCK TO BUY TODAY
Pick the single best opportunity — either a fresh stock from watchlist OR a dip-buy on something they already own (whichever is better today).
**TICKER** — Company name (what they do)
• Why today: simple reason
• News: latest relevant news
• How much: suggest from ${direct_budget} budget — buy now or wait for $X
• Watch out for: one risk

💎 ONE HIGH RISK BIG BET
One stock with 2-3x potential. 3 sentences. How much from the ${direct_budget} budget.
{sip_instructions}

📰 ONE THING TO WATCH
One news story affecting their stocks. 2 plain sentences.

Under 300 words total."""

def get_brief(prompt):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key":os.environ["ANTHROPIC_API_KEY"],"anthropic-version":"2023-06-01","content-type":"application/json"},
        json={"model":ANTHROPIC_MODEL,"max_tokens":1000,"messages":[{"role":"user","content":prompt}]},
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
    dip_opps      = get_dip_buy_opportunities(data)

    # Holdings rows
    rows = ""
    for s in bought[:8]:
        sym   = s.get("Fund Code","?")
        name  = s.get("Fund Name","?")[:18]
        pnl   = safe_float(s.get("P&L %",0))
        col   = "#00c96e" if pnl >= 0 else "#ff5252"
        arrow = "▲" if pnl >= 0 else "▼"
        rows += f'<tr style="border-bottom:1px solid #0d150d"><td style="padding:9px 14px;font-weight:700;color:#fff;font-size:12px;font-family:\'Courier New\',monospace">{sym}</td><td style="padding:9px 14px;color:#6b7280;font-size:11px">{name}</td><td style="padding:9px 14px;text-align:right;font-weight:700;color:{col};font-size:12px">{arrow} {abs(pnl):.1f}%</td></tr>'

    # Buying history rows
    hist_rows = ""
    for s in history[-6:]:
        sym   = s.get("Fund Code","?")
        price = s.get("Avg. Nav","?")
        date  = s.get("Date","?")
        hist_rows += f'<tr style="border-bottom:1px solid #0d150d"><td style="padding:7px 14px;font-weight:700;color:#fff;font-size:12px;font-family:\'Courier New\',monospace">{sym}</td><td style="padding:7px 14px;text-align:right;color:#fbbf24;font-size:11px">${price}</td><td style="padding:7px 14px;text-align:right;color:#475569;font-size:11px">{date}</td></tr>'

    # Dip opportunity rows
    dip_rows = ""
    for d in dip_opps[:4]:
        dip_rows += f'<tr style="border-bottom:1px solid #1a0d0d"><td style="padding:7px 14px;font-weight:700;color:#fff;font-size:12px;font-family:\'Courier New\',monospace">{d["symbol"]}</td><td style="padding:7px 14px;color:#6b7280;font-size:11px">{d["name"][:18]}</td><td style="padding:7px 14px;text-align:right;font-weight:700;color:#ff5252;font-size:12px">▼ {abs(d["pnl_pct"]):.1f}%</td><td style="padding:7px 14px;text-align:right;color:#94a3b8;font-size:11px">${d["current_price"]:.0f}</td></tr>'

    def line_to_html(line):
        line = line.strip()
        if not line: return '<div style="height:5px"></div>'
        if line and line[0] in "☀🛒💎📅📰":
            return f'<div style="margin:20px 0 8px;padding:9px 14px;background:#071407;border-left:3px solid #00c96e;border-radius:0 5px 5px 0"><span style="color:#00c96e;font-size:13px;font-weight:700;font-family:\'Courier New\',monospace">{line}</span></div>'
        if line.startswith("**"):
            fmt = re.sub(r'\*\*(.+?)\*\*', r'<strong style="color:#fff;font-size:14px">\1</strong>', line)
            return f'<p style="color:#e2e8f0;font-size:13px;margin:10px 0 3px">{fmt}</p>'
        if line.startswith("•"):
            labels = {"• why today:":("#60a5fa","WHY TODAY"),"• news:":("#fbbf24","NEWS"),"• how much:":("#00c96e","HOW MUCH"),"• watch out for:":("#f87171","WATCH OUT")}
            for key,(col,label) in labels.items():
                if line.lower().startswith(key):
                    return f'<div style="margin:4px 0 4px 10px;padding:5px 10px;background:#080e08;border-radius:4px"><span style="color:{col};font-size:10px;font-weight:700;font-family:\'Courier New\',monospace;letter-spacing:1px">{label}: </span><span style="color:#cbd5e1;font-size:13px">{line[len(key):].strip()}</span></div>'
            return f'<p style="color:#94a3b8;font-size:13px;margin:4px 0 4px 14px;line-height:1.6">{line}</p>'
        return f'<p style="color:#cbd5e1;font-size:13px;line-height:1.8;margin:4px 0">{line}</p>'

    brief_html = "\n".join(line_to_html(l) for l in brief.split("\n"))
    wed_badge  = '<span style="background:#fbbf24;color:#000;font-size:9px;padding:2px 6px;border-radius:3px;font-family:\'Courier New\',monospace;margin-left:8px">SIP DAY</span>' if is_wednesday() else ''

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:16px 0;background:#030608;font-family:Georgia,serif">
<div style="max-width:600px;margin:0 auto;background:#05080d;border-radius:10px;overflow:hidden;box-shadow:0 0 30px rgba(0,201,110,0.07)">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#040d18,#041208);padding:22px 24px 18px;border-bottom:2px solid #00c96e">
    <table style="width:100%"><tr>
      <td>
        <div style="font-family:'Courier New',monospace;font-size:24px;font-weight:900;letter-spacing:5px;color:#00c96e">ALPHA<span style="color:#60a5fa">BRIEF</span>{wed_badge}</div>
        <div style="font-size:9px;color:#374151;letter-spacing:2px;margin-top:4px;font-family:'Courier New',monospace">{today.upper()}</div>
      </td>
      <td style="text-align:right;vertical-align:middle">
        <div style="font-size:9px;color:#374151;font-family:'Courier New',monospace">PORTFOLIO</div>
        <div style="font-size:20px;font-weight:700;color:#fbbf24;font-family:'Courier New',monospace">${portfolio_val:,.0f}</div>
        <div style="font-size:9px;color:#374151;font-family:'Courier New',monospace;margin-top:2px">BUDGET: SIP ${sip_budget} · BUY ${direct_budget}</div>
      </td>
    </tr></table>
  </div>

  <!-- Brief -->
  <div style="padding:16px 24px 8px">{brief_html}</div>

  <div style="margin:4px 24px;height:1px;background:linear-gradient(90deg,transparent,#1a3a1a,transparent)"></div>

  <!-- Dip opportunities -->
  {f'''<div style="padding:16px 24px 0">
    <div style="font-size:9px;letter-spacing:2px;color:#ff5252;margin-bottom:8px;font-family:'Courier New',monospace">🔴 DIP-BUY OPPORTUNITIES (ALREADY OWN — DOWN &gt;5%)</div>
    <table style="width:100%;border-collapse:collapse;background:#100606;border:1px solid #2a0808;border-radius:6px;overflow:hidden">
      <tr style="background:#0a0303"><th style="padding:7px 14px;text-align:left;font-size:9px;color:#5a2020;font-family:'Courier New',monospace">STOCK</th><th style="padding:7px 14px;text-align:left;font-size:9px;color:#5a2020;font-family:'Courier New',monospace">NAME</th><th style="padding:7px 14px;text-align:right;font-size:9px;color:#5a2020;font-family:'Courier New',monospace">DIP</th><th style="padding:7px 14px;text-align:right;font-size:9px;color:#5a2020;font-family:'Courier New',monospace">PRICE</th></tr>
      {dip_rows}
    </table>
  </div>''' if dip_rows else ''}

  <!-- Holdings -->
  <div style="padding:16px 24px">
    <div style="font-size:9px;letter-spacing:2px;color:#374151;margin-bottom:8px;font-family:'Courier New',monospace">📊 TOP 8 HOLDINGS</div>
    <table style="width:100%;border-collapse:collapse;background:#060c06;border:1px solid #0d150d;border-radius:6px;overflow:hidden">
      <tr style="background:#030803"><th style="padding:7px 14px;text-align:left;font-size:9px;color:#374151;font-family:'Courier New',monospace">STOCK</th><th style="padding:7px 14px;text-align:left;font-size:9px;color:#374151;font-family:'Courier New',monospace">NAME</th><th style="padding:7px 14px;text-align:right;font-size:9px;color:#374151;font-family:'Courier New',monospace">GAIN/LOSS</th></tr>
      {rows}
    </table>
  </div>

  <!-- Buying History -->
  {f'''<div style="padding:0 24px 16px">
    <div style="font-size:9px;letter-spacing:2px;color:#374151;margin-bottom:8px;font-family:'Courier New',monospace">🛒 RECENTLY BOUGHT</div>
    <table style="width:100%;border-collapse:collapse;background:#060c06;border:1px solid #0d150d;border-radius:6px;overflow:hidden">
      <tr style="background:#030803"><th style="padding:7px 14px;text-align:left;font-size:9px;color:#374151;font-family:'Courier New',monospace">STOCK</th><th style="padding:7px 14px;text-align:right;font-size:9px;color:#374151;font-family:'Courier New',monospace">BUY PRICE</th><th style="padding:7px 14px;text-align:right;font-size:9px;color:#374151;font-family:'Courier New',monospace">DATE</th></tr>
      {hist_rows}
    </table>
  </div>''' if hist_rows else ''}

  <div style="padding:10px 24px;background:#030608;text-align:center;font-size:9px;color:#1f2937;font-family:'Courier New',monospace;letter-spacing:1px">
    AlphaBrief · Claude AI · 5-Sheet Google Drive Tracker · {today}
  </div>
</div></body></html>"""

def send_email(html):
    user  = os.environ["GMAIL_USER"]
    pwd   = os.environ["GMAIL_APP_PASSWORD"]
    today = datetime.now().strftime("%b %d")
    is_wed = is_wednesday()
    subject = f"☀️ AlphaBrief — {'💰 SIP Day + ' if is_wed else ''}Morning Update {today}"
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
    print(f"AlphaBrief starting — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Day: {datetime.now().strftime('%A')} | Wednesday (SIP day): {is_wednesday()}")
    data   = read_drive_sheet()
    print("Fetching market news...")
    news   = get_market_news()
    print(f"News: {news.get('market_summary','')[:80]}")
    print("Generating brief...")
    prompt = build_prompt(data, news)
    brief  = get_brief(prompt)
    print("Preview:\n" + brief[:300])
    html   = build_email(brief, data)
    send_email(html)
    print("Done.")

if __name__ == "__main__":
    main()
