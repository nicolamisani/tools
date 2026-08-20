# Outlook → Google Calendar sync

A small self-hosted service that mirrors a published Outlook calendar into
Google Calendar. It polls the Outlook ICS feed on a schedule and keeps a
dedicated Google calendar in step: new events appear, edits follow, and events
deleted in Outlook disappear from Google.

One-way only. Nothing is ever written back to Outlook.


## Status: paused (August 2026)

The service is complete and tested. It is **not** in day-to-day use, because
neither automatic route into a locked-down Microsoft 365 tenant is open:

- **Published ICS URL** — the tenant's Exchange sharing policy disables calendar
  publishing, so no feed URL can be created.
- **Microsoft Graph** — since Microsoft's [secure-by-default consent change][mc]
  (rolled out late 2025), delegated access to Outlook Calendar requires *admin*
  consent, for Graph and for the legacy protocols (EWS, ActiveSync, IMAP) alike.
  A user cannot self-approve it.

That left the uploaded-file route, which works and is shipped — but the export
end could not be automated. On macOS, Outlook exports `.olm` rather than
iCalendar, Calendar.app's File → Export is GUI-only with no scriptable
equivalent, and AppleScript support in current Outlook for Mac is curtailed.
Exporting and uploading by hand is not worth doing on a schedule, so the project
is parked rather than finished.

**What would unblock it**, cheapest first:

1. An admin enables calendar publishing for the mailbox — one Exchange sharing
   policy change. The URL source then works as originally designed.
2. An admin grants consent for a self-registered Entra app with delegated
   `Calendars.Read`. Needs a Graph source type, which does not exist yet.
3. A Windows machine with Outlook desktop — COM automation can export the
   calendar unattended and `PUT` it to `/api/sources/<id>/upload`, which is
   already built and token-authenticated.

Everything below describes the working software. `python -m app.check file.ics`
inspects an export without needing any of the above.

[mc]: https://mc.merill.net/message/MC1163922


## What it does

- Imports a **published Outlook calendar** (`.ics` link) — no Azure app
  registration, no admin consent, works with outlook.com and most work accounts.
- Or takes an **uploaded `.ics` file**, for locked-down tenants where publishing
  is disabled. Drop it in the browser, or push it from a script.
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
- If your organisation has disabled calendar publishing, the URL route is
  closed. Use the uploaded-file source instead — see *If publishing is
  disabled* below. (Microsoft Graph is not an escape hatch here: since late
  2025 its default consent policy requires an admin to approve third-party
  access to Outlook Calendar, via Graph *and* the legacy protocols.)

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
   address under **Test users**. Then read *Publishing your OAuth app* below —
   it decides whether you reconnect weekly or once.
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

### Publishing your OAuth app

While your consent screen sits in **Testing**, Google revokes refresh tokens
after **7 days**. The sync then fails until you press *Connect Google account*
again — the dashboard will say so in as many words.

To stop that, publish the app:

> Google Cloud Console → **APIs & Services → OAuth consent screen** → under
> *Publishing status*, **Publish app**. (In the newer console this lives under
> **Google Auth Platform → Audience**.)

This app requests `auth/calendar`, which Google classifies as a **sensitive**
scope. An unverified app that is published to production shows a "Google hasn't
verified this app" interstitial the first time you connect — take
**Advanced → Go to … (unsafe)** to continue — and the project is capped at 100
authorised users for its lifetime. For a personal instance neither matters.
Going through Google's verification review is only worth it if you intend to
hand this to other people.

If publishing turns out to demand more than you want to do, staying in Testing
costs nothing but a weekly click on *Connect Google account*.

### 3. Run it

```bash
cp .env.example .env
# fill in APP_PASSWORD, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
docker compose up -d
```

Open <http://localhost:8080>, sign in, and click **Connect Google account**.

### If publishing is disabled

