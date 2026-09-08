"""
Inbox Ascent — Fleet gamified triage + Personal smart digest
==============================================================

Runs twice daily (8am / 4pm, via GitHub Actions cron — see
.github_workflow_inbox_ascent.yml). Two independent pipelines:

1. FLEET PIPELINE
   Reads the fleet Gmail inbox -> classifies each new message via Gemini
   as Minion / Elite / Boss -> logs it to the "Inbox Ascent — Encounters"
   Notion database with points + an SLA deadline -> pings Slack
   (#fleet-elites or #fleet-onboarding) for anything above Minion tier ->
   creates a Google Calendar block for Boss-tier (new account onboarding)
   items.

2. PERSONAL PIPELINE
   Reads the personal Gmail inbox -> NOT scored/gamified -> Gemini writes
   a one-line "what this says" + "what you need to do (if anything)" ->
   posts straight to #personal-inbox in Slack in real time.

Both pipelines mark processed messages with a Gmail label so re-runs
don't double-count.

REQUIRED ENVIRONMENT VARIABLES (set these as GitHub Actions secrets):
    GEMINI_API_KEY            - Google AI Studio / Gemini API key
    NOTION_API_KEY             - Notion internal integration token
                                  (must be shared with both Inbox Ascent databases)
    SLACK_BOT_TOKEN             - Slack bot token with chat:write scope
    FLEET_GMAIL_TOKEN_JSON       - contents of an authorized_user token.json
                                  for the FLEET Gmail account (scopes: gmail.readonly,
                                  gmail.modify)
    PERSONAL_GMAIL_TOKEN_JSON    - same, for the PERSONAL Gmail account. This
                                  token also needs the calendar.events scope
                                  since Boss-tier calendar blocks are created
                                  on kwatson@mighty-wash.com.

See README.md in this folder for how to generate the two token JSON blobs
and where to find your Notion integration token.
"""

import os
import json
import base64
from datetime import datetime, timedelta, timezone

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google import genai

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

NOTION_VERSION = "2025-09-03"  # Notion's data-source API. Bump if Notion ships a newer one.
NOTION_ENCOUNTERS_DS = "26697fb1-24ee-4900-a0ac-7edcd3490cc3"     # Inbox Ascent — Encounters
NOTION_WEEKLY_RUNS_DS = "904ef546-e48b-4af6-9929-08b72e11d3c2"    # Inbox Ascent — Weekly Runs

SLACK_FLEET_ELITES_CHANNEL = "C0C07GKSCR1"       # #fleet-elites
SLACK_FLEET_ONBOARDING_CHANNEL = "C0C0DNUHAAE"   # #fleet-onboarding
SLACK_PERSONAL_INBOX_CHANNEL = "C0C09BWAUH3"     # #personal-inbox

CALENDAR_ID = "kwatson@mighty-wash.com"

# Elite fleet senders — bump this list if new priority accounts come on
ELITE_SENDERS = {
    "haaron@diamondbackenergy.com": "Diamondback",
    "kathy.m.bonnell@exxonmobil.com": "Exxon",
    "kerry.griffin@dvn.com": "Devon",
}

# Existing Gmail filter already tags new fleet accounts with this subject
BOSS_SUBJECT_MARKER = "New Entry: Automobile Information Form"

POINTS = {"Minion": 10, "Elite": 25, "Boss": 50}
SLA_HOURS = {"Minion": 48, "Elite": 24, "Boss": 4}

PROCESSED_LABEL = "InboxAscent/Processed"

GMAIL_SCOPES_FLEET = ["https://www.googleapis.com/auth/gmail.readonly",
                       "https://www.googleapis.com/auth/gmail.modify"]
GMAIL_SCOPES_PERSONAL = GMAIL_SCOPES_FLEET + ["https://www.googleapis.com/auth/calendar.events"]

genai_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# --------------------------------------------------------------------------
# AUTH HELPERS
# --------------------------------------------------------------------------

def gmail_service(token_env_var, scopes):
    """Build a Gmail API client from a token JSON blob stored in an env var."""
    token_info = json.loads(os.environ[token_env_var])
    creds = Credentials.from_authorized_user_info(token_info, scopes)
    return build("gmail", "v1", credentials=creds)


def calendar_service(token_env_var):
    token_info = json.loads(os.environ[token_env_var])
    creds = Credentials.from_authorized_user_info(token_info, GMAIL_SCOPES_PERSONAL)
    return build("calendar", "v3", credentials=creds)


# --------------------------------------------------------------------------
# GMAIL HELPERS
# --------------------------------------------------------------------------

