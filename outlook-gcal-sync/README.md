# Outlook → Google Calendar sync

A small self-hosted service that mirrors a published Outlook calendar into
Google Calendar. It polls the Outlook ICS feed on a schedule and keeps a
dedicated Google calendar in step: new events appear, edits follow, and events
deleted in Outlook disappear from Google.

One-way only. Nothing is ever written back to Outlook.


## What it does

- Imports a **published Outlook calendar** (`.ics` link) — no Azure app
  registration, no admin consent, works with outlook.com and most work accounts.
- Writes into a **calendar it creates and owns**, so your existing Google events
  are never touched. Every event it writes carries a private tag; anything
  untagged is left alone, always.
- Keeps **recurring events recurring**. The RRULE is handed to Google as-is, so
  a weekly stand-up stays one event instead of 250 copies. Occurrences that
  Outlook moved or cancelled individually are applied as instance-level edits.
- Handles **deletions**. An event that leaves the feed leaves your Google
  calendar on the next run.
- Offers a **busy-blocks mode** that copies only the times, with no titles,
  descriptions or locations — for mirroring a work calendar into a personal
  account without leaking content.
- Runs **several calendars at once**, each with its own target and settings.

## What it does not do

- No two-way sync, and no writing to Outlook.
- No attendees, invitations or RSVPs — published feeds do not carry reliable
  attendee data, and copying it would send invitations from your Google account.
- No attachments or embedded meeting join buttons. Teams/Zoom links inside the
  description survive as text.
- If your organisation has disabled calendar publishing, this approach cannot
  work at all; you would need a Microsoft Graph app registered by an admin.

---

## Setup

### 1. Publish your Outlook calendar

In Outlook on the web: **Settings → Calendar → Shared calendars → Publish a
calendar**.

Pick the calendar, choose **Can view all details** (or *Can view when I'm busy*
for busy-only), click **Publish**, then copy the **ICS** link. Not the HTML one.

> That URL is a secret — anyone holding it can read the calendar. Treat it like
> a password, and re-publish to rotate it if it leaks.

### 2. Create your own Google OAuth client

The app talks to Google as *you*, using a client you own, so calendar data
never passes through anyone else's project.

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a project.
2. Enable the **Google Calendar API** for it.
3. Configure the OAuth consent screen as **External**, and add your own Google
   address under **Test users**. (A personal-use app can stay in "Testing"
   forever; you do not need verification. Refresh tokens for unverified apps
   expire after 7 days, so if the app is in testing you will be asked to
   reconnect weekly — publishing the app to "In production" removes that.)
4. Create credentials → **OAuth client ID** → **Web application**, and add this
   authorised redirect URI:

   ```
   http://localhost:8080/oauth/google/callback
   ```

   If you host it elsewhere, use `<your APP_BASE_URL>/oauth/google/callback`.
5. Copy the client ID and client secret.

Scopes requested: `calendar` (read/write, needed to create the mirror calendar
and manage its events) and `userinfo.email` (only to show which account is
connected).

### 3. Run it

```bash
cp .env.example .env
# fill in APP_PASSWORD, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
docker compose up -d
```

Open <http://localhost:8080>, sign in, and click **Connect Google account**.

### 4. Add your calendar

**Add a calendar** → paste the ICS link → **Test this link** to confirm it
parses → leave the target as *Create a new Google calendar* → **Add and sync**.

The first run happens immediately. After that it runs on the interval you set
on the dashboard (default: hourly).

---

## Configuration

All configuration is environment variables, via `.env`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `APP_PASSWORD` | *(empty)* | Password for the web UI. Empty means no login — only acceptable when bound to localhost. |
| `APP_BASE_URL` | `http://localhost:8080` | Public URL. Must match the registered redirect URI. |
| `GOOGLE_CLIENT_ID` | — | From the Google Cloud Console. |
| `GOOGLE_CLIENT_SECRET` | — | From the Google Cloud Console. |
| `DATA_DIR` | `./data` | Where the SQLite database and encryption keys live. |

