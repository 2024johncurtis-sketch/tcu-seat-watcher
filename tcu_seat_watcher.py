#!/usr/bin/env python3
"""
TCU class seat watcher (multi-class, self-looping)
--------------------------------------------------
Polls the PUBLIC TCU class search (classes.tcu.edu, no login) for one or more
sections and pings you when any of them flips to OPEN.

Run modes:
  python tcu_seat_watcher.py          one pass, then exit (good for testing)
  python tcu_seat_watcher.py --loop   check every 5 min for ~5.5 hours, then exit
                                       (this is what GitHub Actions runs)

Add a class: copy a line in the CLASSES list below and change the 3 values.
"""

import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup

# ------------------------------- CONFIG -------------------------------
TERM = "4267"   # Fall 2026 (the ddlTerm value). Leave this for Fall '26.

# Watch as many classes as you want. To add one, copy a line, change the
# subject / course / section. The "note" is just a label for the alert.
CLASSES = [
    {"subject": "FINA", "course": "30153", "section": "055", "note": "Rodriguez"},
    {"subject": "FINA", "course": "30213", "section": "035", "note": "NICK - Peckham"},
    {"subject": "MANA", "course": "30153", "section": "050", "note": "LIAM - Chapman"},
    {"subject": "FINA", "course": "30213", "section": "055", "note": "MATTHEW FILICE - Peckham"},
]

# Where to send the alert (set as environment variables / GitHub secrets).
NTFY_TOPIC      = os.environ.get("NTFY_TOPIC", "")       # easiest: phone push
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")  # or a Discord channel

# Loop timing.
CHECK_EVERY_SECONDS = 300                 # check every 5 minutes
MAX_RUNTIME_SECONDS = 5 * 3600 + 30 * 60  # ~5.5 h, stays under GitHub's 6h cap
REMIND_SECONDS      = 1800                # re-ping every 30 min while still open
# ----------------------------------------------------------------------

BASE_URL = "https://classes.tcu.edu/default.aspx"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# A real result row ends like: "... TR 14:00-15:20 Closed 45 45 0 0"
# (Status word + 4 ints: Enr, Max, RsvMax, WaitMax). The search form's
# "Status: Any Open Closed" is NOT followed by 4 ints, so this won't false-match.
RESULT_RE = re.compile(r"\b(Open|Closed)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\b", re.I)


def get_hidden_fields(session):
    """Load the search page and pull the ASP.NET tokens it expects back."""
    r = session.get(BASE_URL, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    def val(name):
        tag = soup.find("input", {"name": name})
        return tag["value"] if tag and tag.has_attr("value") else ""

    return {
        "__VIEWSTATE": val("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": val("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": val("__EVENTVALIDATION"),
    }


def run_search(session, hidden, subject, course, section):
    """Submit the search form for one section; return the results HTML."""
    data = {
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": hidden["__VIEWSTATE"],
        "__VIEWSTATEGENERATOR": hidden["__VIEWSTATEGENERATOR"],
        "__EVENTVALIDATION": hidden["__EVENTVALIDATION"],
        "ddlTerm": TERM,
        "ddlSession": "ANY",
        "ddlLocation": "ANY",
        "ddlSubject": subject,
        "txtCrsNumber": course,
        "txtSection": section,
        "ddlAttribute": "ANY",
        "ddlLevel": "ANY",
        "ddlDay": "ANY",
        "ddlStartTime": "ANY",
        "ddlEndtime": "2000",
        "btnSearch": "Search",
        "hdnShowBldg": "Y",
    }
    headers = {
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://classes.tcu.edu",
        "Referer": BASE_URL,
    }
    r = session.post(BASE_URL, data=data, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def page_text(results_html):
    """Whole page as one normalized string."""
    soup = BeautifulSoup(results_html, "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


def result_slice(text):
    """A short readable piece of the actual result row (for the log)."""
    m = RESULT_RE.search(text)
    if not m:
        return "(section row not found)"
    return text[max(0, m.start() - 160): m.end()].strip()


def judge_open(text):
    """Read the real Status column. OPEN / CLOSED / UNKNOWN."""
    m = RESULT_RE.search(text)
    if not m:
        return "UNKNOWN"
    status_word = m.group(1).lower()
    enrolled, capacity = int(m.group(2)), int(m.group(3))
    if status_word == "open" or enrolled < capacity:
        return "OPEN"
    return "CLOSED"


def notify(title, message):
    sent = False
    if NTFY_TOPIC:
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": title.encode("ascii", "ignore").decode(),
                         "Priority": "high", "Tags": "rotating_light"},
                timeout=30,
            )
            sent = True
        except Exception as e:
            print(f"ntfy failed: {e}", file=sys.stderr)
    if DISCORD_WEBHOOK:
        try:
            requests.post(DISCORD_WEBHOOK,
                          json={"content": f"**{title}**\n{message}"}, timeout=30)
            sent = True
        except Exception as e:
            print(f"discord failed: {e}", file=sys.stderr)
    if not sent:
        print(f"[no notifier set] {title}: {message}", file=sys.stderr)


def check_one(session, cls, state):
    """Search one class; alert when it opens (and remind while it stays open)."""
    subject, course, section = cls["subject"], cls["course"], cls["section"]
    note = cls.get("note", "")
    label = f"{subject} {course}-{section}" + (f" ({note})" if note else "")

    try:
        hidden = get_hidden_fields(session)
        html = run_search(session, hidden, subject, course, section)
        text = page_text(html)
        status = judge_open(text)
    except Exception as e:
        print(f"{label}: ERROR {e}", file=sys.stderr)
        return

    print(f"{label}: status = {status}")
    print(f"    row: {result_slice(text)}")

    if status == "UNKNOWN":
        return  # couldn't read it -> never alert on a guess

    st = state.setdefault(label, {"status": None, "notified_at": 0.0})
    now = time.time()
    if status == "OPEN":
        just_opened = st["status"] != "OPEN"
        due_reminder = (now - st["notified_at"]) >= REMIND_SECONDS
        if just_opened or due_reminder:
            notify(
                f"{label} just OPENED",
                f"A seat opened in {label}. Log into Purple Schedule Builder and "
                f"SWAP it in right now before it's gone.",
            )
            st["notified_at"] = now
    st["status"] = status


def run_once(session, state):
    for cls in CLASSES:
        check_one(session, cls, state)


def main():
    session = requests.Session()
    state = {}

    if "--loop" not in sys.argv:
        run_once(session, state)
        return 0

    start = time.time()
    while time.time() - start < MAX_RUNTIME_SECONDS:
        run_once(session, state)
        remaining = MAX_RUNTIME_SECONDS - (time.time() - start)
        if remaining <= 0:
            break
        time.sleep(min(CHECK_EVERY_SECONDS, remaining))
    print("loop window finished; exiting so the next scheduled run takes over")
    return 0


if __name__ == "__main__":
    sys.exit(main())
