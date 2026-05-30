# AlphaBrief 🚀

Automated morning stock brief — runs every weekday at 7 AM CT.  
Reads your **Daily Tracker** Google Sheet → AI deep research → Email to your inbox.

## Setup (one-time, ~20 minutes)

### 1. Fork / push this repo to GitHub

### 2. Get your API keys

#### Anthropic API Key
- Go to https://console.anthropic.com
- Create API key → copy it

#### Google Service Account (to read your Drive)
1. Go to https://console.cloud.google.com
2. Create a new project (or use existing)
3. Enable **Google Sheets API** and **Google Drive API**
4. Go to IAM → Service Accounts → Create Service Account
5. Name it `alphabrief`, click Create
6. Click the service account → Keys tab → Add Key → JSON
7. Download the JSON file
8. **Share your Daily Tracker sheet** with the service account email
   (looks like `alphabrief@your-project.iam.gserviceaccount.com`)
   Give it **Viewer** access

#### Gmail App Password
1. Go to your Google Account → Security
2. Enable 2-Step Verification (if not already)
3. Search "App passwords" → Create one named "AlphaBrief"
4. Copy the 16-character password

### 3. Add GitHub Secrets
Go to your repo → Settings → Secrets and variables → Actions → New secret

| Secret Name | Value |
|-------------|-------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Paste the entire contents of the JSON file |
| `GMAIL_USER` | ashishgoyal.ietc@gmail.com |
| `GMAIL_APP_PASSWORD` | The 16-char app password |

### 4. Test it manually
- Go to your repo → Actions tab
- Click "AlphaBrief Morning Stock Brief"
- Click "Run workflow" → Run
- Check your email in ~1 minute ✓

## How it works
```
7:00 AM CT (Mon-Fri)
    ↓
GitHub Action triggers
    ↓
Reads Daily Tracker from Google Drive
(Watchlist + Bought + SIP tabs)
    ↓
Claude AI does deep research on watchlist stocks
    ↓
Generates: Top 3 to Buy + High Conviction Pick + SIP Check
    ↓
Sends HTML email to ashishgoyal.ietc@gmail.com
```

## Updating your watchlist
Just update the **Daily Tracker** Google Sheet — next morning's email auto-picks up the new data. No code changes needed.
