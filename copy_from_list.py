#!/usr/bin/env python3
"""
copy_from_list.py

Reads a text file of absolute file paths (one per line) and copies each
file into a destination directory.

Handles filename collisions automatically: if two source files share the
same filename (e.g. from different folders), later ones get a numeric
suffix (file.log, file_1.log, file_2.log, ...) instead of overwriting.

Usage:
    python3 copy_from_list.py --path-list paths.txt --dest /path/to/destination

Options:
    --dry-run   Show what would be copied without actually copying.
    --flat      (default) All files land directly in --dest, collisions
                renamed as above.
    --preserve-structure
                Recreate each file's original absolute path under --dest
                instead of flattening (avoids collisions entirely).
    --by-host   Copy into --dest/<hostname>/<filename>, where <hostname> is
                extracted from the source path itself (the folder name
                right after the third "/", e.g. /mnt/IOC_SCAN/<hostname>/...).
                Collisions are only checked within the same host's subfolder.
                This is the mode to use if you want ioc_log_timeline.py to
                auto-fill the linked_assets column.
"""

import argparse
import os
import shutil
import sys


def extract_hostname(path):
    """Pull the folder name right after the third '/' in an absolute path,
    e.g. /mnt/IOC_SCAN/<hostname>/evidence/file.log -> <hostname>."""
    parts = path.split("/")
    # parts[0] is '' (leading slash), so the segment after the 3rd '/' is parts[3]
    if len(parts) > 3 and parts[3]:
        return parts[3]
    return "unknown_host"


def unique_destination(dest_dir, filename):
    """Return a path in dest_dir that doesn't collide with an existing file,
    appending _1, _2, ... before the extension if needed."""
    candidate = os.path.join(dest_dir, filename)
    if not os.path.exists(candidate):
        return candidate

    base, ext = os.path.splitext(filename)
    n = 1
    while True:
        candidate = os.path.join(dest_dir, f"{base}_{n}{ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def main():
    ap = argparse.ArgumentParser(description="Bulk-copy files from a list of absolute paths.")
    ap.add_argument("--path-list", required=True, help="Text file with one absolute path per line.")
    ap.add_argument("--dest", required=True, help="Destination directory.")
    ap.add_argument("--dry-run", action="store_true", help="Show actions without copying anything.")
    ap.add_argument("--preserve-structure", action="store_true",
                     help="Recreate original absolute paths under --dest instead of flattening.")
    ap.add_argument("--by-host", action="store_true",
                     help="Copy into --dest/<hostname>/<filename>, hostname taken from the "
                          "source path's segment after the third '/'.")
    args = ap.parse_args()

    if args.preserve_structure and args.by_host:
        print("[!] --preserve-structure and --by-host are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(args.path_list):
        print(f"[!] Path list not found: {args.path_list}", file=sys.stderr)
        sys.exit(1)

    with open(args.path_list, "r", encoding="utf-8", errors="replace") as f:
        paths = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]

    if not paths:
        print("[!] No paths found in the list.", file=sys.stderr)
        sys.exit(1)

    if not args.dry_run:
        os.makedirs(args.dest, exist_ok=True)

    copied = 0
    missing = []
    skipped_dirs = []

    for src in paths:
        if not os.path.exists(src):
            missing.append(src)
            continue
        if os.path.isdir(src):
            skipped_dirs.append(src)
            continue

        if args.preserve_structure:
            # Strip the leading separator/drive so os.path.join behaves,
            # then recreate the full original path under dest.
            rel = src.lstrip("/\\").replace(":", "")  # windows drive letter safety
            dst = os.path.join(args.dest, rel)
        elif args.by_host:
            hostname = extract_hostname(src)
            host_dir = os.path.join(args.dest, hostname)
            if not args.dry_run:
                os.makedirs(host_dir, exist_ok=True)
            dst = unique_destination(host_dir, os.path.basename(src))
        else:
            dst = unique_destination(args.dest, os.path.basename(src))

        if args.dry_run:
            print(f"[dry-run] {src} -> {dst}")
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            print(f"[+] {src} -> {dst}")
        copied += 1

    print(f"\n[*] {copied} file(s) {'would be ' if args.dry_run else ''}copied.")
    if skipped_dirs:
        print(f"[!] {len(skipped_dirs)} path(s) were directories, skipped:")
        for d in skipped_dirs:
            print(f"    {d}")
    if missing:
        print(f"[!] {len(missing)} path(s) not found:")
        for m in missing:
            print(f"    {m}")


if __name__ == "__main__":
    main()
