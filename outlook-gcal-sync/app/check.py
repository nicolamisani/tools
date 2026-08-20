"""Inspect an .ics file the way the sync engine would, without touching Google.

    python -m app.check calendar.ics

Prints what the app would make of the file: which calendar it came from, how
many events, and whether anything looks wrong. Event titles are withheld
unless --sample is given, so the output is safe to paste when asking for help.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ics import FeedError, looks_like_google_calendar, parse_calendar, in_window


def _fmt_size(n: int) -> str:
    return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def _fmt_day(value) -> str:
    return value.strftime("%Y-%m-%d")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.check",
        description="Check an .ics export before uploading it.",
    )
    parser.add_argument("path", type=Path, help="the .ics file to inspect")
    parser.add_argument(
        "--account", default="", help="your connected Google address, to catch "
        "an export of the wrong calendar"
    )
    parser.add_argument(
        "--past", type=int, default=30, help="days of history (default: 30)"
    )
    parser.add_argument(
        "--future", type=int, default=365, help="days ahead (default: 365)"
    )
    parser.add_argument(
        "--sample", type=int, default=0, metavar="N",
        help="also print the first N event titles (reveals private data)",
    )
    args = parser.parse_args(argv)

    if not args.path.exists():
        print(f"No such file: {args.path}", file=sys.stderr)
        return 2

    raw = args.path.read_bytes()
    try:
        parsed = parse_calendar(raw)
    except FeedError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        if b"BEGIN:VCALENDAR" not in raw[:4096]:
            print(
                "\n  This does not look like an iCalendar file at all. In "
                "Calendar.app\n  use File → Export → Export… (not Archive, "
                "which writes .icbu).",
                file=sys.stderr,
            )
        return 1

    masters = [e for e in parsed.events if not e.is_override]
    overrides = [e for e in parsed.events if e.is_override]
    recurring = [e for e in masters if e.is_recurring_master]
    all_day = [e for e in masters if e.start.is_all_day]
    cancelled = [e for e in masters if e.cancelled]

    print(f"File         {args.path.name} ({_fmt_size(len(raw))})")
    print(f"Produced by  {parsed.prodid or 'not stated'}")
    calendar_line = parsed.name or "not named"
    if parsed.description and parsed.description != parsed.name:
        calendar_line += f" — {parsed.description}"
    print(f"Calendar     {calendar_line}")
    print(f"Timezone     {parsed.default_tz or 'not declared'}")
    print()
    print(f"Events       {len(masters)}")
    print(f"  recurring  {len(recurring)} series")
    print(f"  exceptions {len(overrides)} moved or cancelled occurrences")
    print(f"  all-day    {len(all_day)}")
    if cancelled:
        print(f"  cancelled  {len(cancelled)} (would be skipped)")

    if masters:
        starts = [e.start.as_utc() for e in masters]
        print(f"Range        {_fmt_day(min(starts))} → {_fmt_day(max(starts))}")
        now = datetime.now(timezone.utc)
        window = (now - timedelta(days=args.past), now + timedelta(days=args.future))
        in_range = [e for e in masters if not e.cancelled and in_window(e, *window)]
        print(
            f"In window    {len(in_range)} would sync "
            f"({args.past} days back, {args.future} ahead)"
        )

    problems: list[str] = []
    reason = looks_like_google_calendar(parsed, args.account)
    if reason:
        problems.append(
            f"This looks like an export of your Google calendar — {reason}.\n"
            "  Select the Outlook/Exchange calendar in the sidebar and export again."
        )
    elif not args.account:
        print(
            "\n  Tip: pass --account you@gmail.com to check you did not export\n"
            "  the Google calendar by mistake."
        )
    if not masters:
        problems.append("The file contains no events at all.")

    print()
    if problems:
        for problem in problems:
            print(f"✗ {problem}")
    else:
        print("✓ No problems detected — this file is ready to upload.")

    if args.sample and masters:
        print(f"\nFirst {min(args.sample, len(masters))} events:")
        for event in masters[: args.sample]:
            when = _fmt_day(event.start.as_utc())
            mark = " (recurring)" if event.is_recurring_master else ""
            print(f"  {when}  {event.summary or '(no title)'}{mark}")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
