# Inbox Ascent — Setup

Two pipelines, one script (`inbox_ascent.py`):

- **Fleet inbox** → classified Minion / Elite / Boss → logged to Notion with
  points + SLA deadline → Slack alert to #fleet-elites or #fleet-onboarding
  → calendar block for Boss (new account onboarding) items.
- **Personal inbox** → not scored → Gemini writes a plain-language summary +
  action item → posted straight to #personal-inbox in Slack.

Runs on a schedule via GitHub Actions (`inbox-ascent-workflow.yml`) — see
that file for the 8am/4pm cron setup and the daylight-saving caveat.

## One-time setup

### 1. Move this folder into your automation repo
Copy `Inbox_Ascent/` (this whole folder) into the same GitHub repo that
already runs your other Gmail → Notion automations. Move
`inbox-ascent-workflow.yml` into `.github/workflows/inbox-ascent.yml` in
that repo.

### 2. Notion integration token
If you don't already have one: notion.so/my-integrations → New integration
→ copy the "Internal Integration Secret". Then open both **Inbox Ascent —
Encounters** and **Inbox Ascent — Weekly Runs** in Notion → "..." menu →
Connections → add your integration. Without this share step, the API calls
will 404 even with a valid token.

### 3. Gemini API key
console.cloud.google.com or aistudio.google.com → get an API key.

### 4. Slack bot token
You're already connected via the Slack app in this workspace — if you want
this script to post as that same app, grab its Bot Token from
api.slack.com/apps → your app → OAuth & Permissions (needs `chat:write`,
and the bot needs to be a member of #fleet-elites, #fleet-onboarding, and
#personal-inbox — it already was auto-invited when I created them).

### 5. Gmail + Calendar tokens (the fiddly part)
You need a `token.json`-style credential for EACH inbox (fleet + personal),
generated once locally via OAuth, then pasted as GitHub secrets:

- `FLEET_GMAIL_TOKEN_JSON` — scopes: `gmail.readonly`, `gmail.modify`
- `PERSONAL_GMAIL_TOKEN_JSON` — scopes: `gmail.readonly`, `gmail.modify`,
  `calendar.events` (calendar block for Boss items is created on
  kwatson@mighty-wash.com using this token)

If you've already got a Google Cloud OAuth client from your prior Gmail →
Notion script, reuse it — just make sure the consent screen includes the
calendar scope this time, or the calendar step will silently fail on
insufficient permission. Standard pattern:

```python
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0)
print(creds.to_json())  # paste this whole blob in as the GitHub secret
```

Run that once per account (with that account's matching scopes), paste the
printed JSON as the two secrets above.

### 6. Add all 5 secrets to the repo
Repo → Settings → Secrets and variables → Actions → New repository secret:
`GEMINI_API_KEY`, `NOTION_API_KEY`, `SLACK_BOT_TOKEN`,
`FLEET_GMAIL_TOKEN_JSON`, `PERSONAL_GMAIL_TOKEN_JSON`

### 7. Test it
Actions tab → Inbox Ascent → Run workflow (the `workflow_dispatch` trigger
lets you fire it on demand instead of waiting for the cron).

## Notes / known rough edges

- Elite senders are hardcoded in `ELITE_SENDERS` at the top of the script —
  add new priority fleet accounts there as they come up.
- The Boss trigger also hard-matches your existing Gmail filter subject
  ("New Entry: Automobile Information Form") so it can never be
  misclassified by Gemini.
- Personal-inbox items are still logged to the Encounters database (Inbox =
  Personal) for a record, but with no Type/Points — they're deliberately
  outside the scoring system per your call to keep Kelly's mail out of the
  game and focused on plain summaries instead.
- Both pipelines dedupe using a `InboxAscent/Processed` Gmail label, so
  re-running the workflow (or the two daily runs seeing overlap) won't
  double-post.