Many Microsoft 365 tenants switch calendar publishing off, which greys out the
option in Outlook. In that case add the calendar as an **uploaded file** source
instead: it needs no tenant permissions at all, because you export the data
yourself with a client you are already signed in to.

**Exporting on a Mac.** Open **Calendar.app**, select the calendar in the
sidebar, then **File → Export → Export…**. That writes a single `.ics` holding
the whole calendar.

> Outlook for Mac's own File → Export produces a `.olm` archive, which is not
> iCalendar and will be rejected. Calendar.app's **Calendar Archive** (`.icbu`)
> is likewise not an `.ics` — take *Export*, not *Archive*.

**Getting the file in.** Either drop it on the source's page in the browser, or
push it over HTTP with the token shown there:

```bash
curl -T calendar.ics \
  -H "Authorization: Bearer ocs_…" \
  http://localhost:8080/api/sources/1/upload
```

Every upload triggers a sync. Anything on your machine that can produce an
`.ics` and run a command — a launchd job, an Automator action, a Shortcut — can
therefore keep this current without you touching the browser. The scheduler is
irrelevant for these sources: re-running against an unchanged file does nothing,
so the upload *is* the trigger.

> **Note:** this is not the same as Google Calendar's own *Import* button, and
> the app's tolerance for a file is not Google's. The app parses the `.ics`
> itself and creates events through the API, so exports that Google's importer
> rejects can still sync fine here.

**Checking a file before you upload it.** The app ships a preflight command
that reads a file exactly as the sync engine would, without needing Google
credentials, a database or any configuration:

```bash
python -m app.check ~/Desktop/work.ics --account you@gmail.com
# or, with only Docker:
docker compose run --rm -v "$PWD:/host" sync python -m app.check /host/work.ics
```

It reports which calendar the file came from, how many events and recurring
series it holds, how many fall inside your sync window, and whether anything
looks wrong — exiting non-zero if so. Event titles are withheld unless you pass
`--sample N`, so the output is safe to paste when asking for help.

**Picking the right calendar.** In the Calendar.app sidebar your Google and
Exchange accounts sit next to each other, and exporting the wrong one is easy.
An upload that looks like an export of the Google account you connected is
rejected rather than synced — round-tripping it would duplicate your whole
calendar into itself. If you want to check a file by hand, look at its header:
`X-WR-CALNAME` and `X-WR-CALDESC` name the calendar it came from.

**The truncation guard.** A published URL is always a complete snapshot; a
hand-made export may not be. So for file sources, a run that would remove more
than a quarter of the events it previously wrote stops and reports `blocked`
instead of deleting — additions and edits are still applied. If the shrink was
intentional, press **Apply deletions**; if it was a partial export, upload a
complete file and the block clears itself.

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
Windows timezone names, recurrence rules and exceptions), a realistic
Apple Calendar export fixture (VTIMEZONE, VALARM, `X-APPLE-*` properties,
folded lines, escaped text), and the sync engine against an in-memory stand-in
for the Calendar API — including that events it did not create are never
deleted, and that a truncated upload cannot wipe a calendar.

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

**"Google sign-in has expired or been revoked"** — if this shows up roughly
weekly, your consent screen is still in Testing. See *Publishing your OAuth
app*. Press **Connect Google account** to resume in the meantime.

**Events are an hour off after a DST change** — an unmapped Windows timezone.
See the timezone note above.

**Nothing happens on schedule** — check `docker compose logs -f sync`; the
scheduler logs its interval on startup and every run's outcome.

## Layout

```
app/
  main.py           FastAPI routes, OAuth callback, web UI
  sync.py           the sync engine (diffing, upserts, deletions)
  ics.py            feed fetching, iCalendar parsing, window filtering
  check.py          preflight CLI for inspecting an .ics file
  google_client.py  OAuth handling and Calendar API helpers
  db.py             SQLite schema and queries
  scheduler.py      APScheduler interval job
  templates/        Jinja2 pages
tests/              pytest suite
```
