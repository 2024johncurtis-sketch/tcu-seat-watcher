#!/usr/bin/env python3
"""
TCU class seat watcher
----------------------
Polls the PUBLIC TCU class search (classes.tcu.edu, no login) for one section
and pings you when it flips from full/closed to OPEN.

Configured below for: FINA 30153 section 055 (Rodriguez), Fall 2026.
Change the CONFIG block to watch a different class.

Run a one-off check + see what it scraped:   python tcu_seat_watcher.py --test
Normal run (used by GitHub Actions):          python tcu_seat_watcher.py
"""

import os
import re
import sys
import requests
from bs4 import BeautifulSoup

# ----------------------------- CONFIG --------------------------------
TERM    = "4267"     # Fall 2026  (this is the ddlTerm value from your search)
SUBJECT = "FINA"     # department code
COURSE  = "30153"    # course number
SECTION = "055"      # the section you want (Rodriguez, CRN 71859)

# Where to send the alert. Set ONE (or both) as environment variables.
NTFY_TOPIC      = os.environ.get("NTFY_TOPIC", "")       # easiest: phone push
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")  # or a Discord channel
# ---------------------------------------------------------------------

BASE_URL = "https://classes.tcu.edu/default.aspx"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


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


def run_search(session, hidden):
    """Submit the search form for the configured section."""
    data = {
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": hidden["__VIEWSTATE"],
        "__VIEWSTATEGENERATOR": hidden["__VIEWSTATEGENERATOR"],
        "__EVENTVALIDATION": hidden["__EVENTVALIDATION"],
        "ddlTerm": TERM,
        "ddlSession": "ANY",
        "ddlLocation": "ANY",
        "ddlSubject": SUBJECT,
        "txtCrsNumber": COURSE,
        "txtSection": SECTION,
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


# TCU's result row ends like: "... TR 14:00-15:20 Closed 45 45 0 0"
# i.e. the Status word (Open/Closed) followed by 4 ints: Enr, Max, RsvMax, WaitMax.
# The search form's "Status: Any Open Closed" is NOT followed by those 4 ints,
# so this pattern only matches a real result row.
RESULT_RE = re.compile(r"\b(Open|Closed)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\b", re.I)


def find_section_text(results_html):
    """Return a short readable slice of the actual result row (for the log)."""
    text = page_text(results_html)
    m = RESULT_RE.search(text)
    if not m:
        return []
    start = max(0, m.start() - 160)
    return [text[start:m.end()].strip()]


def judge_open(results_html):
    """
    Read the real Status column. OPEN / CLOSED / UNKNOWN.
    Trusts the Status word, and treats Enrolled < Max as open too.
    UNKNOWN (couldn't find the row) never triggers an alert.
    """
    text = results_html if isinstance(results_html, str) else ""
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
                # Title header must be plain ASCII; the Tags render the icon.
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


def main():
    label = f"{SUBJECT} {COURSE}-{SECTION}"

    session = requests.Session()
    hidden = get_hidden_fields(session)
    html = run_search(session, hidden)
    text = page_text(html)
    texts = find_section_text(html)
    status = judge_open(text)

    print(f"{label}: status = {status}")
    # Dump the actual result row, so the reading can always be sanity-checked.
    print(">>>> RAW SECTION TEXT (copy this whole block to Claude) >>>>")
    print("\n".join(texts) if texts else "(section row not found)")
    print("<<<< END RAW SECTION TEXT <<<<")

    if status == "OPEN":
        notify(
            f"{label} just OPENED",
            f"A seat opened in {label} (Rodriguez). Log into Purple Schedule "
            f"Builder and SWAP it in right now before it's gone.",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
