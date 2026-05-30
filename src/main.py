"""
AlphaBrief - Daily Stock Research Bot
Runs every morning at 7 AM CT via GitHub Actions
Reads Daily Tracker from Google Drive -> AI research + web search -> Email
"""

import os, json, smtplib, requests, re
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
    total = 0
    for s in bought:
        val = safe_float(s.get("Current Amount", s.get("currentPrice", 0)))
        total += val
    return total

def clean_brief(text):
    """Remove any internal thinking / search narration lines from Claude output"""
    lines = text.split("\n")
    cleaned = []
    skip_phrases = [
        "let me search", "let me look", "i need to look", "i'll search",
        "now let me", "now i have", "let me compile", "let me get",
        "searching for", "looking up", "i will search", "let me find",
        "i'm going to", "let me check", "i found", "based on my search"
    ]
    for line in lines:
        lower = line.strip().lower()
        if any(phrase in lower for phrase in skip_phrases):
            continue
        # Remove lines that are just "---" separators (we style in HTML)
        if line.strip() in ["---", "***", "___"]:
            continue
        cleaned.append(line)

    # Remove multiple consecutive blank lines
    result = re.sub(r'\n{3,}', '\n\n', "\n".join(cleaned))
    return result.strip()

def build_prompt(data):
    watch  = sorted(data.get("Watchlist",[]), key=lambda s: safe_float(s.get("EPS Growth (Proj This Yr vs. Last Yr)",0)), reverse=True)[:15]
    bought = data.get("Bought",[])
    sip    = data.get("SIP",[])
    today  = datetime.now().strftime("%A, %B %d, %Y")

    portfolio_value = get_portfolio_value(bought)
    suggested_min   = max(100, round(portfolio_value * 0.01, -1))
    suggested_max   = max(300, round(portfolio_value * 0.03, -1))

    w  = "\n".join([f"  {s.get('Symbol','?')} | {s.get('Company Name','?')[:28]} | EPS Growth:{s.get('EPS Growth (Proj This Yr vs. Last Yr)','?')}% | Quality:{s.get('S&P Global Market Intelligence Quality','?')}/100 | Analyst Score:{s.get('Equity Summary Score (ESS) from LSEG StarMine','?')} | Trend:{s.get('Equity Summary Score Change (1 Month)','?')} | Big investors buying:{s.get('Institutional Ownership (Last vs. Prior Qtr)','?')}% more" for s in watch])
    b  = "\n".join([f"  {s.get('Fund Code',s.get('Symbol','?'))} | {s.get('Fund Name',s.get('Name','?'))[:22]} | Gain/Loss:{s.get('P&L %',s.get('pnlPct','?'))}% | Value:${s.get('Current Amount',s.get('currentPrice','?'))}" for s in bought[:20]])
    sp = "\n".join([f"  {s.get('Fund Code',s.get('Symbol','?'))} — ${s.get('Amount',s.get('weeklyAmt','?'))} every week" for s in sip])

    return f"""You are helping a regular investor with their morning stock update. Search the web for latest news on the top stocks before writing.

IMPORTANT FORMATTING RULES:
- Do NOT write any thinking out loud. Do NOT say "let me search" or "now I have" or "let me look up". Just write the final brief directly.
- Write in very simple plain English — like texting a friend, not a Wall Street report
- No jargon. If you must use a finance word, explain it simply in brackets right after
- Use the EXACT section headers below with the emojis

Today: {today}
Total portfolio value: ${portfolio_value:,.0f}
Suggested amount per new stock: ${suggested_min:,.0f} to ${suggested_max:,.0f}

WATCHLIST (top 15 by earnings growth):
{w}

STOCKS ALREADY OWNED:
{b}

WEEKLY AUTO-BUY LIST:
{sp}

RULES:
- Do NOT suggest selling anything this week (investor already trimmed)
- Investor is a risk taker who likes high growth stocks
- Search for latest news on each stock you recommend

Write ONLY the brief below, nothing else before it:

☀️ WHAT'S HAPPENING TODAY
2 simple sentences. What is the market doing and why does it matter for their portfolio?

🛒 TOP 3 STOCKS TO BUY
**TICKER** — Company Name (one line: what this company does)
• Why now: [simple explanation of what good thing is happening]
• Latest news: [real recent news you found, one sentence]
• How much: $[{suggested_min:,.0f}–{suggested_max:,.0f}] — [buy now / or wait for price drop to $X]
• Watch out for: [one simple risk]

💎 ONE BIG BET
**TICKER** — Company Name
What they do, why it could 2–3x, latest news, how much to invest. 4 sentences max.

📅 AUTO-BUY CHECK
Any of the weekly auto-buy stocks cheaper than usual? Add extra or keep as-is? One sentence per stock that needs attention.

📰 ONE NEWS STORY TO KNOW
One thing happening in the world today that affects their stocks. Why does it matter? Plain English, 2 sentences.

Total length: under 380 words."""

