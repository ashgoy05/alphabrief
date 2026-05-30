"""
AlphaBrief - Daily Stock Research Bot
Runs every morning at 7 AM CT via GitHub Actions
Cost optimized - single focused web search instead of multiple
"""

import os, json, smtplib, requests, re
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2 import service_account
from googleapiclient.discovery import build

GDRIVE_FILE_ID  = "18aobOtBNbYqhiuP1X13z8VRSwYRnWwVEzV7Ec6zhhtQ"
TO_EMAIL        = "ashishgoyal.ietc@gmail.com"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # Much cheaper, still great quality

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
    for sheet in ["Watchlist", "Bought", "SIP"]:
        rows = svc.spreadsheets().values().get(
            spreadsheetId=GDRIVE_FILE_ID, range=f"{sheet}!A1:Z200"
        ).execute().get("values", [])
        if rows:
            h = rows[0]
            data[sheet] = [dict(zip(h, r + [""]*(len(h)-len(r)))) for r in rows[1:] if any(c.strip() for c in r)]
        else:
            data[sheet] = []
    print(f"Drive read: Watchlist={len(data['Watchlist'])}, Bought={len(data['Bought'])}, SIP={len(data['SIP'])}")
    return data

def get_portfolio_value(bought):
    return sum(safe_float(s.get("Current Amount", s.get("currentPrice", 0))) for s in bought)

def get_market_news():
    """
    Step 1 — ONE single web search call to get today's market news.
    We do this separately so the main brief call has NO web search tools
    (tools are the expensive part).
    """
    today = datetime.now().strftime("%B %d %Y")
    prompt = f"""Search for today's stock market news ({today}) and return a short JSON summary.
Search for: "stock market news today {today}"

Return ONLY this JSON, nothing else:
{{
  "market_summary": "one sentence on what markets did today",
  "top_stories": ["story 1 in one sentence", "story 2 in one sentence", "story 3 in one sentence"],
  "sectors_moving": "which sectors are up or down today in one sentence"
}}"""

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 400,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=45
    )
    r.raise_for_status()
    data = r.json()

    # Extract text from response
    for block in data.get("content", []):
        if block.get("type") == "text":
            text = block["text"].strip()
            # Try to parse JSON
            try:
                # Find JSON in the text
                match = re.search(r'\{.*\}', text, re.DOTALL)
                if match:
                    return json.loads(match.group())
            except:
                pass
            # If JSON parsing fails, return raw text
            return {"market_summary": text, "top_stories": [], "sectors_moving": ""}

    return {"market_summary": "Markets open today.", "top_stories": [], "sectors_moving": ""}

def clean_brief(text):
    """Remove any leaked thinking lines"""
    skip_phrases = [
        "let me search", "let me look", "i need to look", "i'll search",
        "now let me", "now i have", "let me compile", "let me get",
        "searching for", "looking up", "i will search", "let me find",
        "i'm going to", "let me check", "based on my search", "i found that"
    ]
    lines = []
    for line in text.split("\n"):
        if not any(p in line.strip().lower() for p in skip_phrases):
            if line.strip() not in ["---", "***", "___"]:
                lines.append(line)
    return re.sub(r'\n{3,}', '\n\n', "\n".join(lines)).strip()

