#!/usr/bin/env python3
"""
ioc_log_timeline.py

Scans a flat directory of log files (mixed/unknown format — IIS W3C,
SharePoint ULS, syslog-style, etc.) for IOC hits and builds a DFIR-IRIS
compatible timeline CSV, ready to import.

For every line that contains one or more IOCs:
  - event_raw      <- the full raw log line
  - event_iocs     <- the IOC(s) that matched, pipe-separated (e.g. IOC1|IOC2)
                       -- DFIR-IRIS's CSV importer only recognizes "," or "|"
                       as the multi-value delimiter for event_iocs/event_assets.
  - event_date     <- the timestamp parsed from the line (millisecond precision,
                       e.g. 2026-07-15T18:02:15.930)
  - event_tz       <- "+00:00" (assumes all source logs are UTC)
  - event_source   <- the ORIGINAL absolute path of the file the line came
                       from (before it was copied locally), read from a
                       _copy_manifest.csv written by copy_from_list.py. If no
                       manifest is found under --logs-dir, this falls back to
                       the path of the file as scanned (see console output --
                       Claude will warn you if it had to fall back).
  - event_assets   <- the hostname, IF the file lives in a per-host subfolder
                       directly under --logs-dir (i.e. copy_from_list.py's
                       --by-host flag was used). Otherwise left blank.
  - event_title, event_category, event_content, event_tags are left blank
    for manual completion in IRIS.

Timestamp detection order (first match wins, per line):
  1. If the file has an IIS W3C "#Fields:" header, use the declared
     date/time (or date-time) columns.
  2. Otherwise, fall back to generic pattern matching anywhere in the line:
     ISO8601 (2026-07-15 18:02:15.930000), US-style (07/15/2026 18:02:15.93),
     or syslog-style (Jul 15 18:02:15 -- year assumed to be current year,
     since syslog doesn't include one).

IOC matching is case-sensitive and uses boundary checks so an IOC like
"1.2.3.4" will not falsely match inside "1.2.3.44" or "11.2.3.4".

Usage:
    python3 ioc_log_timeline.py --logs-dir /path/to/logs --ioc-file iocs.txt --output timeline.csv

IOC file format: one IOC per line, blank lines and lines starting with #
are ignored.
"""

import argparse
import csv
import os
import re
import sys
from datetime import datetime, timezone

MANIFEST_NAME = "_copy_manifest.csv"

IRIS_FIELDS = [
    "event_date",
    "event_tz",
    "event_title",
    "event_category",
    "event_content",
    "event_raw",
    "event_source",
    "event_assets",
    "event_iocs",
    "event_tags",
]

