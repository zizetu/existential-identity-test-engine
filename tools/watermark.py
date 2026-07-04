#!/usr/bin/env python3
"""
Build-time watermark tool for EITElite / EITE.

The public repo always carries the open-source watermark.
For commercial builds, see the private eite-agent repo's version
which generates unique per-customer nonces.

Usage:
    python3 tools/watermark.py check [--dir ./release]

Commands:
    check     Verify all watermark occurrences in a directory
    inspect   Show build provenance from __tical_watermark__
"""

import argparse
import os
import re
import sys

WATERMARK_PATTERN = re.compile(r'__tical_watermark__\s*=\s*"([^"]*)"')
SECONDARY_PATTERN = re.compile(r"# __wm__:\s*(\S+)")

PUBLIC_WATERMARK = "EITE_PUBLIC_OPEN_SOURCE"


def cmd_check(args):
    """Verify all watermark occurrences in a directory."""
    root = args.dir or "."
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(dirpath, fn)
            with open(fp) as f:
                content = f.read()
            m = WATERMARK_PATTERN.search(content)
            if m:
                results.append((fp, m.group(1)))
            sm = SECONDARY_PATTERN.search(content)
            if sm:
                results.append((fp, f"secondary:{sm.group(1)}"))

    if not results:
        print("No watermark found.")
        return 1

    for fp, wm in sorted(results):
        label = "PRIMARY" if wm.startswith("EITE_") else "SECONDARY"
        print(f"  [{label}] {wm}")
        print(f"          {fp}")

    return 0


def cmd_inspect(args):
    """Show build provenance from __tical_watermark__."""
    target = args.dir or "."
    identity_file = os.path.join(target, "identity", "__init__.py")
    if not os.path.exists(identity_file):
        print("No identity/__init__.py found.")
        return 1

    with open(identity_file) as f:
        m = WATERMARK_PATTERN.search(f.read())
    if m:
        wm = m.group(1)
        if wm == PUBLIC_WATERMARK:
            print(f"Build: PUBLIC OPEN SOURCE ({wm})")
        elif wm.startswith("EITE_CUST_"):
            print(f"Build: COMMERCIAL (customer hash: {wm[len('EITE_CUST_'):]})")
        else:
            print(f"Build: UNKNOWN ({wm})")
    else:
        print("No watermark found in identity/__init__.py")

    return 0


def main():
    parser = argparse.ArgumentParser(description="Build watermark utility")
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="Check watermarks in a directory")
    p_check.add_argument("--dir", default=".", help="Directory to scan")

    p_inspect = sub.add_parser("inspect", help="Show build provenance")
    p_inspect.add_argument("--dir", default=".", help="Directory containing identity/")

    args = parser.parse_args()
    if args.command == "check":
        return cmd_check(args)
    elif args.command == "inspect":
        return cmd_inspect(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