def build_prompt(data, news):
    watch  = sorted(data.get("Watchlist",[]), key=lambda s: safe_float(s.get("EPS Growth (Proj This Yr vs. Last Yr)",0)), reverse=True)[:12]
    bought = data.get("Bought",[])
    sip    = data.get("SIP",[])
    today  = datetime.now().strftime("%A, %B %d, %Y")

    portfolio_value = get_portfolio_value(bought)
    suggested_min   = max(100, round(portfolio_value * 0.01, -1))
    suggested_max   = max(250, round(portfolio_value * 0.03, -1))

    w  = "\n".join([
        f"  {s.get('Symbol','?')} | {s.get('Company Name','?')[:25]} | "
        f"Earnings growth:{s.get('EPS Growth (Proj This Yr vs. Last Yr)','?')}% | "
        f"Quality:{s.get('S&P Global Market Intelligence Quality','?')}/100 | "
        f"Big investors buying:{s.get('Institutional Ownership (Last vs. Prior Qtr)','?')}% more | "
        f"Analyst trend:{s.get('Equity Summary Score Change (1 Month)','?')}"
        for s in watch
    ])
    b  = "\n".join([
        f"  {s.get('Fund Code',s.get('Symbol','?'))} | "
        f"{s.get('Fund Name',s.get('Name','?'))[:20]} | "
        f"Gain/Loss:{s.get('P&L %',s.get('pnlPct','?'))}%"
        for s in bought[:18]
    ])
    sp = "\n".join([
        f"  {s.get('Fund Code',s.get('Symbol','?'))} — "
        f"${s.get('Amount',s.get('weeklyAmt','?'))}/week"
        for s in sip
    ])

    # Format today's news for the prompt
    news_str = f"""Today's market: {news.get('market_summary','')}
Top stories: {' | '.join(news.get('top_stories',[]))}
Sectors: {news.get('sectors_moving','')}"""

    return f"""You are helping a regular investor with their morning stock update. Write in simple plain English like texting a smart friend. No Wall Street jargon.

Today: {today}
Portfolio value: ${portfolio_value:,.0f}
Good amount per new stock: ${suggested_min:,.0f}–${suggested_max:,.0f}

TODAY'S MARKET NEWS (already fetched):
{news_str}

WATCHLIST STOCKS:
{w}

STOCKS ALREADY OWNED:
{b}

WEEKLY AUTO-BUY:
{sp}

RULES:
- Do NOT suggest selling (investor already trimmed this week)
- Risk taker who likes high growth
- Use the market news above — do not search again
- Explain any finance word in simple brackets right after

Write ONLY the brief. No thinking out loud. Start directly with the first section:

☀️ WHAT'S HAPPENING TODAY
2 sentences. What markets are doing today and what it means for their portfolio.

🛒 TOP 3 STOCKS TO BUY
**TICKER** — Company Name (what this company does in plain words)
• Why now: simple reason this stock looks good today
• News: one relevant news item from today's market news above
• How much: $[{suggested_min:,.0f}–{suggested_max:,.0f}] — buy today or wait for $X
• Watch out for: one simple risk in plain words

💎 ONE BIG BET (high risk, could 2–3x)
**TICKER** — what they do, why it could grow a lot, how much to invest. 3 sentences.

📅 AUTO-BUY CHECK
Any weekly auto-buy stocks down this week worth adding extra? One line answer.

📰 ONE THING TO WATCH
One news story from today that matters for their stocks. 2 plain English sentences.

Keep it under 320 words total."""

def get_brief(prompt):
    """Main brief call — NO web search tools = much cheaper"""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=45
    )
    r.raise_for_status()
    data = r.json()
    text = "".join(b["text"] for b in data.get("content",[]) if b.get("type")=="text")
    return clean_brief(text)

