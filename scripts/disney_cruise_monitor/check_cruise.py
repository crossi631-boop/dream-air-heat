#!/usr/bin/env python3
"""Checks the Disney Cruise Line search page for the filtered results the
user cares about (Southampton, England / 8-13 nights / Disney Wish) and
sends a push notification via ntfy.sh when the search stops returning zero
cruises.

Runs headless via Playwright because the results are rendered client-side;
a plain HTTP fetch of the URL would only see the empty app shell.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

URL = (
    "https://disneycruise.disney.go.com/cruises-destinations/list"
    "#southampton-england,8-to-13,disney-wish"
)
STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
REMINDER_INTERVAL_SECONDS = 24 * 3600
ERROR_ALERT_THRESHOLD = 3


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "found": False,
            "last_alert_found_at": None,
            "consecutive_errors": 0,
            "error_alerted": False,
        }


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def notify(title, message, priority="default", tags=None):
    if not NTFY_TOPIC:
        print(f"NTFY_TOPIC not set; would have sent: [{title}] {message}")
        return
    req = urllib.request.Request(NTFY_URL, data=message.encode("utf-8"), method="POST")
    req.add_header("Title", title)
    req.add_header("Priority", priority)
    if tags:
        req.add_header("Tags", tags)
    try:
        urllib.request.urlopen(req, timeout=15)
        print(f"Sent notification: [{title}] {message}")
    except Exception as e:
        print(f"Failed to send ntfy notification: {e}")


def fetch_body_text():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)
        # The results grid loads via client-side JS after the initial
        # network-idle event; give it a bit more time to render.
        page.wait_for_timeout(8000)
        body_text = page.inner_text("body")
        browser.close()
    return body_text


def detect(body_text):
    """Returns (found: bool, count: int|None, evidence: str)."""
    # Primary signal: an explicit count like "0 Cruises" / "3 Itineraries".
    m = re.search(r"(\d+)\s+(cruises?|itinerar(?:y|ies))\b", body_text, re.IGNORECASE)
    if m:
        count = int(m.group(1))
        return count > 0, count, m.group(0)

    # Explicit "no results" style message.
    if re.search(r"\bno\s+(cruises?|itinerar(?:y|ies))\b", body_text, re.IGNORECASE):
        return False, 0, "explicit no-results message"

    # Fallback: itinerary-card text mentioning both ports, with no count found.
    if re.search(r"southampton", body_text, re.IGNORECASE) and re.search(
        r"new york", body_text, re.IGNORECASE
    ):
        return True, None, "Southampton + New York mentioned, no explicit count parsed"

    return False, None, "no count or fallback signal matched"


def main():
    state = load_state()

    try:
        body_text = fetch_body_text()
    except Exception as e:
        state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
        print(f"ERROR checking page (consecutive failures={state['consecutive_errors']}): {e}")
        if state["consecutive_errors"] >= ERROR_ALERT_THRESHOLD and not state.get(
            "error_alerted"
        ):
            notify(
                "Disney cruise monitor needs attention",
                f"Failed {state['consecutive_errors']} times in a row: {e}",
                priority="high",
                tags="warning",
            )
            state["error_alerted"] = True
        save_state(state)
        sys.exit(1)

    state["consecutive_errors"] = 0
    state["error_alerted"] = False

    found, count, evidence = detect(body_text)
    print(f"found={found} count={count} evidence={evidence!r}")

    now_iso = datetime.now(timezone.utc).isoformat()
    was_found = state.get("found", False)

    if found and not was_found:
        notify(
            "Disney Wish transatlantic cruise is listed!",
            f"The Southampton -> New York search now shows results ({evidence}). {URL}",
            priority="urgent",
            tags="rotating_light,ship",
        )
        state["last_alert_found_at"] = now_iso
    elif found and was_found:
        last = state.get("last_alert_found_at")
        stale = True
        if last:
            try:
                elapsed = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(last)
                ).total_seconds()
                stale = elapsed > REMINDER_INTERVAL_SECONDS
            except ValueError:
                stale = True
        if stale:
            notify(
                "Reminder: Disney Wish TA cruise still listed",
                f"Still showing as of now ({evidence}). {URL}",
                priority="default",
                tags="ship",
            )
            state["last_alert_found_at"] = now_iso
    elif not found and was_found:
        notify(
            "Disney Wish TA cruise no longer listed",
            "The search that was previously showing results is back to showing none.",
            priority="default",
            tags="warning",
        )

    state["found"] = found
    save_state(state)


if __name__ == "__main__":
    main()
