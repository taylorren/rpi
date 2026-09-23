#!/usr/bin/env python3
"""Syntax-check the JavaScript embedded in ui/index.html.

A syntax error in the inline script stops the entire script from running, which
leaves the page stuck on its initial "loading" state with no visible error. That
failure mode is easy to ship and easy to miss, so check it here rather than
discovering it in a browser.

Requires Node.js for `node --check`. If Node is unavailable the check is
skipped rather than failed, so this never blocks the pipeline.

Usage::

    python tools/check_ui_js.py
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rpi import paths  # noqa: E402

SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)


def extract_scripts(html: str) -> List[str]:
    return [match.group(1) for match in SCRIPT_RE.finditer(html)]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("Usage")[0].strip(),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--html", type=Path, default=paths.UI_DIR / "index.html")
    args = parser.parse_args(argv)

    if not args.html.exists():
        print("ERROR no such file: {}".format(args.html))
        return 2

    node = shutil.which("node")
    scripts = extract_scripts(args.html.read_text(encoding="utf-8"))
    if not scripts:
        print("no <script> blocks found in {}".format(args.html))
        return 2

    print("{}: {} script block(s), {} bytes of JS".format(
        args.html.name, len(scripts), sum(len(s) for s in scripts)))

    if node is None:
        print("SKIP: Node.js not found, cannot syntax-check")
        print("      install Node, or verify in a browser instead")
        return 0

    failures = 0
    for index, source in enumerate(scripts, start=1):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(source)
            temp_path = Path(handle.name)
        try:
            # --check parses without executing.
            result = subprocess.run([node, "--check", str(temp_path)],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                print("  block {}: OK".format(index))
            else:
                failures += 1
                print("  block {}: SYNTAX ERROR".format(index))
                for line in (result.stderr or "").strip().splitlines():
                    print("    {}".format(line))
        finally:
            temp_path.unlink(missing_ok=True)

    if failures:
        print("{} of {} block(s) failed".format(failures, len(scripts)))
        return 1
    print("all blocks parse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