def build_email(brief, data):
    today           = datetime.now().strftime("%A, %B %d, %Y")
    bought          = sorted(data.get("Bought",[]), key=lambda s: safe_float(s.get("P&L %",s.get("pnlPct",0))), reverse=True)
    portfolio_value = get_portfolio_value(bought)

    rows = ""
    for s in bought[:8]:
        sym   = s.get("Fund Code", s.get("Symbol","?"))
        name  = s.get("Fund Name",  s.get("Name","?"))[:20]
        pnl   = safe_float(s.get("P&L %", s.get("pnlPct",0)))
        col   = "#00c96e" if pnl >= 0 else "#ff5252"
        arrow = "▲" if pnl >= 0 else "▼"
        rows += f"""<tr style="border-bottom:1px solid #0d150d">
          <td style="padding:10px 16px;font-weight:700;color:#fff;font-size:13px;font-family:'Courier New',monospace">{sym}</td>
          <td style="padding:10px 16px;color:#6b7280;font-size:12px">{name}</td>
          <td style="padding:10px 16px;text-align:right;font-weight:700;color:{col};font-size:13px">{arrow} {abs(pnl):.1f}%</td>
        </tr>"""

    def line_to_html(line):
        line = line.strip()
        if not line:
            return '<div style="height:5px"></div>'
        if line and line[0] in "☀🛒💎📅📰":
            return f'<div style="margin:22px 0 8px;padding:9px 14px;background:#071407;border-left:3px solid #00c96e;border-radius:0 5px 5px 0"><span style="color:#00c96e;font-size:13px;font-weight:700;font-family:\'Courier New\',monospace">{line}</span></div>'
        if line.startswith("**"):
            fmt = re.sub(r'\*\*(.+?)\*\*', r'<strong style="color:#fff;font-size:14px">\1</strong>', line)
            return f'<p style="color:#e2e8f0;font-size:13px;margin:10px 0 3px">{fmt}</p>'
        if line.startswith("•"):
            labels = {
                "• why now:":      ("#60a5fa","WHY NOW"),
                "• news:":         ("#fbbf24","NEWS"),
                "• how much:":     ("#00c96e","HOW MUCH"),
                "• watch out for:":("#f87171","WATCH OUT"),
            }
            for key,(col,label) in labels.items():
                if line.lower().startswith(key):
                    content = line[len(key):].strip()
                    return f'<div style="margin:4px 0 4px 10px;padding:5px 10px;background:#080e08;border-radius:4px"><span style="color:{col};font-size:10px;font-weight:700;font-family:\'Courier New\',monospace;letter-spacing:1px">{label}: </span><span style="color:#cbd5e1;font-size:13px">{content}</span></div>'
            return f'<p style="color:#94a3b8;font-size:13px;margin:4px 0 4px 14px;line-height:1.6">{line}</p>'
        return f'<p style="color:#cbd5e1;font-size:13px;line-height:1.8;margin:4px 0">{line}</p>'

    brief_html = "\n".join(line_to_html(l) for l in brief.split("\n"))

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:16px 0;background:#030608;font-family:Georgia,serif">
<div style="max-width:600px;margin:0 auto;background:#05080d;border-radius:10px;overflow:hidden;box-shadow:0 0 30px rgba(0,201,110,0.07)">

  <div style="background:linear-gradient(135deg,#040d18,#041208);padding:24px 26px 20px;border-bottom:2px solid #00c96e">
    <table style="width:100%"><tr>
      <td><div style="font-family:'Courier New',monospace;font-size:26px;font-weight:900;letter-spacing:5px;color:#00c96e">ALPHA<span style="color:#60a5fa">BRIEF</span></div>
        <div style="font-size:9px;color:#374151;letter-spacing:2px;margin-top:4px;font-family:'Courier New',monospace">{today.upper()}</div></td>
      <td style="text-align:right;vertical-align:middle">
        <div style="font-size:10px;color:#374151;font-family:'Courier New',monospace">PORTFOLIO</div>
        <div style="font-size:20px;font-weight:700;color:#fbbf24;font-family:'Courier New',monospace">${portfolio_value:,.0f}</div>
      </td>
    </tr></table>
  </div>

  <div style="padding:18px 26px 8px">{brief_html}</div>

  <div style="margin:4px 26px;height:1px;background:linear-gradient(90deg,transparent,#1a3a1a,transparent)"></div>

  <div style="padding:18px 26px">
    <div style="font-size:9px;letter-spacing:2px;color:#374151;margin-bottom:10px;font-family:'Courier New',monospace">📊 YOUR TOP 8 HOLDINGS</div>
    <table style="width:100%;border-collapse:collapse;background:#060c06;border:1px solid #0d150d;border-radius:6px;overflow:hidden">
      <tr style="background:#030803">
        <th style="padding:8px 16px;text-align:left;font-size:9px;color:#374151;font-family:'Courier New',monospace;letter-spacing:1px">STOCK</th>
        <th style="padding:8px 16px;text-align:left;font-size:9px;color:#374151;font-family:'Courier New',monospace;letter-spacing:1px">NAME</th>
        <th style="padding:8px 16px;text-align:right;font-size:9px;color:#374151;font-family:'Courier New',monospace;letter-spacing:1px">GAIN/LOSS</th>
      </tr>{rows}
    </table>
  </div>

  <div style="padding:10px 26px;background:#030608;text-align:center;font-size:9px;color:#1f2937;font-family:'Courier New',monospace;letter-spacing:1px">
    AlphaBrief · Claude AI · Your Google Sheet · {today}
  </div>
</div>
</body></html>"""

def send_email(html):
    user = os.environ["GMAIL_USER"]
    pwd  = os.environ["GMAIL_APP_PASSWORD"]
    today = datetime.now().strftime("%b %d")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"☀️ AlphaBrief — Morning Stock Update {today}"
    msg["From"] = user
    msg["To"]   = TO_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, TO_EMAIL, msg.as_string())
    print(f"Email sent to {TO_EMAIL}")

def main():
    print(f"AlphaBrief starting — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    data  = read_drive_sheet()

    print("Fetching market news (1 search call)...")
    news  = get_market_news()
    print(f"News: {news.get('market_summary','')[:80]}")

    print("Generating brief (no search = cheap)...")
    prompt = build_prompt(data, news)
    brief  = get_brief(prompt)
    print("Brief preview:\n" + brief[:300])

    html = build_email(brief, data)
    send_email(html)
    print("Done.")

if __name__ == "__main__":
    main()
