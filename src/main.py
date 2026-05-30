"""
AlphaBrief - Daily Stock Research Bot
Runs every morning at 7 AM CT via GitHub Actions
Reads Daily Tracker from Google Drive -> AI research -> Email
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

def build_prompt(data):
    watch = sorted(data.get("Watchlist",[]), key=lambda s: safe_float(s.get("EPS Growth (Proj This Yr vs. Last Yr)",0)), reverse=True)[:15]
    bought = data.get("Bought",[])
    sip = data.get("SIP",[])
    today = datetime.now().strftime("%A, %B %d, %Y")

    w = "\n".join([f"  {s.get('Symbol','?')} | {s.get('Company Name','?')[:28]} | EPS Grw:{s.get('EPS Growth (Proj This Yr vs. Last Yr)','?')}% | Quality:{s.get('S&P Global Market Intelligence Quality','?')} | ESS:{s.get('Equity Summary Score (ESS) from LSEG StarMine','?')} | ESS Trend:{s.get('Equity Summary Score Change (1 Month)','?')} | Inst Buy Chg:{s.get('Institutional Ownership (Last vs. Prior Qtr)','?')}%" for s in watch])
    b = "\n".join([f"  {s.get('Fund Code',s.get('Symbol','?'))} | {s.get('Fund Name',s.get('Name','?'))[:22]} | P&L:{s.get('P&L %',s.get('pnlPct','?'))}% | Value:${s.get('Current Amount',s.get('currentPrice','?'))}" for s in bought[:20]])
    sp = "\n".join([f"  {s.get('Fund Code',s.get('Symbol','?'))} ${s.get('Amount',s.get('weeklyAmt','?'))}/wk" for s in sip])

    return f"""You are a high-conviction stock analyst. RISK TAKER investor wanting HIGH GROWTH. Today: {today}

WATCHLIST (top 15 by EPS growth):
{w}

CURRENT HOLDINGS:
{b}

SIP:
{sp}

CONTEXT: Investor recently trimmed portfolio — NO sell recommendations needed this week. Focus ONLY on buy opportunities.

Write morning brief with EXACTLY this structure:

🌅 MARKET PULSE
One sharp sentence on today's setup.

🎯 TOP 3 TO BUY TODAY
**TICKER** — Company
• Why: [catalyst + what makes it exceptional]
• Invest: $[50-300] — [buy now / wait for dip to $X]
• Risk: [one honest risk]

💡 HIGH CONVICTION PICK (high risk, high reward)
One aggressive pick. 4-sentence thesis. Why it could 2-3x.

📅 SIP CHECK
Any SIP stocks at a dip worth adding extra this week?

⚡ ONE THING TO WATCH TODAY
One macro/sector signal that moves your portfolio.

Under 300 words. Sharp hedge fund tone."""

def get_brief(prompt):
    r = requests.post("https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version":"2023-06-01","content-type":"application/json"},
        json={"model":ANTHROPIC_MODEL,"max_tokens":1200,"messages":[{"role":"user","content":prompt}]}, timeout=60)
    r.raise_for_status()
    return r.json()["content"][0]["text"]

def build_email(brief, data):
    today = datetime.now().strftime("%A, %B %d, %Y")
    bought = sorted(data.get("Bought",[]), key=lambda s: safe_float(s.get("P&L %",s.get("pnlPct",0))), reverse=True)

    rows = ""
    for s in bought[:8]:
        sym  = s.get("Fund Code", s.get("Symbol","?"))
        name = s.get("Fund Name",  s.get("Name","?"))[:22]
        pnl  = safe_float(s.get("P&L %", s.get("pnlPct",0)))
        col  = "#00ff87" if pnl >= 0 else "#ff4d4d"
        rows += f'<tr><td style="padding:7px 12px;font-weight:700;color:#e2e8f0">{sym}</td><td style="padding:7px 12px;color:#64748b;font-size:11px">{name}</td><td style="padding:7px 12px;text-align:right;font-weight:700;color:{col}">{("+" if pnl>=0 else "")}{pnl:.1f}%</td></tr>'

    brief_html = ""
    for line in brief.split("\n"):
        line = line.strip()
        if not line: brief_html += "<br/>"
        elif line[0] in "🌅🎯💡📅⚡": brief_html += f'<p style="color:#00ff87;font-size:13px;font-weight:700;margin:16px 0 6px;letter-spacing:1px">{line}</p>'
        elif line.startswith("•"): brief_html += f'<p style="color:#94a3b8;font-size:12px;margin:3px 0 3px 12px">{line}</p>'
        else: brief_html += f'<p style="color:#cbd5e1;font-size:12px;line-height:1.7;margin:3px 0">{line}</p>'

    return f"""<!DOCTYPE html><html><body style="margin:0;background:#05080d;font-family:'Courier New',monospace">
<div style="max-width:620px;margin:0 auto">
  <div style="background:#060e18;padding:20px 26px;border-bottom:2px solid #00ff87">
    <div style="font-size:26px;font-weight:900;letter-spacing:5px;color:#00ff87">ALPHA<span style="color:#60a5fa">BRIEF</span></div>
    <div style="font-size:10px;color:#475569;letter-spacing:2px;margin-top:3px">{today.upper()} · 7:00 AM CT</div>
  </div>
  <div style="padding:22px 26px;background:#080c14">{brief_html}</div>
  <div style="padding:18px 26px">
    <div style="font-size:9px;letter-spacing:2px;color:#475569;margin-bottom:10px">PORTFOLIO SNAPSHOT</div>
    <table style="width:100%;border-collapse:collapse;background:#0a0f18;border:1px solid #141e2e">
      <tr style="background:#060a10"><th style="padding:7px 12px;text-align:left;font-size:9px;color:#334155">TICKER</th><th style="padding:7px 12px;text-align:left;font-size:9px;color:#334155">NAME</th><th style="padding:7px 12px;text-align:right;font-size:9px;color:#334155">P&L</th></tr>
      {rows}
    </table>
  </div>
  <div style="padding:10px 26px;border-top:1px solid #141e2e;font-size:9px;color:#1e2d40">AlphaBrief · Claude AI · Daily Tracker · {today}</div>
</div></body></html>"""

def send_email(html):
    user = os.environ["GMAIL_USER"]
    pwd  = os.environ["GMAIL_APP_PASSWORD"]
    today = datetime.now().strftime("%b %d")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[AlphaBrief] Morning Brief — {today}"
    msg["From"] = user
    msg["To"] = TO_EMAIL
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
    print("Brief generated:\n" + brief[:300])
    html   = build_email(brief, data)
    send_email(html)
    print("Done.")

if __name__ == "__main__":
    main()