def fetch_unprocessed_messages(service, max_results=25):
    """Return unread inbox messages that don't yet have the Processed label."""
    query = f'in:inbox -label:"{PROCESSED_LABEL}"'
    resp = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    return resp.get("messages", [])


def get_message_detail(service, msg_id):
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    body = _extract_body(msg["payload"])
    return {
        "id": msg_id,
        "thread_id": msg["threadId"],
        "subject": headers.get("subject", "(no subject)"),
        "sender": headers.get("from", ""),
        "sender_email": _extract_email(headers.get("from", "")),
        "received_at": datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc),
        "snippet": msg.get("snippet", ""),
        "body": body[:4000],  # keep prompt size sane
    }


def _extract_email(from_header):
    if "<" in from_header and ">" in from_header:
        return from_header.split("<")[1].split(">")[0].strip().lower()
    return from_header.strip().lower()


def _extract_body(payload):
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        text = _extract_body(part)
        if text:
            return text
    return ""


def mark_processed(service, msg_id):
    label_id = _get_or_create_label(service, PROCESSED_LABEL)
    service.users().messages().modify(
        userId="me", id=msg_id, body={"addLabelIds": [label_id]}
    ).execute()


_label_cache = {}

def _get_or_create_label(service, name):
    if name in _label_cache:
        return _label_cache[name]
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == name:
            _label_cache[name] = lbl["id"]
            return lbl["id"]
    created = service.users().labels().create(
        userId="me", body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
    ).execute()
    _label_cache[name] = created["id"]
    return created["id"]


# --------------------------------------------------------------------------
# GEMINI CLASSIFICATION
# --------------------------------------------------------------------------

def classify_fleet_message(msg):
    """Returns dict: {type: Minion|Elite|Boss, summary: str, action: str}"""
    is_known_elite_sender = msg["sender_email"] in ELITE_SENDERS
    is_boss_subject = BOSS_SUBJECT_MARKER.lower() in msg["subject"].lower()

    prompt = f"""You triage fleet-account emails for a car wash company.

Subject: {msg['subject']}
From: {msg['sender']}
Body:
{msg['body']}

Known priority account: {"YES - " + ELITE_SENDERS[msg['sender_email']] if is_known_elite_sender else "no"}
Looks like a new-account onboarding form: {"YES" if is_boss_subject else "no"}

Classify this email's urgency tier:
- "Boss": a brand-new fleet account signing up (onboarding)
- "Elite": a priority account (already flagged above), OR any account reporting
  vehicles not washing / service failures / a complaint needing fast attention
- "Minion": everything else routine

Respond ONLY as compact JSON:
{{"type": "Minion|Elite|Boss", "summary": "one sentence, what this email says", "action": "one sentence, what Kesean needs to do, or 'No action needed'"}}
"""
    result = genai_client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
    data = _parse_json_response(result.text)

    # Hard overrides so known senders/subjects never get misclassified
    if is_boss_subject:
        data["type"] = "Boss"
    elif is_known_elite_sender and data.get("type") == "Minion":
        data["type"] = "Elite"
    return data


def classify_personal_message(msg):
    """Returns dict: {summary: str, action: str, urgent: bool}"""
    prompt = f"""You triage personal work email for a Revenue Ops lead at a car wash company.

Subject: {msg['subject']}
From: {msg['sender']}
Body:
{msg['body']}

Respond ONLY as compact JSON:
{{"summary": "one sentence, what this email says", "action": "one sentence, what Kesean needs to do, or 'No action needed'", "urgent": true or false}}
Mark urgent=true only if it needs a response today.
"""
    result = genai_client.models.generate_content(model="gemini-3.6-flash", contents=prompt)
    return _parse_json_response(result.text)


def _parse_json_response(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text)


# --------------------------------------------------------------------------
# NOTION HELPERS
# --------------------------------------------------------------------------

NOTION_HEADERS = {
    "Authorization": f"Bearer {os.environ.get('NOTION_API_KEY', '')}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def get_or_create_week_page():
    today = datetime.now(timezone.utc).date()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_label = f"Week of {week_start.strftime('%b %-d')}"

    query = {
        "filter": {
            "property": "Week Start",
            "date": {"equals": week_start.isoformat()},
        }
    }
    resp = requests.post(
        f"https://api.notion.com/v1/data_sources/{NOTION_WEEKLY_RUNS_DS}/query",
        headers=NOTION_HEADERS, json=query
    ).json()
    results = resp.get("results", [])
    if results:
        return results[0]["id"]

    create_body = {
        "parent": {"type": "data_source_id", "data_source_id": NOTION_WEEKLY_RUNS_DS},
        "properties": {
            "Week Of": {"title": [{"text": {"content": week_label}}]},
            "Week Start": {"date": {"start": week_start.isoformat()}},
            "Result": {"select": {"name": "In Progress"}},
        },
    }
    created = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=create_body).json()
    return created["id"]


