"""Generate a subscription calendar from the public UniSR room timetable."""
import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from html import unescape
import json
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid5, NAMESPACE_URL
from zoneinfo import ZoneInfo

BASE = "https://orario.unisr.it/default.asp"
ROME = ZoneInfo("Europe/Rome")
LOCATION = "Aula LEONARDO, DIBIT 1 – piano terra"


def source_url(day):
    return f"{BASE}?" + urlencode(dict(
        ext="ok", data=day.strftime("%d/%m/%Y"), id_palazzo=1,
        id_aula=31, ANNO_CORSO=5, lezioni="ok",
    ))


def plain(html):
    return " ".join(unescape(re.sub(r"<[^>]*>", "", html)).split())


def parse(page, day):
    requested = day.strftime("%d/%m/%Y")
    if not re.search(r'name="data"\s+value="' + requested + '"', page):
        raise ValueError(f"Data non confermata dal portale: {day}")
    if not re.search(r'<option value="31" selected>Leonardo \(DIBIT1', page, re.I):
        raise ValueError("Aula LEONARDO non confermata")
    if not re.search(r'<option value="5" selected>5</option>', page, re.I):
        raise ValueError("Anno di corso non confermato")
    _, marker, results = page.partition('name="succ"')
    if not marker or "</body>" not in results:
        raise ValueError("Pagina dei risultati incompleta")
    if "Nessun risultato" in results:
        return []
    if requested not in results or "Aula LEONARDO" not in results:
        raise ValueError("Intestazione dei risultati assente")

    events = []
    recognized = 0
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", results, re.I | re.S):
        cells = [plain(cell) for cell in re.findall(
            r"<td\b[^>]*>(.*?)</td>", row, re.I | re.S)]
        if len(cells) != 4 or not cells[1].casefold().startswith("lezione:"):
            continue
        hours, detail, course, year = cells
        start, end = re.fullmatch(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})", hours).groups()
        recognized += 1
        if year != "5" or not course.casefold().startswith("international md program"):
            continue
        title = detail.split(":", 1)[1].strip()
        teacher_match = re.search(r"\s*\(docente:\s*(.*?)\)$", title, re.I)
        teacher = teacher_match[1] if teacher_match else ""
        if teacher_match:
            title = title[:teacher_match.start()].strip()
        if not title or utc_time(day.isoformat(), start) >= utc_time(day.isoformat(), end):
            raise ValueError(f"Lezione incompleta: {day}")
        event = dict(day=day.isoformat(), start=start, end=end, title=title, teacher=teacher)
        if event not in events:
            events.append(event)
    if not recognized:
        raise ValueError(f"Struttura dei risultati inattesa per {day}")
    return events


def fetch(day):
    for attempt in range(3):
        try:
            with urlopen(source_url(day), timeout=20) as response:
                page = response.read().decode(
                    response.headers.get_content_charset() or "windows-1252", errors="replace")
            return parse(page, day)
        except (OSError, ValueError) as error:
            if attempt == 2:
                raise RuntimeError(f"UniSR {day}: {error}") from error


def group_key(event):
    return event["day"], event["title"].casefold()


def reconcile(old, current, first, last, keep_from, now):
    previous = defaultdict(dict)
    incoming = defaultdict(list)
    result = {}
    for uid, entry in old.items():
        day = entry["event"]["day"]
        if first <= day <= last:
            previous[group_key(entry["event"])][uid] = entry
        elif keep_from <= day:
            result[uid] = entry
    for event in current:
        incoming[group_key(event)].append(event)

    for key in previous.keys() | incoming.keys():
        candidates = previous[key]
        pending = []
        for event in incoming[key]:
            matches = [uid for uid, entry in candidates.items() if entry["event"] == event]
            if not matches:
                matches = [uid for uid, entry in candidates.items()
                           if entry["event"]["start"] == event["start"]]
            if len(matches) == 1:
                uid = matches[0]
                result[uid] = update(candidates.pop(uid), event, True, now)
            else:
                pending.append(event)
        if len(candidates) == len(pending) == 1:
            uid, entry = candidates.popitem()
            result[uid] = update(entry, pending.pop(), True, now)
        for event in pending:
            identity = json.dumps(event, sort_keys=True)
            uid = str(uuid5(NAMESPACE_URL, BASE + "#31/5/" + identity)) + "@unisr-imdp5"
            result[uid] = update(candidates.pop(uid, old.get(uid)), event, True, now)
        for uid, entry in candidates.items():
            result[uid] = update(entry, entry["event"], False, now)
    return result


def update(old, event, active, now):
    if old and old["event"] == event and old["active"] == active:
        return old
    return dict(event=event, active=active, stamp=now,
                sequence=old["sequence"] + 1 if old else 0)


def utc_time(day, hour):
    return datetime.fromisoformat(f"{day}T{hour}").replace(tzinfo=ROME).astimezone(timezone.utc)


def stamp(instant):
    return instant.strftime("%Y%m%dT%H%M%SZ")


def escape(value):
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold(line):
    parts = [""]
    size = 0
    for character in line:
        width = len(character.encode("utf-8"))
        if size + width > 75:
            parts.append(" ")
            size = 1
        parts[-1] += character
        size += width
    return "\r\n".join(parts)


def calendar(state):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//UniSR IMDP5//LEONARDO//IT",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:UniSR – IMDP 5", "X-WR-TIMEZONE:Europe/Rome"]
    for uid, entry in sorted(state.items(), key=lambda item: (item[1]["event"]["day"],
                                                            item[1]["event"]["start"], item[0])):
        if not entry["active"]:
            continue
        event = entry["event"]
        lines.extend([
            "BEGIN:VEVENT", "UID:" + uid, "DTSTAMP:" + entry["stamp"],
            f'SEQUENCE:{entry["sequence"]}',
            "DTSTART:" + stamp(utc_time(event["day"], event["start"])),
            "DTEND:" + stamp(utc_time(event["day"], event["end"])),
            "SUMMARY:" + escape(event["title"]),
            "LOCATION:" + escape(LOCATION),
        ])
        if event["teacher"]:
            lines.append("DESCRIPTION:" + escape("Docente: " + event["teacher"]))
        lines.extend(["URL:" + source_url(date.fromisoformat(event["day"])), "END:VEVENT"])
    lines.append("END:VCALENDAR")
    return ("\r\n".join(map(fold, lines)) + "\r\n").encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument("--output", type=Path, default=Path("site/imdp5.ics"))
    args = parser.parse_args()
    now = datetime.now(ROME)
    today = now.date()
    first = today - timedelta(days=7)
    last = today + timedelta(days=90)
    old = json.loads(args.state.read_text())["events"] if args.state.exists() else {}
    current = []
    day = first
    while day <= last:
        current.extend(fetch(day))
        day += timedelta(days=1)
    state = reconcile(old, current, first.isoformat(), last.isoformat(),
                      (today - timedelta(days=30)).isoformat(), stamp(now.astimezone(timezone.utc)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(calendar(state))
    args.state.write_text(json.dumps(dict(checked=today.isoformat(), events=state),
                                    indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"{len(current)} lezioni verificate dal {first} al {last}")


if __name__ == "__main__":
    main()