# --- Generic timestamp patterns, tried in order if IIS header isn't present ---
TIMESTAMP_PATTERNS = [
    # ISO8601: 2026-07-15T18:02:15.930000 / 2026-07-15 18:02:15.93 / no fraction
    (re.compile(r'\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)\b'),
     ["%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f",
      "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]),

    # US-style: 07/15/2026 18:02:15.93 / 07/15/2026 18:02:15
    (re.compile(r'\b(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\b'),
     ["%m/%d/%Y %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S"]),

    # Syslog style: Jul 15 18:02:15 (no year present in the log line)
    (re.compile(r'\b([A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2})\b'),
     ["%b %d %H:%M:%S"]),
]


def build_ioc_matchers(iocs):
    """Compile a boundary-aware, case-sensitive regex for each IOC."""
    matchers = []
    for ioc in iocs:
        ioc = ioc.strip()
        if not ioc or ioc.startswith("#"):
            continue
        escaped = re.escape(ioc)
        pattern = re.compile(r'(?<![\w.:-])' + escaped + r'(?![\w.:-])')
        matchers.append((ioc, pattern))
    return matchers


def find_iocs_in_line(line, matchers):
    return [ioc for ioc, pattern in matchers if pattern.search(line)]


def parse_generic_timestamp(line):
    for regex, formats in TIMESTAMP_PATTERNS:
        m = regex.search(line)
        if not m:
            continue
        text = m.group(1)
        for fmt in formats:
            try:
                dt = datetime.strptime(text, fmt)
                if fmt == "%b %d %H:%M:%S":
                    dt = dt.replace(year=datetime.now(timezone.utc).year)
                return dt
            except ValueError:
                continue
    return None


def format_iris_timestamp(dt):
    # Millisecond precision (3 digits), e.g. 2026-07-15T18:02:15.930
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


class IISHeaderState:
    """Tracks the most recent #Fields: header seen in a file, so it also
    works on files with multiple rolled-over headers."""

    def __init__(self):
        self.fields = None
        self.date_idx = None
        self.time_idx = None
        self.datetime_idx = None

    def consume(self, line):
        if line.startswith("#Fields:"):
            self.fields = line[len("#Fields:"):].strip().split()
            self.date_idx = self.fields.index("date") if "date" in self.fields else None
            self.time_idx = self.fields.index("time") if "time" in self.fields else None
            self.datetime_idx = self.fields.index("date-time") if "date-time" in self.fields else None
            return True
        return False

    def extract_timestamp(self, line):
        if self.fields is None:
            return None
        parts = line.split()
        try:
            if self.datetime_idx is not None and self.datetime_idx < len(parts):
                text = parts[self.datetime_idx]
                return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
            if (self.date_idx is not None and self.time_idx is not None
                    and self.date_idx < len(parts) and self.time_idx < len(parts)):
                date_text = parts[self.date_idx]
                time_text = parts[self.time_idx]
                if "." in time_text:
                    return datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M:%S.%f")
                return datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M:%S")
        except (ValueError, IndexError):
            return None
        return None


def process_file(filepath, matchers, rows, needs_review, asset="", event_source=""):
    iis = IISHeaderState()
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for lineno, raw_line in enumerate(f, start=1):
                line = raw_line.rstrip("\n\r")
                if not line.strip():
                    continue
                if iis.consume(line):
                    continue
                if line.startswith("#"):
                    continue

                hits = find_iocs_in_line(line, matchers)
                if not hits:
                    continue

                dt = iis.extract_timestamp(line) if iis.fields is not None else None
                if dt is None:
                    dt = parse_generic_timestamp(line)

                if dt is None:
                    needs_review.append(
                        f"{filepath}:{lineno}: IOC hit but no timestamp could be parsed -> {line[:200]}"
                    )
                    event_date = ""
                else:
                    event_date = format_iris_timestamp(dt)

                rows.append({
                    "event_date": event_date,
                    "event_tz": "+00:00",
                    "event_title": "",
                    "event_category": "",
                    "event_content": "",
                    "event_raw": line,
                    "event_source": event_source,
                    "event_assets": asset,
                    "event_iocs": "|".join(hits),
                    "event_tags": "",
                })
    except Exception as e:
        print(f"[!] Failed to read {filepath}: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Scan logs for IOC hits and build a DFIR-IRIS timeline CSV.")
    ap.add_argument("--logs-dir", required=True, help="Directory containing log files (flat, non-recursive).")
    ap.add_argument("--ioc-file", required=True, help="Text file with one IOC per line.")
    ap.add_argument("--output", default="timeline.csv", help="Output CSV path.")
    args = ap.parse_args()

    if not os.path.isdir(args.logs_dir):
        print(f"[!] Not a directory: {args.logs_dir}", file=sys.stderr)
        sys.exit(1)

    with open(args.ioc_file, "r", encoding="utf-8", errors="replace") as f:
        iocs = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]

    if not iocs:
        print("[!] No IOCs loaded from IOC file.", file=sys.stderr)
        sys.exit(1)

    matchers = build_ioc_matchers(iocs)

    exclude = {os.path.abspath(args.ioc_file), os.path.abspath(args.output)}
    logs_dir_abs = os.path.abspath(args.logs_dir)

    # Load the copy manifest (copied_path -> original_path) if copy_from_list.py
    # produced one. Its own filename is always excluded from scanning.
    manifest = {}
    manifest_path = os.path.join(args.logs_dir, MANIFEST_NAME)
    exclude.add(os.path.abspath(manifest_path))
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", newline="", encoding="utf-8", errors="replace") as mf:
            for row in csv.DictReader(mf):
                cp = row.get("copied_path")
                op = row.get("original_path")
                if cp and op:
                    manifest[os.path.abspath(cp)] = op
        print(f"[*] Loaded manifest: {manifest_path} ({len(manifest)} entries)")
    else:
        print(f"[*] No manifest found at {manifest_path} -- event_source will use "
              f"the scanned file path instead of the original source path.")

    # Walk recursively so per-host subfolders (e.g. from copy_from_list.py's
    # --by-host mode) are picked up. A file directly under --logs-dir has no
    # host subfolder and gets asset="".
    files_with_meta = []
    fallback_count = 0
    for root, _dirs, filenames in os.walk(args.logs_dir):
        for fname in sorted(filenames):
            fp = os.path.join(root, fname)
            fp_abs = os.path.abspath(fp)
            if fp_abs in exclude:
                continue
            rel = os.path.relpath(root, logs_dir_abs)
            asset = "" if rel == "." else rel.split(os.sep)[0]
            if fp_abs in manifest:
                event_source = manifest[fp_abs]
            else:
                event_source = fp_abs
                fallback_count += 1
            files_with_meta.append((fp, asset, event_source))

    if not files_with_meta:
        print(f"[!] No files found in {args.logs_dir}", file=sys.stderr)
        sys.exit(1)

    if manifest and fallback_count:
        print(f"[!] {fallback_count} file(s) had no manifest entry -- their event_source "
              f"is the scanned path, not the original source path.")

    print(f"[*] Loaded {len(iocs)} IOC(s)")
    print(f"[*] Scanning {len(files_with_meta)} file(s) under {args.logs_dir}")

    rows = []
    needs_review = []
    for fp, asset, event_source in files_with_meta:
        process_file(fp, matchers, rows, needs_review, asset=asset, event_source=event_source)

    with open(args.output, "w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=IRIS_FIELDS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[+] Wrote {len(rows)} timeline row(s) to {args.output}")

    if needs_review:
        review_path = os.path.splitext(args.output)[0] + "_needs_review.txt"
        with open(review_path, "w", encoding="utf-8") as rf:
            rf.write("\n".join(needs_review))
        print(f"[!] {len(needs_review)} IOC hit(s) had no parseable timestamp -> see {review_path}")
        print("    Those rows are still in the CSV with an empty event_date(UTC); fill in manually.")


if __name__ == "__main__":
    main()
