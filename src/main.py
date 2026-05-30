"""
AlphaBrief - Daily Stock Research Bot
Runs every morning at 7 AM CT via GitHub Actions
Reads Daily Tracker from Google Drive -> AI research + web search -> Email
"""

import os, json, smtplib, requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2 import service_account
from googleapiclient.discovery import build

GDRIVE_FILE_ID  = "18aobOtBNbYqhiuP1X13z8VRSwYRnWwVEzV7Ec6zhhtQ"
TO_EMAIL        = "ashishgoyal.ietc@gmail.com"
ANTHROPIC_MODEL = "claude-opus-4-6"

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
    """Calculate total current portfolio value"""
    total = 0
    for s in bought:
        val = safe_float(s.get("Current Amount", s.get("currentPrice", 0)))
        total += val
    return total

def build_prompt(data):
    watch  = sorted(data.get("Watchlist",[]), key=lambda s: safe_float(s.get("EPS Growth (Proj This Yr vs. Last Yr)",0)), reverse=True)[:15]
    bought = data.get("Bought",[])
    sip    = data.get("SIP",[])
    today  = datetime.now().strftime("%A, %B %d, %Y")

    # Portfolio value for position sizing
    portfolio_value = get_portfolio_value(bought)
    # Suggest investing 1-3% of portfolio per new position
    suggested_min = round(portfolio_value * 0.01, -1)  # 1%
    suggested_max = round(portfolio_value * 0.03, -1)  # 3%

    w  = "\n".join([f"  {s.get('Symbol','?')} | {s.get('Company Name','?')[:28]} | EPS Growth:{s.get('EPS Growth (Proj This Yr vs. Last Yr)','?')}% | Quality:{s.get('S&P Global Market Intelligence Quality','?')}/100 | Analyst Score:{s.get('Equity Summary Score (ESS) from LSEG StarMine','?')} | Trend:{s.get('Equity Summary Score Change (1 Month)','?')} | Big investors buying:{s.get('Institutional Ownership (Last vs. Prior Qtr)','?')}% more" for s in watch])
    b  = "\n".join([f"  {s.get('Fund Code',s.get('Symbol','?'))} | {s.get('Fund Name',s.get('Name','?'))[:22]} | Gain/Loss:{s.get('P&L %',s.get('pnlPct','?'))}% | Current value:${s.get('Current Amount',s.get('currentPrice','?'))}" for s in bought[:20]])
    sp = "\n".join([f"  {s.get('Fund Code',s.get('Symbol','?'))} — ${s.get('Amount',s.get('weeklyAmt','?'))} every week" for s in sip])

    return f"""You are helping a regular investor understand what stocks to buy today. Write in very simple, plain English — like explaining to a friend over coffee. No Wall Street jargon. No fancy terms. If you use a financial word, explain it in simple words right after.

Today: {today}
Total portfolio value: ${portfolio_value:,.0f}
Good amount to invest per new stock: ${suggested_min:,.0f} to ${suggested_max:,.0f} (1-3% of portfolio)

WATCHLIST STOCKS (from their screener, top 15 by earnings growth):
{w}

STOCKS THEY ALREADY OWN:
{b}

WEEKLY AUTO-BUY (SIP) LIST:
{sp}

IMPORTANT RULES:
- Investor recently trimmed their portfolio — do NOT suggest selling anything this week
- They are a risk taker who likes high growth stocks
- Use web search to find the latest news for each stock you recommend
- Suggest how much to invest based on portfolio size (${suggested_min:,.0f}–${suggested_max:,.0f} per stock)

Write the morning brief with this EXACT structure. Use simple words a non-expert can understand:

☀️ WHAT'S HAPPENING TODAY
In 2 simple sentences — what is the stock market doing today and why does it matter for their portfolio? No jargon.

🛒 TOP 3 STOCKS TO BUY TODAY
For each stock write:
**TICKER** — Company Name (what this company actually does in one line)
• Why buy it now: Explain in simple words what good thing is happening with this company
• Latest news: What happened recently with this stock (use web search)
• How much to invest: $[amount from ${suggested_min:,.0f}–${suggested_max:,.0f}] — buy today OR wait until price drops to $X
• One risk to know: In plain words, what could go wrong

💎 ONE BIG BET (higher risk, higher reward)
Pick one stock that could grow 2x or 3x. Explain in simple words:
- What does this company do
- Why could it grow a lot
- What is the latest news about it (use web search)
- How much to invest

📅 WEEKLY AUTO-BUY CHECK
Look at their SIP list. Is any of those stocks cheaper than usual this week? Should they buy a little extra? Simple yes/no with one reason.

📰 ONE NEWS STORY TO KNOW
One thing happening in the world today that could affect their stocks. Explain why it matters in plain English.

Keep total length under 350 words. Write like you are texting a smart friend, not writing a Wall Street report."""

def get_brief(prompt):
    """Call Claude API with web search enabled for latest news"""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 1500,
            "tools": [
                {
                    "type": "web_search_20250305",
                    "name": "web_search"
                }
            ],
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=90  # longer timeout for web search
    )
    r.raise_for_status()
    data = r.json()

    # Extract all text blocks (web search may add multiple content blocks)
    text_parts = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block["text"])

    return "\n".join(text_parts) if text_parts else "Could not generate brief today."

