"""RPI (Renpin Index) - news analysis and index calculation.

Pipeline:

    fetcher -> inbox/*.jsonl -> ingest -> SQLite -> analyse -> rpi -> export -> ui

The fetcher lives outside this package and only produces news. Everything that
consumes it lives here.
"""

import sys as _sys


def _force_utf8_output() -> None:
    """Make stdout and stderr UTF-8 so news text can never crash a print.

    Windows consoles default to a legacy codepage - GBK on a Chinese install.
    Printing a headline containing, say, a zero-width space then raises
    UnicodeEncodeError and kills the whole run. That is especially nasty for
    the scheduled job, whose output is redirected to a file: it fails with an
    encoding error from a print statement, far from anything obviously wrong.

    ``errors="replace"`` guarantees no write can ever fail, whatever the data.
    Only the encoding is changed; buffering is left alone.
    """
    for stream in (_sys.stdout, _sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # Not a reconfigurable stream (e.g. redirected to something exotic);
            # nothing to do, and nothing worth failing over.
            pass


_force_utf8_output()

__all__ = ["api", "calculator", "config", "dedupe", "export", "ingest",
           "paths", "schema", "storage"]