def create_encounter(msg, classification, inbox_label, week_page_id=None):
    etype = classification.get("type", "Minion") if inbox_label == "Fleet" else None
    points = POINTS.get(etype, 0) if etype else 0
    received_at = msg["received_at"]
    sla_deadline = received_at + timedelta(hours=SLA_HOURS.get(etype, 48)) if etype else None

    properties = {
        "Name": {"title": [{"text": {"content": msg["subject"][:200]}}]},
        "Inbox": {"select": {"name": inbox_label}},
        "Sender": {"rich_text": [{"text": {"content": msg["sender"][:200]}}]},
        "Status": {"select": {"name": "Open"}},
        "Received At": {"date": {"start": received_at.isoformat()}},
    }
    if etype:
        properties["Type"] = {"select": {"name": etype}}
        properties["Points"] = {"number": points}
        properties["SLA Deadline"] = {"date": {"start": sla_deadline.isoformat()}}
    if week_page_id:
        properties["Week"] = {"relation": [{"id": week_page_id}]}

    body = {"parent": {"type": "data_source_id", "data_source_id": NOTION_ENCOUNTERS_DS}, "properties": properties}
    resp = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=body).json()
    return resp.get("id"), etype, points


# --------------------------------------------------------------------------
# SLACK HELPER
# --------------------------------------------------------------------------

def slack_post(channel_id, text):
    requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
        json={"channel": channel_id, "text": text},
    )


# --------------------------------------------------------------------------
# CALENDAR HELPER
# --------------------------------------------------------------------------

def create_boss_calendar_block(cal_service, msg):
    start = datetime.now(timezone.utc) + timedelta(minutes=15)
    end = start + timedelta(minutes=30)
    event = {
        "summary": f"🐉 BOSS: New fleet onboarding — {msg['sender']}",
        "description": f"Auto-created by Inbox Ascent.\n\nSubject: {msg['subject']}\n\n{msg['snippet']}",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    cal_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()


# --------------------------------------------------------------------------
# PIPELINES
# --------------------------------------------------------------------------

def process_fleet_inbox():
    service = gmail_service("FLEET_GMAIL_TOKEN_JSON", GMAIL_SCOPES_FLEET)
    cal_service = calendar_service("PERSONAL_GMAIL_TOKEN_JSON")  # calendar lives on personal account
    week_page_id = get_or_create_week_page()

    for stub in fetch_unprocessed_messages(service):
        msg = get_message_detail(service, stub["id"])
        classification = classify_fleet_message(msg)
        etype = classification["type"]

        page_id, etype, points = create_encounter(msg, classification, "Fleet", week_page_id)

        if etype == "Boss":
            slack_post(
                SLACK_FLEET_ONBOARDING_CHANNEL,
                f"🐉 *New Boss encounter* ({points} pts)\n*{msg['subject']}*\nFrom: {msg['sender']}\n"
                f"{classification['summary']}\n*Action:* {classification['action']}",
            )
            create_boss_calendar_block(cal_service, msg)
        elif etype == "Elite":
            slack_post(
                SLACK_FLEET_ELITES_CHANNEL,
                f"⚔️ *Elite encounter* ({points} pts)\n*{msg['subject']}*\nFrom: {msg['sender']}\n"
                f"{classification['summary']}\n*Action:* {classification['action']}",
            )

        mark_processed(service, msg["id"])


def process_personal_inbox():
    service = gmail_service("PERSONAL_GMAIL_TOKEN_JSON", GMAIL_SCOPES_PERSONAL)

    for stub in fetch_unprocessed_messages(service):
        msg = get_message_detail(service, stub["id"])
        classification = classify_personal_message(msg)

        # Still logged to Notion for a record, but with no Type/Points (unscored)
        create_encounter(msg, classification, "Personal")

        flag = "🔴" if classification.get("urgent") else "🔵"
        slack_post(
            SLACK_PERSONAL_INBOX_CHANNEL,
            f"{flag} *{msg['subject']}*\nFrom: {msg['sender']}\n"
            f"{classification['summary']}\n*Action:* {classification['action']}",
        )

        mark_processed(service, msg["id"])


def main():
    process_fleet_inbox()
    process_personal_inbox()


if __name__ == "__main__":
    main()
