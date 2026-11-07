#!/usr/bin/env python3
"""Check that GitHub Pages serves the status.json this run just built.

Usage: python3 verify_live.py --built public/status.json --url https://status.example/status.json

GitHub Pages can report a deployment as successful and keep serving the previous one (the page froze for 19 hours
on 2026-11-06, status#6). Exit 1 when the live generated_at is still older than the one built, after the retries.
"""
import argparse
import json
import sys
import time
import urllib.request


def generated_at(raw):
    return json.loads(raw)["generated_at"]


def is_live(built, live):
    """The live page shows this build or a later one (ISO 8601 UTC with the same offset, so strings order by time)."""
    return live >= built


def wait_for(built, read_live, attempts=18, delay=10, sleep=time.sleep):
    """Poll until the live page shows `built`; return the last generated_at seen (None if it could not be read)."""
    seen = None
    for attempt in range(attempts):
        try:
            seen = generated_at(read_live())
            if is_live(built, seen):
                return seen
        except (OSError, ValueError, KeyError, TypeError):
            seen = None
        if attempt < attempts - 1:
            sleep(delay)
    return seen


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--built", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--attempts", type=int, default=18)
    parser.add_argument("--delay", type=int, default=10)
    args = parser.parse_args(argv)
    with open(args.built, encoding="utf-8") as handle:
        built = generated_at(handle.read())

    def read_live():
        # The query string keeps a cached copy of the plain URL out of the comparison.
        request = urllib.request.Request(f"{args.url}?verify={time.time_ns()}", headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read()

    seen = wait_for(built, read_live, args.attempts, args.delay)
    if seen is not None and is_live(built, seen):
        print(f"live: {seen} (built {built})")
        return 0
    print(f"::error::GitHub Pages still serves {seen or 'nothing readable'}, built {built}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