Per-calendar settings live in the UI: target calendar, detail level, how many
days of history and future to import, whether Google adds its own
notifications, and whether the calendar syncs automatically.

## Running without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
set -a && source .env && set +a
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite covers ICS parsing (all-day events, `DURATION` without `DTEND`,
Windows timezone names, recurrence rules and exceptions) and the sync engine
against an in-memory stand-in for the Calendar API — including that events it
did not create are never deleted.

---

## How the sync works

Each run:

1. Fetches the feed with `If-None-Match` / `If-Modified-Since`. An unchanged
   feed costs one HTTP request and stops there.
2. Parses it into master events plus recurrence exceptions.
3. Drops anything outside the configured window, and anything Outlook marked
   cancelled.
4. Lists the events on the target calendar tagged with this source's id.
5. Creates, updates or deletes to make the two match. Each event carries a hash
   of its content, so unchanged events are not rewritten.
6. Applies moved or cancelled single occurrences as instance patches.

Every Google event id is derived deterministically from the source id and the
Outlook UID, which makes runs idempotent — and means that even if `sync.db` is
lost, the next run converges instead of duplicating everything.

### Things worth knowing

- **The window bounds series, not occurrences.** A recurring event that started
  in 2019 and still recurs is imported as one recurring event, so its old
  occurrences show up even beyond "days of history". That is the cost of not
  exploding recurrences into copies.
- **Unknown timezones fall back to UTC.** Exchange sometimes emits Windows zone
  names; the common ones are mapped to IANA names, and anything unrecognised is
  converted to absolute UTC timestamps. A recurring event in an unmapped zone
  can therefore drift by an hour across a DST change. If you hit this, open an
  issue with the `TZID` and it can be added to the map in `app/ics.py`.
- **Removing a source does not delete its events.** The Google calendar it
  wrote to stays as it is; delete that calendar in Google if you want it gone.
- **Google's API has daily quotas.** A calendar with tens of thousands of
  events, synced very frequently, can hit them. The default hourly interval and
  the change-detection hashing keep normal use far below the limits.

## Security notes

- The app holds an OAuth refresh token for your Google Calendar. **Set
  `APP_PASSWORD`.** The bundled compose file binds to `127.0.0.1` precisely so
  that an unconfigured instance is not exposed.
- The refresh token is encrypted at rest with a key in `DATA_DIR`. That protects
  it in database backups; it is not a defence against someone who can already
  read `DATA_DIR`. Back up both, or neither.
- If you expose this beyond localhost, put it behind a reverse proxy with TLS
  and update `APP_BASE_URL` (and the redirect URI in Google) to match. Sessions
  use a signed cookie; over plain HTTP that cookie is interceptable.
- To revoke access entirely: **Disconnect** in the UI, then remove the app at
  [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

## Troubleshooting

**"The URL did not return an iCalendar document"** — you copied the HTML link.
Go back to Outlook and take the one ending in `.ics`.

**"Feed returned HTTP 404"** — the publication was revoked or the calendar was
re-published, which mints a new URL. Publish again and update the source.

**"Google did not return a refresh token"** — Google only issues one on first
consent. Remove the app at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions)
and connect again.

**Events are an hour off after a DST change** — an unmapped Windows timezone.
See the timezone note above.

**Nothing happens on schedule** — check `docker compose logs -f sync`; the
scheduler logs its interval on startup and every run's outcome.

## Layout

```
app/
  main.py           FastAPI routes, OAuth callback, web UI
  sync.py           the sync engine (diffing, upserts, deletions)
  ics.py            feed fetching and iCalendar parsing
  google_client.py  OAuth handling and Calendar API helpers
  db.py             SQLite schema and queries
  scheduler.py      APScheduler interval job
  templates/        Jinja2 pages
tests/              pytest suite
```