def get_brief(prompt):
    """Call Claude with web search tool enabled"""
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
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=90
    )
    r.raise_for_status()
    data = r.json()

    # Only grab text blocks — skip tool_use and tool_result blocks
    text_parts = [
        block["text"]
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    raw = "\n".join(text_parts)
    return clean_brief(raw)

def build_email(brief, data):
    today           = datetime.now().strftime("%A, %B %d, %Y")
    bought          = sorted(data.get("Bought",[]), key=lambda s: safe_float(s.get("P&L %",s.get("pnlPct",0))), reverse=True)
    portfolio_value = get_portfolio_value(bought)

    # Portfolio table rows
    rows = ""
    for s in bought[:8]:
        sym  = s.get("Fund Code", s.get("Symbol","?"))
        name = s.get("Fund Name",  s.get("Name","?"))[:22]
        pnl  = safe_float(s.get("P&L %", s.get("pnlPct",0)))
        col  = "#00c96e" if pnl >= 0 else "#ff5252"
        arrow = "▲" if pnl >= 0 else "▼"
        rows += f"""<tr style="border-bottom:1px solid #0f1a0f">
          <td style="padding:10px 16px;font-weight:700;color:#ffffff;font-size:13px;font-family:'Courier New',monospace">{sym}</td>
          <td style="padding:10px 16px;color:#6b7280;font-size:12px">{name}</td>
          <td style="padding:10px 16px;text-align:right;font-weight:700;color:{col};font-size:13px">{arrow} {abs(pnl):.1f}%</td>
        </tr>"""

    # Convert brief text to clean HTML
    def line_to_html(line):
        line = line.strip()
        if not line:
            return '<div style="height:6px"></div>'

        # Section headers
        if line and line[0] in "☀🛒💎📅📰":
            return f'''<div style="margin:24px 0 10px;padding:10px 16px;background:#0a1a0a;border-left:3px solid #00c96e;border-radius:0 6px 6px 0">
              <span style="color:#00c96e;font-size:14px;font-weight:700;font-family:'Courier New',monospace;letter-spacing:0.5px">{line}</span>
            </div>'''

        # Bold ticker lines **TICKER** — Name
        if line.startswith("**"):
            line_fmt = re.sub(r'\*\*(.+?)\*\*', r'<strong style="color:#ffffff;font-size:15px;letter-spacing:0.5px">\1</strong>', line)
            return f'<p style="color:#e2e8f0;font-size:14px;margin:12px 0 4px;font-family:Georgia,serif">{line_fmt}</p>'

        # Bullet points
        if line.startswith("•"):
            label_map = {
                "• Why now:":      ("#60a5fa", "Why now"),
                "• Latest news:":  ("#fbbf24", "Latest news"),
                "• How much:":     ("#00c96e", "How much"),
                "• Watch out for:":("#f87171", "Watch out for"),
            }
            for key, (color, label) in label_map.items():
                if line.lower().startswith(key.lower()):
                    content = line[len(key):].strip()
                    return f'''<div style="margin:5px 0 5px 12px;padding:6px 12px;border-radius:4px;background:#080e08">
                      <span style="color:{color};font-size:11px;font-weight:700;font-family:'Courier New',monospace;text-transform:uppercase;letter-spacing:1px">{label} </span>
                      <span style="color:#cbd5e1;font-size:13px">{content}</span>
                    </div>'''
            # Generic bullet
            return f'<p style="color:#94a3b8;font-size:13px;margin:5px 0 5px 16px;line-height:1.6">{line}</p>'

        # Normal text
        return f'<p style="color:#cbd5e1;font-size:13px;line-height:1.8;margin:5px 0;font-family:Georgia,serif">{line}</p>'

    brief_html = "\n".join(line_to_html(l) for l in brief.split("\n"))

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>AlphaBrief</title>
</head>
<body style="margin:0;padding:20px 0;background:#030608;font-family:Georgia,serif">
<div style="max-width:620px;margin:0 auto;background:#05080d;border-radius:12px;overflow:hidden;box-shadow:0 4px 24px rgba(0,255,135,0.08)">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#040d18 0%,#041208 100%);padding:28px 28px 22px;border-bottom:2px solid #00c96e">
    <table style="width:100%"><tr>
      <td>
        <div style="font-family:'Courier New',monospace;font-size:30px;font-weight:900;letter-spacing:6px;color:#00c96e">
          ALPHA<span style="color:#60a5fa">BRIEF</span>
        </div>
        <div style="font-family:'Courier New',monospace;font-size:10px;color:#374151;letter-spacing:2px;margin-top:5px">
          {today.upper()}
        </div>
      </td>
      <td style="text-align:right;vertical-align:top">
        <div style="font-size:11px;color:#374151;font-family:'Courier New',monospace">PORTFOLIO</div>
        <div style="font-size:22px;font-weight:700;color:#fbbf24;font-family:'Courier New',monospace">${portfolio_value:,.0f}</div>
      </td>
    </tr></table>
  </div>

  <!-- Brief content -->
  <div style="padding:20px 28px 10px;background:#05080d">
    {brief_html}
  </div>

  <!-- Divider -->
  <div style="margin:0 28px;height:1px;background:linear-gradient(90deg,transparent,#1a3a1a,transparent)"></div>

  <!-- Portfolio table -->
  <div style="padding:20px 28px">
    <div style="font-family:'Courier New',monospace;font-size:10px;letter-spacing:2px;color:#374151;margin-bottom:12px">
      📊 YOUR TOP 8 HOLDINGS
    </div>
    <table style="width:100%;border-collapse:collapse;background:#080e08;border-radius:8px;overflow:hidden;border:1px solid #0f1a0f">
      <tr style="background:#040a04">
        <th style="padding:9px 16px;text-align:left;font-size:9px;color:#374151;font-family:'Courier New',monospace;letter-spacing:2px;font-weight:600">STOCK</th>
        <th style="padding:9px 16px;text-align:left;font-size:9px;color:#374151;font-family:'Courier New',monospace;letter-spacing:2px;font-weight:600">NAME</th>
        <th style="padding:9px 16px;text-align:right;font-size:9px;color:#374151;font-family:'Courier New',monospace;letter-spacing:2px;font-weight:600">GAIN/LOSS</th>
      </tr>
      {rows}
    </table>
  </div>

  <!-- Footer -->
  <div style="padding:14px 28px;background:#030608;text-align:center">
    <div style="font-family:'Courier New',monospace;font-size:9px;color:#1f2937;letter-spacing:1px">
      AlphaBrief · Claude AI + Live Web Search · Powered by your Google Sheet · {today}
    </div>
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
    print("Brief preview:\n" + brief[:500])
    html   = build_email(brief, data)
    send_email(html)
    print("Done.")

if __name__ == "__main__":
    main()
