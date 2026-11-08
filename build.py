#!/usr/bin/env python3
"""Build subscribable iCalendar feeds for Oshman Family JCC (Palo Alto) group
fitness classes, from the GroupEx Pro schedule that backs
https://fitness.paloaltojcc.org/pages/group-classes.

Standard library only. Writes one .ics per class category plus all.ics and an
index.html into the output directory (default: ./site).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ACCOUNT = 946
API = "https://www.groupexpro.com/schedule/embed/json.php"
SCHEDULE_PAGE = "https://fitness.paloaltojcc.org/pages/group-classes"
TZ = ZoneInfo("America/Los_Angeles")
ADDRESS = "Oshman Family JCC, 3921 Fabian Way, Palo Alto, CA 94303"
DAYS_BACK = 14
DAYS_AHEAD = 60
CAL_PREFIX = "PAJCC"

# Notices (holiday schedules etc.) in these categories go into every feed
# instead of getting a feed of their own.
EVERY_FEED = {"General"}

# Category -> (feed slug, display name). Unknown categories get a slug
# automatically, so new categories added by the gym still get a feed.
KNOWN = {
    "Yoga": ("yoga", "Yoga"),
    "Pilates": ("pilates", "Pilates (Mat)"),
    "Pilates Reformer ($)": ("pilates-reformer", "Pilates Reformer ($)"),
    "Strength": ("strength", "Strength"),
    "Cardio & HIIT": ("cardio-hiit", "Cardio & HIIT"),
    "Dance Fitness": ("dance", "Dance Fitness"),
    "Mobility and Recovery": ("mobility-recovery", "Mobility & Recovery"),
    "Aqua": ("aqua", "Aqua"),
    "Sports and Recreation": ("sports-recreation", "Sports & Recreation"),
    "Youth Programming": ("youth", "Youth Programming"),
}


def fetch_json(params: dict) -> object:
    url = API + "?" + "&".join(
        k if v is None else f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items()
    )
    req = urllib.request.Request(url, headers={"User-Agent": "pajcc-class-calendars"})
    last_err = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - retry any transient failure
            last_err = e
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last_err}")


def clean(s: str) -> str:
    s = re.sub(r"<br\s*/?>", " ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def parse_time(day: datetime, t: str) -> datetime:
    tm = datetime.strptime(t.strip().lower(), "%I:%M%p")
    return day.replace(hour=tm.hour, minute=tm.minute, tzinfo=TZ)


def parse_row(row: list) -> dict:
    date_s, time_s, title, _, room, category, instructor_html = row[:7]
    node_m = re.search(r'alt="(\d+)"', row[8]) if len(row) > 8 else None
    day = datetime.strptime(date_s, "%A, %B %d, %Y")
    all_day = "-" not in time_s
    if all_day:  # e.g. "All Day" holiday notices
        start = day.replace(tzinfo=TZ)
        end = start + timedelta(days=1)
    else:
        start_s, end_s = time_s.split("-")
        start = parse_time(day, start_s)
        end = parse_time(day, end_s)
        if end <= start:
            end += timedelta(days=1)

    sub_m = re.search(r"\(sub for ([^)]*)\)", clean(instructor_html))
    instructor = clean(re.sub(r"<span[^>]*subbed.*?</span>", "", instructor_html))
    title = clean(title)
    canceled = bool(re.match(r"(?i)cancel+ed\b", title))
    return {
        "start": start,
        "end": end,
        "title": title,
        "room": clean(room),
        "category": clean(category),
        "instructor": instructor,
        "sub_for": sub_m.group(1).strip() if sub_m else None,
        "canceled": canceled,
        "all_day": all_day,
        "node": node_m.group(1) if node_m else None,
    }


def ics_escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Fold to 75 octets per RFC 5545."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def event_uid(ev: dict) -> str:
    key = f"{ev['start'].date()}|{ev['node'] or ev['title']}|{ev['start'].strftime('%H%M')}"
    return hashlib.sha1(key.encode()).hexdigest()[:20] + "@pajcc-class-calendars"


def build_calendar(name: str, events: list[dict], now: datetime) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//pajcc-class-calendars//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)}",
        "X-WR-TIMEZONE:America/Los_Angeles",
        f"X-WR-CALDESC:{ics_escape('Oshman Family JCC group fitness classes. Source: ' + SCHEDULE_PAGE)}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    stamp = utc(now)
    for ev in events:
        instr = ev["instructor"]
        if ev["sub_for"]:
            instr += f" (sub for {ev['sub_for']})"
        desc = "\n".join(
            x
            for x in [
                f"Instructor: {instr}" if instr else "",
                f"Room: {ev['room']}" if ev["room"] else "",
                f"Category: {ev['category']}",
                "",
                f"Schedule: {SCHEDULE_PAGE}",
            ]
        )
        summary = ev["title"]
        if ev["sub_for"] and not ev["canceled"]:
            summary += " (sub)"
        loc = f"{ev['room']}, {ADDRESS}" if ev["room"] else ADDRESS
        lines += [
            "BEGIN:VEVENT",
            f"UID:{event_uid(ev)}",
            f"DTSTAMP:{stamp}",
            *(
                [
                    f"DTSTART;VALUE=DATE:{ev['start']:%Y%m%d}",
                    f"DTEND;VALUE=DATE:{ev['end']:%Y%m%d}",
                ]
                if ev["all_day"]
                else [f"DTSTART:{utc(ev['start'])}", f"DTEND:{utc(ev['end'])}"]
            ),
            f"SUMMARY:{ics_escape(summary)}",
            f"LOCATION:{ics_escape(loc)}",
            f"DESCRIPTION:{ics_escape(desc)}",
            f"URL:{SCHEDULE_PAGE}",
            f"CATEGORIES:{ics_escape(ev['category'])}",
            f"STATUS:{'CANCELLED' if ev['canceled'] else 'CONFIRMED'}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def build_index(feeds: list[tuple[str, str, int]], base_url: str, now: datetime) -> str:
    rows = []
    for slug, name, count in feeds:
        url = f"{base_url}{slug}.ics"
        webcal = re.sub(r"^https?://", "webcal://", url)
        gcal = "https://calendar.google.com/calendar/r?cid=" + urllib.parse.quote(webcal, safe="")
        rows.append(
            f"<tr><td>{html.escape(name)}</td><td class=n>{count}</td>"
            f'<td><a href="{html.escape(gcal)}">Add to Google Calendar</a></td>'
            f'<td><a href="{html.escape(webcal)}">Apple / Outlook</a></td>'
            f'<td><input readonly value="{html.escape(url)}" onclick="this.select()"></td></tr>'
        )
    updated = now.astimezone(TZ).strftime("%b %-d, %Y %-I:%M %p %Z")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PAJCC Class Calendars</title>
<style>
:root {{ color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#ddd; --accent:#1a5fb4; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161616; --fg:#eee; --muted:#999; --line:#333; --accent:#78aeed; }} }}
body {{ background:var(--bg); color:var(--fg); font:16px/1.5 system-ui,sans-serif; max-width:960px; margin:0 auto; padding:24px 16px; }}
a {{ color:var(--accent); }}
p.m {{ color:var(--muted); }}
.wrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; }}
td, th {{ border-bottom:1px solid var(--line); padding:8px 10px; text-align:left; white-space:nowrap; }}
td.n {{ text-align:right; color:var(--muted); }}
input {{ width:22em; font:13px ui-monospace,monospace; background:transparent; color:inherit; border:1px solid var(--line); padding:4px; }}
</style></head><body>
<h1>Oshman Family JCC class calendars</h1>
<p>Subscribable calendars of the <a href="{SCHEDULE_PAGE}">group fitness schedule</a>, one per class category.
Feeds cover the past {DAYS_BACK} days and next {DAYS_AHEAD} days and are refreshed every few hours
(Google Calendar re-checks subscribed calendars roughly every 12&ndash;24 hours).
Substitutes are marked &ldquo;(sub)&rdquo;; canceled classes stay on the calendar marked canceled.</p>
<p>In Google Calendar you can also go to <b>Other calendars &rarr; + &rarr; From URL</b> and paste a URL below.</p>
<div class="wrap"><table>
<tr><th>Feed</th><th>Classes</th><th></th><th></th><th>URL</th></tr>
{''.join(rows)}
</table></div>
<p class="m">Last updated {updated}. Unofficial; not affiliated with the Oshman Family JCC.
Always check the official schedule before heading over.</p>
</body></html>
"""


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "site")
    # Pages may report http:// when HTTPS isn't enforced; always link https.
    base_url = re.sub(r"^http://", "https://", sys.argv[2] if len(sys.argv) > 2 else "")
    out.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=DAYS_BACK)
    end = today + timedelta(days=DAYS_AHEAD)

    categories = fetch_json({"categories": None, "format": "json", "a": ACCOUNT})
    data = fetch_json(
        {
            "schedule": None,
            "format": "json",
            "a": ACCOUNT,
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
        }
    )
    rows = data["aaData"]
    if not rows:
        print("no classes returned; refusing to publish empty feeds", file=sys.stderr)
        return 1

    events = sorted((parse_row(r) for r in rows), key=lambda e: (e["start"], e["title"]))

    cat_names = {clean(c) for c in (categories.values() if isinstance(categories, dict) else [])}
    cat_names |= {e["category"] for e in events}
    cat_names -= EVERY_FEED

    feeds = [("all", "All classes", len(events))]
    (out / "all.ics").write_text(build_calendar(f"{CAL_PREFIX}: All classes", events, now), newline="")
    for cat in sorted(cat_names, key=lambda c: KNOWN.get(c, (slugify(c), c))[1]):
        slug, name = KNOWN.get(cat, (slugify(cat), cat))
        evs = [e for e in events if e["category"] == cat or e["category"] in EVERY_FEED]
        (out / f"{slug}.ics").write_text(build_calendar(f"{CAL_PREFIX}: {name}", evs, now), newline="")
        feeds.append((slug, name, len(evs)))

    (out / "index.html").write_text(build_index(feeds, base_url, now))
    (out / ".nojekyll").write_text("")
    for slug, name, count in feeds:
        print(f"{slug}.ics\t{count}\t{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
