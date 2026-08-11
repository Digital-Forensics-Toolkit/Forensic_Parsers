#!/usr/bin/env python3
"""
DFIR-IRIS: List and bulk-delete timeline events for a given case.

Workflow:
  1. Fetches all timeline events for a case.
  2. Optionally filters them (by tag substring, source substring, or title substring).
  3. Prints a preview table of what matched.
  4. Only deletes after you type "DELETE" to confirm (dry-run by default).

IMPORTANT before running:
  - Fill in IRIS_HOST and API_KEY below.
  - Listing uses case/timeline/events/list?cid=X — confirmed working against
    this instance via curl.
  - Deletion defaults to the same-generation POST .../delete/{id}?cid=X call,
    but this hasn't been curl-confirmed yet on this instance. Test ONE delete
    manually first (see comment near EVENT_DELETE_MODE below) before trusting
    the bulk path. If it 403s, flip EVENT_DELETE_MODE to "v2" to use the
    newer DELETE /api/v2/cases/{case_identifier}/events/{identifier} call.
  - Test on a case you don't mind experimenting with, or run in DRY_RUN mode
    (default) first and eyeball the preview carefully before disabling it.
  - Deletions in IRIS are immediate and permanent. Make sure you have a DB
    backup before running this for real.
"""

import sys
import requests
import urllib3

# ─────────────────────────── CONFIG ───────────────────────────
IRIS_HOST = "https://your-iris-host/"        # trailing slash matters
API_KEY = "YOUR_API_KEY_HERE"
VERIFY_TLS = False                           # True if you have a valid cert

# Set to False only once you've reviewed the preview and are sure.
DRY_RUN = True

# Confirmed working against this instance via curl:
#   curl -kv --url ".../case/timeline/events/list?cid=17" --header "Authorization: Bearer ..."
EVENTS_LIST_PATH = "case/timeline/events/list"

# NOT YET CONFIRMED against this instance. The docs list this as deprecated
# in favor of DELETE /api/v2/cases/{case_identifier}/events/{identifier}.
# Test ONE delete manually via curl before trusting this in bulk:
#   curl -kv -X POST --url ".../case/timeline/events/delete/<event_id>?cid=17" --header "Authorization: Bearer ..."
# If that 403s/404s, switch EVENT_DELETE_MODE below to "v2".
EVENT_DELETE_PATH_V1 = "case/timeline/events/delete"   # POST .../delete/{event_id}?cid=X
EVENT_DELETE_MODE = "v1"   # "v1" or "v2"
# ────────────────────────────────────────────────────────────────

if not VERIFY_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
})
session.verify = VERIFY_TLS


def fetch_events(case_id: int):
    """Fetch all timeline events for a case using the confirmed-working
    events/list endpoint. Filtering happens client-side afterward."""
    url = f"{IRIS_HOST.rstrip('/')}/{EVENTS_LIST_PATH}"
    resp = session.get(url, params={"cid": case_id})
    resp.raise_for_status()
    payload = resp.json()

    # IRIS wraps responses as {"status": "success", "data": {...}} or
    # sometimes {"status": "success", "data": [...]} depending on version.
    data = payload.get("data", payload)
    if isinstance(data, dict) and "timeline" in data:
        tl = data["timeline"]
        events = tl.get("timeline", tl) if isinstance(tl, dict) else tl
    else:
        events = data

    if not isinstance(events, list):
        raise RuntimeError(f"Unexpected response shape, got: {payload}")

    return events


def matches_filter(event: dict, tag_contains, source_contains, title_contains):
    if tag_contains:
        tags = (event.get("event_tags") or "").lower()
        if tag_contains.lower() not in tags:
            return False
    if source_contains:
        source = (event.get("event_source") or "").lower()
        if source_contains.lower() not in source:
            return False
    if title_contains:
        title = (event.get("event_title") or "").lower()
        if title_contains.lower() not in title:
            return False
    return True


def preview(events):
    print(f"\n{'ID':<6} {'Date':<20} {'Title':<40} {'Source':<15} {'Tags'}")
    print("-" * 110)
    for e in events:
        eid = e.get("event_id", "?")
        date = (e.get("event_date") or "")[:19]
        title = (e.get("event_title") or "")[:38]
        source = (e.get("event_source") or "")[:13]
        tags = e.get("event_tags") or ""
        print(f"{eid:<6} {date:<20} {title:<40} {source:<15} {tags}")
    print("-" * 110)
    print(f"Total matched: {len(events)}\n")


def delete_events(case_id: int, event_ids):
    for eid in event_ids:
        if EVENT_DELETE_MODE == "v1":
            url = f"{IRIS_HOST.rstrip('/')}/{EVENT_DELETE_PATH_V1}/{eid}"
            resp = session.post(url, params={"cid": case_id})
        elif EVENT_DELETE_MODE == "v2":
            url = f"{IRIS_HOST.rstrip('/')}api/v2/cases/{case_id}/events/{eid}"
            resp = session.delete(url)
        else:
            raise ValueError(f"Unknown EVENT_DELETE_MODE: {EVENT_DELETE_MODE}")

        status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        print(f"  event_id={eid} -> HTTP {status} -> {body}")


def main():
    try:
        case_id = int(input("Case ID: ").strip())
    except ValueError:
        print("Case ID must be a number.")
        sys.exit(1)

    print("\nOptional filters (press Enter to skip any):")
    tag_contains = input("  Tag contains: ").strip() or None
    source_contains = input("  Source contains: ").strip() or None
    title_contains = input("  Title contains: ").strip() or None

    print("\nFetching events...")
    events = fetch_events(case_id)
    print(f"Fetched {len(events)} total events for case {case_id}.")

    matched = [
        e for e in events
        if matches_filter(e, tag_contains, source_contains, title_contains)
    ]

    if not matched:
        print("No events matched your filter. Nothing to do.")
        return

    preview(matched)

    if DRY_RUN:
        print("DRY_RUN is True — no events were deleted.")
        print("Review the list above. If it's correct, set DRY_RUN = False and rerun.")
        return

    confirm = input(
        f"Type DELETE to permanently remove these {len(matched)} events "
        f"from case {case_id}: "
    ).strip()
    if confirm != "DELETE":
        print("Confirmation not received, aborting. Nothing deleted.")
        return

    ids_to_delete = [e.get("event_id") for e in matched]
    print("\nDeleting...")
    delete_events(case_id, ids_to_delete)
    print("\nDone. Re-run the fetch/preview step to confirm they're gone.")


if __name__ == "__main__":
    main()