def build_email(brief, data):
    today  = datetime.now().strftime("%A, %B %d, %Y")
    bought = sorted(data.get("Bought",[]), key=lambda s: safe_float(s.get("P&L %",s.get("pnlPct",0))), reverse=True)
    portfolio_value = get_portfolio_value(bought)

    rows = ""
    for s in bought[:8]:
        sym  = s.get("Fund Code", s.get("Symbol","?"))
        name = s.get("Fund Name",  s.get("Name","?"))[:22]
        pnl  = safe_float(s.get("P&L %", s.get("pnlPct",0)))
        col  = "#00ff87" if pnl >= 0 else "#ff4d4d"
        rows += f'<tr><td style="padding:8px 14px;font-weight:700;color:#e2e8f0;font-size:13px">{sym}</td><td style="padding:8px 14px;color:#64748b;font-size:12px">{name}</td><td style="padding:8px 14px;text-align:right;font-weight:700;color:{col};font-size:13px">{("+" if pnl>=0 else "")}{pnl:.1f}%</td></tr>'

    brief_html = ""
    for line in brief.split("\n"):
        line = line.strip()
        if not line:
            brief_html += "<div style='height:8px'></div>"
        elif line[0] in "☀🛒💎📅📰":
            brief_html += f'<p style="color:#00ff87;font-size:14px;font-weight:700;margin:20px 0 8px;border-bottom:1px solid #1a2a1a;padding-bottom:6px">{line}</p>'
        elif line.startswith("**") and "**" in line[2:]:
            # Bold stock ticker line
            line_fmt = line.replace("**","<strong style='color:#ffffff;font-size:14px'>",1).replace("**","</strong>",1)
            brief_html += f'<p style="color:#e2e8f0;font-size:13px;margin:10px 0 4px">{line_fmt}</p>'
        elif line.startswith("•"):
            brief_html += f'<p style="color:#94a3b8;font-size:13px;margin:4px 0 4px 16px;line-height:1.6">{line}</p>'
        else:
            brief_html += f'<p style="color:#cbd5e1;font-size:13px;line-height:1.8;margin:4px 0">{line}</p>'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#05080d;font-family:Georgia,serif">
<div style="max-width:640px;margin:0 auto;background:#05080d">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#060e18,#06100e);padding:22px 28px;border-bottom:3px solid #00ff87">
    <div style="font-family:'Courier New',monospace;font-size:28px;font-weight:900;letter-spacing:5px;color:#00ff87">
      ALPHA<span style="color:#60a5fa">BRIEF</span>
    </div>
    <div style="font-family:'Courier New',monospace;font-size:10px;color:#475569;letter-spacing:2px;margin-top:5px">
      {today.upper()} · YOUR MORNING STOCK UPDATE
    </div>
    <div style="margin-top:10px;font-size:12px;color:#334155">
      Portfolio value: <span style="color:#fbbf24;font-weight:700">${portfolio_value:,.0f}</span>
    </div>
  </div>

  <!-- Brief -->
  <div style="padding:24px 28px;background:#080c14;line-height:1.7">
    {brief_html}
  </div>

  <!-- Portfolio -->
  <div style="padding:20px 28px;background:#05080d">
    <div style="font-family:'Courier New',monospace;font-size:10px;letter-spacing:2px;color:#475569;margin-bottom:12px">
      YOUR PORTFOLIO — TOP 8 BY GAINS
    </div>
    <table style="width:100%;border-collapse:collapse;border-radius:6px;overflow:hidden;border:1px solid #141e2e">
      <tr style="background:#060a10">
        <th style="padding:8px 14px;text-align:left;font-size:10px;color:#334155;font-family:'Courier New',monospace">STOCK</th>
        <th style="padding:8px 14px;text-align:left;font-size:10px;color:#334155;font-family:'Courier New',monospace">NAME</th>
        <th style="padding:8px 14px;text-align:right;font-size:10px;color:#334155;font-family:'Courier New',monospace">GAIN/LOSS</th>
      </tr>
      {rows}
    </table>
  </div>

  <!-- Footer -->
  <div style="padding:12px 28px;border-top:1px solid #141e2e;font-family:'Courier New',monospace;font-size:9px;color:#1e2d40;text-align:center">
    AlphaBrief · Powered by Claude AI · Updates daily from your Google Sheet · {today}
  </div>

</div>
</body>
</html>"""

def send_email(html):
    user = os.environ["GMAIL_USER"]
    pwd  = os.environ["GMAIL_APP_PASSWORD"]
    today = datetime.now().strftime("%b %d")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"☀️ AlphaBrief — Your Morning Stock Update {today}"
    msg["From"]    = user
    msg["To"]      = TO_EMAIL
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.sendmail(user, TO_EMAIL, msg.as_string())
    print(f"Email sent to {TO_EMAIL}")

def main():
    print(f"AlphaBrief starting — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    data   = read_drive_sheet()
    prompt = build_prompt(data)
    brief  = get_brief(prompt)
    print("Brief preview:\n" + brief[:400])
    html   = build_email(brief, data)
    send_email(html)
    print("Done.")

if __name__ == "__main__":
    main()
