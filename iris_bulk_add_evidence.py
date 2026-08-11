#!/usr/bin/env python3
"""
DFIR-IRIS: Bulk-register evidence from a CSV inventory.

Intended workflow:
  1. Inventory your forensic images externally (hashdeep, md5deep, KAPE, your
     own script, etc.) and produce a CSV with one row per piece of evidence.
  2. Run this script. It will:
       - Fetch the case's available evidence types (so you can map your
         inventory's "type" text to the numeric type_id IRIS expects)
       - Read your CSV
       - Print a preview of every evidence record it's about to create
       - Only POST them after you confirm (dry-run by default)

CSV columns expected (rename to match, or edit COLUMN_MAP below):
  filename, file_size, file_hash, type_id, file_description, start_date, end_date

Notes:
  - type_id must be the numeric ID from IRIS's evidence type list, not free text.
    Run this script once with just the "list types" step (see main()) to see
    the mapping for your instance, then fill in type_id in your CSV (or add a
    text->id lookup in TYPE_NAME_TO_ID below).
  - This registers evidence METADATA (hash, size, description, dates) in IRIS's
    chain-of-custody ledger. It does NOT upload the actual image bytes into
    IRIS's Datastore. Keep your forensic images on your existing evidence
    storage; this just gets the record + hash into IRIS.
  - Endpoint paths are for the v2 API documented at docs.dfir-iris.org. Check
    your instance's own API reference if requests come back 404.
"""

import csv
import sys
import requests
import urllib3

# ─────────────────────────── CONFIG ───────────────────────────
IRIS_HOST = ""
API_KEY = ""
CASE_ID = 2
VERIFY_TLS = False

DRY_RUN = True

CSV_PATH = "evidence_inventory_sample.csv"

EVIDENCE_TYPES_LIST_PATH = "manage/evidence-types/list"
EVIDENCE_ADD_PATH = "case/evidences/add"

# Optional: map your inventory tool's type strings to IRIS type_id values,
# once you know them from the fetched list (see fetch_evidence_types()).
# e.g. {"HDD image": 2, "Memory dump": 5}
TYPE_NAME_TO_ID = {}
# ────────────────────────────────────────────────────────────────

if not VERIFY_TLS:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
})
session.verify = VERIFY_TLS


def fetch_evidence_types():
    url = f"{IRIS_HOST.rstrip('/')}/{EVIDENCE_TYPES_LIST_PATH}"
    resp = session.get(url)
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("data", payload)


def print_evidence_types(types_data):
    print("\nAvailable evidence types on this instance:")
    print(f"{'ID':<6} {'Name':<30} {'Description'}")
    print("-" * 80)
    # Shape varies by version; handle both a flat list and a wrapped dict
    items = types_data if isinstance(types_data, list) else types_data.get("types", [])
    for t in items:
        tid = t.get("id", "?")
        name = (t.get("name") or "")[:28]
        desc = t.get("description") or ""
        print(f"{tid:<6} {name:<30} {desc}")
    print("-" * 80)
    print("Use these IDs in your CSV's type_id column, or fill in TYPE_NAME_TO_ID.\n")


def load_inventory(csv_path):
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def resolve_type_id(row):
    if row.get("type_id"):
        return row["type_id"]
    type_name = row.get("type_name") or row.get("type")
    if type_name and type_name in TYPE_NAME_TO_ID:
        return TYPE_NAME_TO_ID[type_name]
    return None


def build_payload(row):
    return {
        "filename": row.get("filename", "").strip(),
        "file_size": row.get("file_size", "").strip(),
        "file_hash": row.get("file_hash", "").strip(),
        "type_id": resolve_type_id(row),
        "file_description": row.get("file_description", "").strip(),
        "start_date": row.get("start_date", "").strip(),
        "end_date": row.get("end_date", "").strip(),
    }


def preview(payloads):
    print(f"\n{'Filename':<35} {'Hash':<20} {'Size':<12} {'TypeID':<8} {'Description'}")
    print("-" * 110)
    for p in payloads:
        fname = p["filename"][:33]
        fhash = (p["file_hash"][:18] + "..") if len(p["file_hash"]) > 20 else p["file_hash"]
        size = p["file_size"]
        tid = str(p["type_id"])
        desc = p["file_description"][:30]
        print(f"{fname:<35} {fhash:<20} {size:<12} {tid:<8} {desc}")
    print("-" * 110)
    print(f"Total records to create: {len(payloads)}\n")


def add_evidence(payloads):
    url = f"{IRIS_HOST.rstrip('/')}/{EVIDENCE_ADD_PATH}"
    for p in payloads:
        resp = session.post(url, params={"cid": CASE_ID}, json=p)
        status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        print(f"  {p['filename']} -> HTTP {status} -> {body}")


def main():
    print("Fetching evidence types for this instance...")
    try:
        types_data = fetch_evidence_types()
        print_evidence_types(types_data)
    except Exception as e:
        print(f"Could not fetch evidence types ({e}). Continuing anyway — "
              f"make sure type_id values in your CSV are correct.\n")

    print(f"Loading inventory from {CSV_PATH}...")
    rows = load_inventory(CSV_PATH)
    print(f"Loaded {len(rows)} rows.")

    payloads = [build_payload(r) for r in rows]

    missing_type = [p["filename"] for p in payloads if not p["type_id"]]
    if missing_type:
        print(f"\nWARNING: {len(missing_type)} row(s) have no resolvable type_id "
              f"and will likely fail: {missing_type}")

    preview(payloads)

    if DRY_RUN:
        print("DRY_RUN is True — nothing was submitted.")
        print("Review the preview above. If it's correct, set DRY_RUN = False and rerun.")
        return

    confirm = input(
        f"Type CONFIRM to create these {len(payloads)} evidence records "
        f"in case {CASE_ID}: "
    ).strip()
    if confirm != "CONFIRM":
        print("Confirmation not received, aborting. Nothing submitted.")
        return

    print("\nSubmitting...")
    add_evidence(payloads)
    print("\nDone. Check the case's Evidence tab to confirm.")


if __name__ == "__main__":
    main()
